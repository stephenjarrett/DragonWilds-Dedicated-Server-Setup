"""End-to-end tests use only temporary files and disposable helper processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil

from core import installer, monitor, settings, steam
from core.service import ManagerService


class IntegrationTests(unittest.TestCase):
    def test_scheduled_backup_survives_reopen_and_creates_a_real_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saves = root / "saves"
            saves.mkdir()
            (saves / "world.sav").write_bytes(b"A disposable test world")
            with patch.dict(os.environ, {"DRAGONWILDS_MANAGER_HOME": str(root / "manager")}), \
                 patch.object(installer, "SAVEGAMES_DIR", saves), \
                 patch.object(installer, "CONFIG_WIN", root / "config"):
                prefs = dict(settings.DEFAULTS, auto_backup=True, backup_dir=str(root / "backups"), install_dir=str(root / "server"))
                settings.atomic_json(settings.app_dir() / "state.json", {"next_backup": time.time() - 30})
                service = ManagerService(prefs)
                service.start()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if list((root / "backups").glob("*.zip")) and not service.busy:
                        break
                    time.sleep(0.05)
                self.assertTrue(service.close())
                service.thread.join(3)
                self.assertFalse(service.thread.is_alive())
                self.assertEqual(len(list((root / "backups").glob("*.zip"))), 1)
                saved = settings.read_json(settings.app_dir() / "state.json")
                self.assertGreater(saved["next_backup"], time.time())
                reopened = ManagerService(prefs)
                self.assertEqual(reopened.state["next_backup"], saved["next_backup"])
                for handler in reopened.logger.handlers[:]:
                    handler.close()
                    reopened.logger.removeHandler(handler)

    @unittest.skipUnless(os.name == "nt", "Windows console shutdown")
    def test_console_shutdown_stops_only_a_disposable_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "console_fixture.py"
            ready, stopped = root / "ready", root / "stopped"
            script.write_text(
                "import signal, sys, time\nfrom pathlib import Path\n"
                "def stop(*args):\n    Path(sys.argv[2]).write_text('clean')\n    sys.exit(0)\n"
                "signal.signal(signal.SIGINT, stop)\n"
                "Path(sys.argv[1]).write_text('ready')\n"
                "while True: time.sleep(0.1)\n")
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            process = subprocess.Popen([sys.executable, str(script), str(ready), str(stopped)],
                                       creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                tracked = psutil.Process(process.pid)
                mon = monitor.ServerMonitor()
                mon.processes = [tracked]
                monitor.stop(mon, [monitor.identity(tracked)], False, lambda *_: None, timeout=5)
                process.wait(timeout=5)
                self.assertEqual(stopped.read_text(), "clean")
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_streaming_process_and_timeout(self):
        logged = []
        code, output = steam.run(sys.executable, ["-c", "print('line one'); print('line two')"],
                                 lambda message, _: logged.append(message), timeout=10)
        self.assertEqual(code, 0)
        self.assertEqual(logged, ["line one", "line two"])
        with self.assertRaises(TimeoutError):
            steam.run(sys.executable, ["-c", "import time; time.sleep(30)"], lambda *_: None, timeout=0.4)


if __name__ == "__main__":
    unittest.main()
