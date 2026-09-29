"""Single serialized worker. No Tk calls, including from logging or scheduling."""
import copy
import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
import time
from pathlib import Path

from core import backups, installer, monitor, scheduler, settings, steam


class ManagerService:
    def __init__(self, prefs, events=None):
        self.prefs = dict(prefs)
        self.events = events if events is not None else queue.Queue()
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.busy = False
        self.closing = threading.Event()
        self.monitor = monitor.ServerMonitor()
        self.state_path = settings.app_dir() / "state.json"
        self.state = settings.read_json(self.state_path)
        self.thread = None
        self.logger = logging.getLogger(f"dragonwilds.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        settings.app_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(settings.app_dir() / "manager.log", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        errors = scheduler.validate(self.prefs)
        if errors:
            self.prefs["auto_backup"] = self.prefs["auto_update"] = False
            self.log("Automation paused: " + " ".join(errors), "WARN")
        scheduler.reconcile(self.prefs, self.state, time.time())
        self._backup_records = []
        self.state["installed_build"] = steam.installed_build(self.prefs["install_dir"])

    def log(self, message, level="INFO"):
        # Never persist passwords if an external tool happens to echo one.
        for key in ("admin_pass", "world_pass"):
            secret = self.prefs.get(key)
            if secret:
                message = str(message).replace(secret, "[redacted]")
        self.events.put(("log", (time.time(), str(message), level)))
        self.logger.log({"ERROR": logging.ERROR, "WARN": logging.WARNING}.get(level, logging.INFO), message)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="manager-worker", daemon=True)
        self.thread.start()

    def submit(self, action, prefs=None):
        with self.lock:
            if self.busy or self.closing.is_set():
                return False
            self.busy = True
            self.commands.put((action, copy.deepcopy(prefs), False))
        return True

    def close(self):
        with self.lock:
            if self.busy:
                return False
            self.closing.set()
        return True

    def _save_state(self):
        settings.atomic_json(self.state_path, self.state)

    def _poll(self):
        self.monitor.poll(self.prefs.get("server_exe"))
        owned = self.state.get("owned_processes", [])
        if any(monitor.same_process(item) for item in owned):
            # Track shipping children sharing the isolated console if the launcher exits.
            for process in self.monitor.processes:
                try:
                    if any(parent.pid == item.get("pid") for parent in process.parents() for item in owned):
                        record = monitor.identity(process)
                        if record not in owned:
                            owned.append(record)
                            self.state["owned_processes"] = owned
                            self._save_state()
                except Exception:
                    pass

    def _publish(self):
        self.events.put(("status", {**self.monitor.snapshot(), "busy": self.busy,
                         "prefs": dict(self.prefs), "state": copy.deepcopy(self.state),
                         "backups": list(self._backup_records)}))

    def _loop(self):
        try:
            self._refresh_backups()
            self._save_state()
        except Exception as exc:
            self.log(f"Could not read backup history or save the schedule: {exc}", "WARN")
        while not self.closing.is_set():
            try:
                try:
                    command = self.commands.get(timeout=1)
                except queue.Empty:
                    command = None
                if command:
                    self._execute(*command)
                    continue
                self._poll()
                if command is None:
                    with self.lock:
                        due = scheduler.due_job(self.prefs, self.state, time.time())
                        if due and not self.busy and not self.closing.is_set():
                            self.busy = True
                            command = (due, None, True)
                if command:
                    self._execute(*command)
                self._publish()
            except Exception as exc:
                # A poll or disk error must not silently kill the scheduler thread.
                self.log(f"Manager error: {exc}", "ERROR")
                self.closing.wait(3)
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

    def _execute(self, action, new_prefs=None, scheduled=False):
        self.state["activity"] = action.replace("_", " ").capitalize()
        self._publish()
        self.log(f"{'Scheduled' if scheduled else 'Starting'}: {self.state['activity']}")
        try:
            self._poll()
            if action in scheduler.JOBS and (scheduled or self.prefs[scheduler.JOBS[action][0]]):
                scheduler.reserve(action, self.prefs, self.state, time.time())
                self._save_state()
            if new_prefs is not None:
                self._configure(new_prefs)
            actions = {"save_settings": lambda: None, "start": self._start_server,
                       "stop": self._stop_server, "restart": self._restart,
                       "backup": lambda: self._backup("scheduled" if scheduled else "manual"),
                       "check_update": lambda: self._check_update(apply=scheduled, scheduled=scheduled),
                       "apply_update": lambda: self._check_update(apply=True),
                       "install": self._install, "write_config": self._write_config,
                       "firewall": lambda: installer.set_firewall(self.prefs["port"], self.log),
                       "refresh_backups": self._refresh_backups}
            result = actions[action]()
            self.state["last_result"] = result or f"{action.replace('_', ' ').capitalize()} complete."
            self.state["last_result_level"] = "OK"
            self.log(self.state["last_result"], "OK")
        except Exception as exc:
            self.state["last_result"] = str(exc)
            self.state["last_result_level"] = "ERROR"
            self.log(f"{action.replace('_', ' ').capitalize()} failed: {exc}", "ERROR")
            if action in scheduler.JOBS and (scheduled or self.prefs[scheduler.JOBS[action][0]]):
                # Bounded retries; a broken disk/network never creates a tight loop.
                self.state[f"next_{action}"] = min(self.state.get(f"next_{action}", float("inf")),
                                                    time.time() + 15 * 60)
            self.events.put(("failure", {"action": action, "message": str(exc), "scheduled": scheduled}))
        finally:
            self.state["activity"] = "Idle"
            try:
                self._save_state()
                self._poll()
                self._refresh_backups()
            except Exception as exc:
                self.log(f"Could not refresh manager state: {exc}", "ERROR")
            with self.lock:
                self.busy = False
            self._publish()

    def _configure(self, prefs):
        errors = scheduler.validate(prefs)
        if errors:
            raise ValueError(" ".join(errors))
        root = Path(prefs["install_dir"]).expanduser()
        if not root.is_absolute() or root.resolve() == Path(root.anchor):
            raise ValueError("Choose an absolute server installation folder, not a drive root.")
        prefs["install_dir"] = str(root.resolve())
        if prefs["server_exe"]:
            exe = Path(prefs["server_exe"]).expanduser()
            if not exe.is_file() or exe.name.casefold() != installer.SERVER_EXE.casefold():
                raise ValueError(f"Choose the server launcher named {installer.SERVER_EXE}.")
            prefs["server_exe"] = str(exe.resolve())
            if exe.resolve().parent != root.resolve():
                raise ValueError("Install folder must be the folder containing the selected server executable.")
        previous = self.prefs
        if previous.get("server_exe") != prefs.get("server_exe") or previous["install_dir"] != prefs["install_dir"]:
            self.state = {key: value for key, value in self.state.items() if key.startswith("next_")}
        settings.save(prefs)
        self.prefs = dict(prefs)
        scheduler.reconcile(self.prefs, self.state, time.time(), previous)
        self.state["installed_build"] = steam.installed_build(self.prefs["install_dir"])
        self.events.put(("preferences", dict(self.prefs)))
        self._poll()

    def _assert_monitor(self):
        if not self.prefs.get("server_exe"):
            found = installer.find_server_exe(self.prefs["install_dir"])
            if found:
                self.prefs["server_exe"] = found
        self._poll()
        if self.monitor.error:
            raise RuntimeError(self.monitor.error)

    def _start_server(self):
        self._assert_monitor()
        if self.monitor.processes:
            return "Server is already running."
        exe = self.prefs.get("server_exe") or installer.find_server_exe(self.prefs["install_dir"])
        if not exe:
            raise RuntimeError("No server executable configured. Use Server settings or Install / repair.")
        self.prefs["server_exe"] = exe
        self._assert_monitor()
        if self.monitor.processes:
            return "Server is already running."
        record = monitor.launch(exe)
        self.state["owned_processes"] = [record]
        settings.save(self.prefs)
        self._save_state()
        self.log("Waiting for the launched server process...", "INFO")
        # Capture the launcher and its shipping child before announcing success.
        for _ in range(16):
            time.sleep(0.5)
            self._poll()
        if not self.monitor.processes:
            raise RuntimeError("Server exited during startup. Open the server log to diagnose it.")
        return "Server process started. World readiness is reported in the server log."

    def _stop_server(self):
        self._assert_monitor()
        monitor.stop(self.monitor, self.state.get("owned_processes", []),
                     self.prefs["allow_force_stop"], self.log)
        self._assert_monitor()
        if self.monitor.processes:
            raise RuntimeError("Server is still running; maintenance cancelled.")
        self.state["owned_processes"] = []

    def _backup(self, reason):
        self._assert_monitor()
        target = backups.create(self.prefs["backup_dir"], self.prefs.get("server_exe"),
                                self.prefs["backup_keep"], reason, self.log,
                                online=bool(self.monitor.processes))
        self.state["last_backup"] = time.time()
        self.state["last_backup_path"] = str(target)
        self._save_state()
        return f"Backup verified: {target.name}"

    def _restart(self):
        self._assert_monitor()
        if not self.monitor.processes:
            return self._start_server()
        self._backup("before-restart")
        self._stop_server()
        try:
            self._backup("after-stop")
        except Exception:
            self._start_server()
            raise
        return self._start_server()

    def _check_update(self, apply=False, scheduled=False):
        root = self.prefs["install_dir"]
        # Resolve the installed launcher before any update decisions, including
        # older preferences that contain only an install folder.
        self._assert_monitor()
        if not (Path(root) / "steamcmd/steamcmd.exe").is_file():
            installer.download_steamcmd(Path(root) / "steamcmd", self.log)
        current = steam.installed_build(root)
        try:
            latest = steam.latest_build(root, self.log)
        except Exception:
            self.state["update_status"] = "Update check failed; see Activity for details."
            raise
        self.state.update(installed_build=current, available_build=latest, last_check=time.time())
        if current is None:
            self.state["update_status"] = "Installed build unknown. Run Install / repair to establish the manifest."
        elif current == latest:
            self.state["update_status"] = "Up to date"
        elif not apply:
            self.state["update_status"] = f"Build {latest} available"
        else:
            self._assert_monitor()
            reason = ""
            if scheduled and not scheduler.in_window(self.prefs, time.time()):
                reason = "Waiting for the maintenance window"
            elif scheduled and self.prefs["wait_for_empty"] and self.monitor.processes:
                if not self.monitor.player_count_known:
                    reason = "Waiting: player activity is unknown"
                elif self.monitor.players:
                    reason = "Waiting for players to leave"
                elif self.monitor.empty_since is None or time.time() - self.monitor.empty_since < 60:
                    reason = "Waiting for 60 seconds with no players"
            if reason:
                self.state["update_status"] = f"Build {latest} available · {reason}"
                self.state["next_check_update"] = time.time() + 300
                return self.state["update_status"]
            self._apply_update(latest, scheduled)
        return self.state["update_status"]

    def _apply_update(self, expected, scheduled=False):
        self._assert_monitor()
        was_online = bool(self.monitor.processes)
        self._backup("before-update")  # Abort before stopping if a backup cannot be made.
        # Recheck after the backup, since players can join while files are copied.
        self._assert_monitor()
        if bool(self.monitor.processes) != was_online:
            raise RuntimeError("Server state changed during backup; update cancelled.")
        if scheduled and not scheduler.in_window(self.prefs, time.time()):
            self.state["update_status"] = "Update deferred: maintenance window ended during backup."
            self.state["next_check_update"] = time.time() + 300
            return
        if scheduled and was_online and self.prefs["wait_for_empty"]:
            if (not self.monitor.player_count_known or self.monitor.players or
                    self.monitor.empty_since is None or time.time() - self.monitor.empty_since < 60):
                self.state["update_status"] = "Update deferred: player activity changed during backup."
                self.state["next_check_update"] = time.time() + 300
                return
        if was_online:
            self._stop_server()
            try:
                self._backup("after-stop-before-update")
            except Exception:
                self._start_server()
                raise
        # Once Steam begins modifying files, leave the server stopped on failure.
        self._assert_monitor()
        if self.monitor.processes:
            raise RuntimeError("Server started externally; update cancelled.")
        steam.install(self.prefs["install_dir"], self.log)
        actual = steam.installed_build(self.prefs["install_dir"])
        if actual != expected:
            raise RuntimeError(f"Installed build {actual or 'unknown'} does not match expected build {expected}. Server remains stopped.")
        if not Path(self.prefs["server_exe"]).is_file():
            raise RuntimeError("Server executable is missing after update.")
        self.state.update(installed_build=actual, update_status="Up to date", last_update=time.time())
        if was_online:
            self._start_server()

    def _install(self):
        self._assert_monitor()
        if self.monitor.processes:
            raise RuntimeError("Stop the server before Install / repair, or use Apply update.")
        root = Path(self.prefs["install_dir"])
        existing = self.prefs.get("server_exe") or installer.find_server_exe(root)
        if existing:
            self.prefs["server_exe"] = existing
            self._assert_monitor()
            if self.monitor.processes:
                raise RuntimeError("Server is running. Stop it before repairing.")
            self._backup("before-repair")
        if not (root / "steamcmd/steamcmd.exe").is_file():
            installer.download_steamcmd(root / "steamcmd", self.log)
        steam.install(root, self.log)
        exe = installer.find_server_exe(root)
        if not exe:
            raise RuntimeError("SteamCMD finished, but the server executable is missing.")
        self.prefs["server_exe"] = exe
        settings.save(self.prefs)
        self.state["installed_build"] = steam.installed_build(root)
        self.events.put(("preferences", dict(self.prefs)))
        return "Server files installed. Save the game configuration before first launch."

    def _write_config(self):
        self._assert_monitor()
        if not self.prefs.get("server_exe"):
            detected = installer.find_server_exe(self.prefs["install_dir"])
            if detected:
                self.prefs["server_exe"] = detected
                self._assert_monitor()
        if self.monitor.processes:
            raise RuntimeError("Stop the server before writing its configuration.")
        installer.write_config(self.prefs, self.log)

    def _refresh_backups(self):
        self._backup_records = backups.list_backups(self.prefs["backup_dir"],
                                                   backups.profile_id(self.prefs.get("server_exe")))
