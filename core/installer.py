"""Installation, non-destructive configuration editing, and firewall rules."""
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from core import steam

STEAM_CMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAM_APP_ID = steam.APP_ID
SERVER_EXE = "RSDragonwildsServer.exe"
LOCAL_APP = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
CONFIG_WIN = LOCAL_APP / "RSDragonwilds/Saved/Config/WindowsServer"
CONFIG_FILE = CONFIG_WIN / "DedicatedServer.ini"
SAVEGAMES_DIR = LOCAL_APP / "RSDragonwilds/Saved/SaveGames"
SECTIONS = ("/Script/Dominion.DedicatedServerSettings", "/Script/RSDragonwilds.DedicatedServerConfig")
KEYS = {"owner_id": "OwnerId", "server_name": "ServerName", "world_name": "DefaultWorldName",
        "admin_pass": "AdminPassword", "world_pass": "WorldPassword", "port": "Port"}


def download_steamcmd(steamcmd_dir, log):
    directory = Path(steamcmd_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    log("Downloading SteamCMD from Steam...")
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        archive = Path(temp) / "steamcmd.zip"
        with urllib.request.urlopen(STEAM_CMD_URL, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                target = (directory / item.filename).resolve()
                if not target.is_relative_to(directory):
                    raise RuntimeError("SteamCMD archive contains an unsafe path.")
            source.extractall(directory)
    if not (directory / "steamcmd.exe").is_file():
        raise RuntimeError("SteamCMD download did not contain steamcmd.exe.")
    log("SteamCMD is ready.", "OK")


def find_server_exe(install_dir):
    root = Path(install_dir)
    direct = root / SERVER_EXE
    if direct.is_file():
        return str(direct.resolve())
    if root.is_dir():
        for candidate in root.rglob(SERVER_EXE):
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def _dedicated_config_paths_from_exe(exe_path):
    if not exe_path:
        return []
    base = Path(exe_path).resolve().parent
    return [base / "RSDragonwilds/Saved/Config/WindowsServer/DedicatedServer.ini",
            base / "Saved/Config/WindowsServer/DedicatedServer.ini"]


def dedicated_savegames_paths_from_exe(exe_path):
    if not exe_path:
        return []
    base = Path(exe_path).resolve().parent
    return [base / "RSDragonwilds/Saved/SaveGames", base / "Saved/SaveGames"]


def validate_config(cfg):
    errors = []
    for key, label in (("owner_id", "Player ID"), ("server_name", "Server name"),
                       ("world_name", "World name"), ("admin_pass", "Admin password")):
        if not str(cfg.get(key, "")).strip():
            errors.append(f"{label} is required.")
    for key in KEYS:
        if any(character in str(cfg.get(key, "")) for character in ("\r", "\n", "\0")):
            errors.append(f"{KEYS[key]} must be a single line.")
    try:
        if not 1 <= int(cfg.get("port", "")) <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        errors.append("UDP port must be a number between 1 and 65535.")
    return errors


def write_config(cfg, log):
    errors = validate_config(cfg)
    if errors:
        raise ValueError(" ".join(errors))
    paths = list(dict.fromkeys([CONFIG_FILE, *_dedicated_config_paths_from_exe(cfg.get("server_exe"))]))
    for path in paths:
        # Edit only managed assignments; preserve comments, duplicate Unreal keys,
        # admin lists and all settings owned by the game or the user.
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        output, section, seen, visited = [], "", set(), set()

        def finish():
            if section in SECTIONS:
                for field, ini_key in KEYS.items():
                    if ini_key.casefold() not in seen:
                        output.append(f"{ini_key}={cfg.get(field, '')}")

        reverse = {v.casefold(): k for k, v in KEYS.items()}
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                finish()
                section, seen = stripped[1:-1], set()
                visited.add(section)
            if section in SECTIONS and "=" in line and not stripped.startswith((";", "#")):
                key = line.partition("=")[0].strip().casefold()
                if key in reverse:
                    if key not in seen:
                        output.append(f"{KEYS[reverse[key]]}={cfg.get(reverse[key], '')}")
                        seen.add(key)
                    continue
            output.append(line)
        finish()
        for name in SECTIONS:
            if name not in visited:
                output.extend(["", f"[{name}]"])
                output.extend(f"{ini_key}={cfg.get(field, '')}" for field, ini_key in KEYS.items())
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(".ini.bak"))
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(output) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
        log(f"Configuration saved: {path}", "OK")


def read_config(config_path=None):
    path = Path(config_path) if config_path else CONFIG_FILE
    if not path.is_file():
        return {}
    data, section = {}, ""
    keys = {value.casefold(): key for key, value in KEYS.items()}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section in SECTIONS and "=" in line and not stripped.startswith((";", "#")):
            key, _, value = line.partition("=")
            if key.strip().casefold() in keys:
                data[keys[key.strip().casefold()]] = value.strip()
    return data


def set_firewall(port, log):
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("UDP port must be between 1 and 65535.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    failures = []
    for direction in ("in", "out"):
        name = f"RSDragonwilds-UDP-{port}-{direction}"
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                       capture_output=True, timeout=30, creationflags=flags)
        result = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                                 f"name={name}", f"dir={direction}", "action=allow", "protocol=UDP",
                                 f"localport={port}", "profile=any", "enable=yes"],
                                capture_output=True, text=True, timeout=30, creationflags=flags)
        if result.returncode:
            failures.append((result.stdout + result.stderr).strip())
        else:
            log(f"Firewall rule created: {name}", "OK")
    if failures:
        raise RuntimeError("Firewall failed. Run the manager as Administrator. " + " ".join(failures))
    log(f"Windows Firewall configured. Router forwarding for UDP {port} is separate.", "OK")
