"""
core/settings.py
Persists app preferences to %APPDATA%/DragonwildsManager/settings.json
"""

import json
import os
import tempfile
from pathlib import Path

_APP_DIR  = Path(os.getenv("APPDATA", ".")) / "DragonwildsManager"

DEFAULTS = {
    "install_dir":  str(Path.home() / "DragonWildsServer"),
    "server_exe":   "",          # overridden once found/chosen
    "owner_id":     "",
    "server_name":  "",
    "world_name":   "",
    "admin_pass":   "",
    "world_pass":   "",
    "port":         "7777",
    "window_x":     -1,
    "window_y":     -1,
    "auto_update": False, "update_minutes": 60, "wait_for_empty": True,
    "maintenance_window": False, "window_start": "03:00", "window_end": "05:00",
    "auto_backup": False, "backup_hours": 6, "backup_keep": 20,
    "backup_dir": str(Path.home() / "DragonwildsBackups"),
    "allow_force_stop": False,
}


def load() -> dict:
    saved = read_json(app_dir() / "settings.json")
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = saved.get(key, default)
        if type(value) is type(default):
            result[key] = value
    return result


def save(data: dict) -> None:
    atomic_json(app_dir() / "settings.json", data)


def app_dir() -> Path:
    return Path(os.environ.get("DRAGONWILDS_MANAGER_HOME") or str(_APP_DIR))


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
