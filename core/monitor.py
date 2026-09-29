"""Exact-install process tracking and conservative player activity detection."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

JOIN = re.compile(r"Join succeeded:\s*(.+)")
LEAVE = re.compile(r"Player Removed from session \[[^\]]+\]-\[(.+)\]")
READY = re.compile(r"GameNetDriver.*(?:Listening|listening)|Game Engine Initialized|LogLoad:.*Took.*LoadMap")
NAMES = {"rsdragonwildsserver.exe", "rsdragonwildsserver-win64-shipping.exe"}


def identity(process):
    return {"pid": process.pid, "created": process.create_time()}


def same_process(record):
    try:
        process = psutil.Process(record["pid"])
        return process if abs(process.create_time() - record["created"]) < 0.01 else None
    except (psutil.Error, KeyError, TypeError):
        return None


class ServerMonitor:
    def __init__(self):
        self.exe = ""
        self.processes = []
        self.players = set()
        self.player_count_known = False
        self.error = ""
        self._identity = None
        self._log_key = None
        self._offset = 0
        self._partial = b""
        self.empty_since = None

    def poll(self, exe):
        self.exe = str(exe or "")
        self.processes = []
        self.error = ""
        if not self.exe:
            self._reset()
            return
        selected = Path(self.exe).resolve()
        for process in psutil.process_iter(["name", "exe", "create_time"]):
            try:
                name = (process.info["name"] or "").casefold()
                if name not in NAMES and name != selected.name.casefold():
                    continue
                executable = process.info.get("exe")
                if not executable:
                    self.error = "Cannot read a server process path; run with matching permissions."
                    continue
                path = Path(executable).resolve()
                if path == selected or (path.is_relative_to(selected.parent) and name in NAMES):
                    self.processes.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                self.error = "Server process access denied."
        self.processes.sort(key=lambda p: p.create_time())
        current = tuple((p.pid, p.create_time()) for p in self.processes)
        if current != self._identity:
            self._reset()
            self._identity = current
        if self.processes:
            self._read_players()
        else:
            self._reset()
        if self.processes and self.player_count_known and not self.players:
            self.empty_since = self.empty_since or time.time()
        else:
            self.empty_since = None

    def _reset(self):
        self.players.clear()
        self.player_count_known = False
        self._log_key = None
        self._offset = 0
        self._partial = b""
        self.empty_since = None

    def _read_players(self):
        base = Path(self.exe).parent
        paths = [base / "RSDragonwilds/Saved/Logs/RSDragonwilds.log", base / "Saved/Logs/RSDragonwilds.log"]
        try:
            available = [path for path in paths if path.is_file()]
            if not available:
                self.player_count_known = False
                return
            path = max(available, key=lambda p: p.stat().st_mtime_ns)
            stat = path.stat()
            if stat.st_mtime < min(p.create_time() for p in self.processes):
                self.player_count_known = False
                return
            key = (str(path), stat.st_ino, stat.st_ctime_ns)
            if key != self._log_key or stat.st_size < self._offset:
                self._reset()
                self._log_key = key
            with path.open("rb") as stream:
                stream.seek(self._offset)
                data = self._partial + stream.read()
                self._offset = stream.tell()
            lines = data.split(b"\n")
            self._partial = lines.pop()
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").strip()
                if READY.search(line):
                    self.player_count_known = True
                joined, left = JOIN.search(line), LEAVE.search(line)
                if joined:
                    self.players.add(joined.group(1).strip())
                elif left:
                    self.players.discard(left.group(1).strip())
        except (OSError, psutil.Error):
            self.player_count_known = False

    def snapshot(self):
        try:
            created = min((p.create_time() for p in self.processes), default=None)
            return {"online": bool(self.processes), "pids": [p.pid for p in self.processes],
                    "started": created, "players": sorted(self.players),
                    "players_known": self.player_count_known, "empty_since": self.empty_since,
                    "monitor_error": self.error}
        except psutil.Error:
            return {"online": False, "pids": [], "started": None, "players": [],
                    "players_known": False, "empty_since": None, "monitor_error": "Process changed; refreshing."}


def launch(exe):
    if not Path(exe).is_file():
        raise RuntimeError("Server executable not found. Choose it in Server settings or run Install / repair.")
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
    child = subprocess.Popen([str(Path(exe).resolve()), "-log", "-NewConsole"],
                             cwd=Path(exe).resolve().parent, **options)
    return identity(psutil.Process(child.pid))


def signal_console(pid):
    """Used only in a short-lived helper attached to a manager-owned console."""
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.FreeConsole()
    if not kernel.AttachConsole(int(pid)):
        return 1
    kernel.SetConsoleCtrlHandler(None, True)
    success = kernel.GenerateConsoleCtrlEvent(0, 0)
    time.sleep(0.75)
    kernel.FreeConsole()
    return 0 if success else 1


def stop(mon, owned, allow_force, log, timeout=30):
    processes = list(mon.processes)
    if not processes:
        return
    records = [identity(p) for p in processes]
    descendants = []
    for process in processes:
        try:
            descendants.extend(identity(child) for child in process.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
    records.extend(descendants)
    console_target = next((p for p in reversed(processes) if any(
        p.pid == record.get("pid") and same_process(record) is not None for record in owned)), None)
    signalled = False
    if console_target and os.name == "nt":
        entry = [] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve().parents[1] / "main.py")]
        helper = subprocess.run([sys.executable, *entry, "--signal-stop", str(console_target.pid)],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, capture_output=True)
        signalled = helper.returncode == 0
    if signalled:
        log("Requested a console shutdown; waiting for the server to exit...", "INFO")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(same_process(record) for record in records):
            time.sleep(0.25)
    alive = [process for record in records if (process := same_process(record))]
    if alive and not allow_force:
        raise RuntimeError("Server did not stop. Maintenance cancelled. Stop it manually, or enable forced stops in Automation.")
    if alive:
        log("Using the enabled forced-stop fallback; unsaved in-memory progress may be lost.", "WARN")
        for process in reversed(alive):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(alive, timeout=15)
        if remaining:
            raise RuntimeError("Server processes are still running. Maintenance cancelled.")
    log("Server process tree has stopped.", "OK")
