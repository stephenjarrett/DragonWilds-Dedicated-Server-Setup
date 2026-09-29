"""Entry point for the desktop manager and isolated shutdown helper."""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path

from core import monitor, settings


def main():
    parser = argparse.ArgumentParser(description="Dragonwilds Server Manager")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--data-dir", help="Separate manager preferences, schedule state and logs")
    parser.add_argument("--signal-stop", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.signal_stop is not None:
        return monitor.signal_console(args.signal_stop)
    if args.data_dir:
        os.environ["DRAGONWILDS_MANAGER_HOME"] = str(Path(args.data_dir).resolve())
    mutex = None
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        token = hashlib.sha256(str(settings.app_dir().resolve()).casefold().encode()).hexdigest()[:24]
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\DragonwildsManager-" + token)
        if kernel.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "The manager is already running for this profile.", "Dragonwilds Manager", 0)
            return 1
    from core.ui import DragonwildsManager
    app = DragonwildsManager(minimized=args.minimized)
    app.mainloop()
    if mutex:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
