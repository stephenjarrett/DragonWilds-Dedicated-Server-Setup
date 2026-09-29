"""Verified ZIP snapshots, atomic publication, and scoped retention."""
import hashlib
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from core import installer

MARKER = "dragonwilds-manager-backup-v1"


def sources(exe):
    result = []
    candidates = [("local/SaveGames", installer.SAVEGAMES_DIR),
                  ("local/Config", installer.CONFIG_WIN)]
    if exe:
        base = Path(exe).resolve().parent
        candidates.extend([
            ("server/SaveGames", base / "RSDragonwilds/Saved/SaveGames"),
            ("server/Config", base / "RSDragonwilds/Saved/Config/WindowsServer"),
            ("alternate/SaveGames", base / "Saved/SaveGames"),
            ("alternate/Config", base / "Saved/Config/WindowsServer"),
        ])
    seen = set()
    for label, path in candidates:
        path = path.resolve()
        if path.is_dir() and path not in seen:
            seen.add(path)
            result.append((label, path))
    return result


def profile_id(exe):
    identity = str(Path(exe).resolve()).casefold() if exe else "local"
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def inventory(roots):
    files = {}
    for label, root in roots:
        for path in sorted(root.rglob("*")):
            # Do not traverse outside a source through symbolic links/junctions.
            if not path.resolve().is_relative_to(root):
                raise RuntimeError(f"Backup source links outside its folder: {path}")
            if path.is_file():
                stat = path.stat()
                files[f"{label}/{path.relative_to(root).as_posix()}"] = (
                    path, stat.st_size, stat.st_mtime_ns)
    return files


def create(destination, exe, keep, reason, log, online=False):
    destination = Path(destination).expanduser().resolve()
    roots = sources(exe)
    for _, root in roots:
        if destination == root or destination.is_relative_to(root):
            raise ValueError("Choose a backup folder outside the game's Saved folders.")
    files = inventory(roots)
    if not files:
        raise RuntimeError("No save or configuration files were found to back up.")
    if int(keep) < 1:
        raise ValueError("At least one backup must be retained.")
    destination.mkdir(parents=True, exist_ok=True)
    profile = profile_id(exe)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = destination / f"Dragonwilds-{profile}-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    temp = target.with_suffix(".partial")
    manifest = {"format": MARKER, "profile": profile, "reason": reason,
                "created": datetime.now(timezone.utc).isoformat(), "live_snapshot": online,
                "sources": {label: str(path) for label, path in roots}, "files": {}}
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for name, (path, _, _) in files.items():
                digest = hashlib.sha256()
                with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        output.write(chunk)
                manifest["files"][name] = digest.hexdigest()
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        if inventory(roots) != files:
            raise RuntimeError("Save files changed during backup. Snapshot discarded; retry shortly.")
        with zipfile.ZipFile(temp) as archive:
            if archive.testzip():
                raise RuntimeError("Backup archive integrity verification failed.")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    log(f"Verified backup: {target}", "OK")
    if online:
        log("Live snapshot contains files already saved to disk; in-memory progress is not included.", "INFO")
    prune(destination, profile, int(keep), log)
    return target


def list_backups(destination, profile=None):
    records = []
    root = Path(destination).expanduser().resolve()
    if not root.is_dir():
        return records
    for path in root.glob("Dragonwilds-*.zip"):
        if path.is_symlink() or path.resolve().parent != root:
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                metadata = archive.getinfo("manifest.json")
                if metadata.file_size > 8 * 1024 * 1024:
                    continue
                info = json.loads(archive.read(metadata))
            if info.get("format") == MARKER and (not profile or info.get("profile") == profile):
                datetime.fromisoformat(info["created"])
                records.append({"path": str(path), "created": info["created"],
                                "reason": info.get("reason", ""), "size": path.stat().st_size,
                                "live": info.get("live_snapshot", False)})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile):
            continue
    return sorted(records, key=lambda item: (item["created"], item["path"]), reverse=True)


def prune(destination, profile, keep, log):
    # Only completed archives with our marker, for this installation, are eligible.
    for record in list_backups(destination, profile)[keep:]:
        try:
            Path(record["path"]).unlink()
            log(f"Retention removed {Path(record['path']).name}", "INFO")
        except OSError as exc:
            log(f"Could not remove an old backup: {exc}", "WARN")
