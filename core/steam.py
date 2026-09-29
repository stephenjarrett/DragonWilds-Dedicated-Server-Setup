"""SteamCMD execution and public-branch build discovery."""
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

APP_ID = "4019830"


def parse_vdf(text):
    tokens = re.findall(r'//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+', text)
    tokens = [t for t in tokens if not t.startswith("//")]
    position = 0

    def value(token):
        if token.startswith('"'):
            return re.sub(r'\\(["\\])', r'\1', token[1:-1])
        return token

    def block(nested=False):
        nonlocal position
        result = {}
        while position < len(tokens):
            key = tokens[position]
            position += 1
            if key == "}":
                if not nested:
                    raise ValueError("Unexpected closing brace in Steam metadata.")
                return result
            if key == "{" or position >= len(tokens):
                raise ValueError("Incomplete Steam metadata.")
            item = tokens[position]
            position += 1
            if item == "}":
                raise ValueError("Missing Steam metadata value.")
            result[value(key)] = block(True) if item == "{" else value(item)
        if nested:
            raise ValueError("Incomplete Steam metadata block.")
        return result
    return block()


def public_build(output):
    match = re.search(r'"' + APP_ID + r'"\s*\{', output)
    if not match:
        raise RuntimeError("Steam returned no app metadata. Check the connection and retry.")
    segment = output[match.start():]
    depth = 0
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|[{}]', segment):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                data = parse_vdf(segment[:token.end()])
                try:
                    build = data[APP_ID]["depots"]["branches"]["public"]["buildid"]
                    if not str(build).isdigit():
                        raise ValueError()
                    return str(build)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError("Steam did not provide a valid public build ID.") from exc
    raise RuntimeError("Steam returned incomplete app metadata.")


def installed_build(install_dir):
    root = Path(install_dir)
    paths = [root / "steamapps" / f"appmanifest_{APP_ID}.acf",
             root / f"appmanifest_{APP_ID}.acf"]
    library_manifest = None
    if root.parent.name.casefold() == "common" and root.parent.parent.name.casefold() == "steamapps":
        library_manifest = root.parent.parent / f"appmanifest_{APP_ID}.acf"
        paths.append(library_manifest)
    for path in paths:
        if path.is_file():
            try:
                app = parse_vdf(path.read_text(encoding="utf-8"))["AppState"]
                build = str(app["buildid"])
                if path == library_manifest and str(app.get("installdir", "")).casefold() != root.name.casefold():
                    continue
                if str(app.get("appid")) == APP_ID and build.isdigit():
                    return build
            except (ValueError, KeyError, TypeError):
                pass
    return None


def run(executable, arguments, log, timeout=3600):
    """Stream output and bound hung commands, including their subprocesses."""
    import psutil
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise RuntimeError("SteamCMD is missing. Run Install / repair first.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen([str(executable), *arguments], cwd=executable.parent,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=flags, bufsize=1) as process:
        lines = queue.Queue()

        def read():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        deadline = time.monotonic() + timeout
        output = []
        ended = False
        try:
            while not ended or process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"SteamCMD timed out after {timeout // 60} minutes.")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                else:
                    output.append(line)
                    if line.strip():
                        log(line.rstrip(), "INFO")
            return process.wait(), "".join(output)
        except BaseException:
            try:
                for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait(timeout=10)
            except (psutil.Error, OSError, subprocess.TimeoutExpired):
                pass
            raise


def command(install_dir, arguments, log, timeout=3600):
    executable = Path(install_dir) / "steamcmd" / "steamcmd.exe"
    for attempt in range(2):
        code, output = run(executable, arguments, log, timeout)
        if code == 7 and attempt == 0:
            log("SteamCMD updated itself; retrying once.", "WARN")
            continue
        if code != 0:
            raise RuntimeError(f"SteamCMD exited with code {code}. See the activity log.")
        return output


def latest_build(install_dir, log):
    output = command(install_dir, ["+login", "anonymous", "+app_info_update", "1",
                     "+app_info_print", APP_ID, "+quit"], log, timeout=180)
    return public_build(output)


def install(install_dir, log):
    output = command(install_dir, ["+force_install_dir", str(Path(install_dir).resolve()),
                     "+login", "anonymous", "+app_update", APP_ID, "validate", "+quit"], log)
    if not re.search(r"Success!\s+App\s+'?" + APP_ID + r"'?\s+fully installed", output, re.I):
        raise RuntimeError("SteamCMD did not confirm a successful install. See the activity log.")
