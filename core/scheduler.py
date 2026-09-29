"""Clock-injected scheduler. One due job is reserved at a time."""
import re
from datetime import datetime

JOBS = {"backup": ("auto_backup", "backup_hours", 3600),
        "check_update": ("auto_update", "update_minutes", 60)}


def validate(prefs):
    errors = []
    for key, lo, hi, label in (
        ("update_minutes", 5, 10080, "Update interval (minutes)"),
        ("backup_hours", 1, 720, "Backup interval (hours)"),
        ("backup_keep", 1, 365, "Backups to keep"),
    ):
        try:
            if not lo <= int(prefs[key]) <= hi:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            errors.append(f"{label} must be between {lo} and {hi}.")
    for key in ("window_start", "window_end"):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(prefs.get(key, ""))):
            errors.append("Maintenance times must use 24-hour HH:MM format.")
    if prefs.get("maintenance_window") and prefs.get("window_start") == prefs.get("window_end"):
        errors.append("Maintenance start and end must be different.")
    if not str(prefs.get("backup_dir", "")).strip():
        errors.append("Choose a backup folder.")
    return errors


def in_window(prefs, now):
    if not prefs.get("maintenance_window"):
        return True
    minute = datetime.fromtimestamp(now).strftime("%H:%M")
    start, end = prefs["window_start"], prefs["window_end"]
    return start <= minute < end if start < end else minute >= start or minute < end


def reconcile(prefs, state, now, previous=None):
    for job, (enabled, interval, unit) in JOBS.items():
        key = f"next_{job}"
        if not prefs[enabled]:
            state.pop(key, None)
        elif (not isinstance(state.get(key), (int, float)) or
              (previous and (previous[enabled] != prefs[enabled] or
                             previous[interval] != prefs[interval]))):
            state[key] = now + int(prefs[interval]) * unit


def due_job(prefs, state, now):
    for job, (enabled, _, _) in JOBS.items():
        if prefs[enabled] and now >= state.get(f"next_{job}", float("inf")):
            return job
    return None


def reserve(job, prefs, state, now):
    _, interval, unit = JOBS[job]
    state[f"next_{job}"] = now + int(prefs[interval]) * unit
