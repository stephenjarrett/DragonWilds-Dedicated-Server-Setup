# Dragonwilds Server Manager 3

A Windows desktop manager for RuneScape: Dragonwilds dedicated servers, built on [Obietek's original project](https://github.com/Obietek/DragonWilds-Dedicated-Server-Setup).

## What's new

- Modern charcoal interface with rounded controls, blue-violet accents, compact icon navigation, a connection screen and a focused server dashboard.
- Overview, Server settings, Automation, Backups and Activity pages.
- Steam public-build checks; automatic installation only when the build changes.
- Automatic updates wait for 60 seconds with no players by default. Unknown activity defers maintenance.
- Optional daily maintenance window, including windows spanning midnight.
- Scheduled ZIP backups, integrity verification, SHA-256 inventories and retention per installation.
- Required backups before updates, repairs and restarts, plus a stopped snapshot before updating an online server.
- Serialized operations, streaming SteamCMD output, timeouts, retries, persistent schedules and rotating logs.
- Installation-specific process tracking, PID reuse protection, shutdown requests and an optional forced-stop fallback.
- Configuration editing preserves comments, game-managed admin lists and unrelated settings, with `.ini.bak` copies.

Automatic updates, scheduled backups and forced stops are **off by default**. Opening the manager does not install, launch, update or stop a server. This version does not expose the old destructive Delete Server Files action.

## Run or build

Use Python 3.12 or later:

```powershell
python -m pip install -r requirements.txt
python build.py
```

Open **dist/DragonwildsManager.exe**. The executable at the repository root is the original upstream binary; the new build is in **dist**. Run from source with `python main.py`.

Administrator rights are only needed for the explicit Configure firewall action or access to an elevated server process.

## Connect an existing server without reinstalling

1. In Server settings, browse to the existing `RSDragonwildsServer.exe` launcher, not the executable inside `Binaries/Win64`.
2. The install folder follows the launcher's folder and existing configuration is loaded into the form. Use Save changes to store manager preferences.
3. Saving preferences does not rewrite game configuration. Do not use Install / repair just to connect an existing installation.
4. Choose a backup folder outside the game's Saved folders and create a manual backup.
5. Enable the desired schedules in Automation, then save settings.

Use one manager per server. An already-running server is discovered without being restarted. A server launched elsewhere is not assumed to own an isolated console: if safe shutdown cannot be requested, maintenance stops with an explanation. At a convenient maintenance time, stop it using its existing controls and start it through this manager for managed console shutdown. Forced stops are optional and may lose unsaved progress.

For a new installation: select a folder, save settings, run Install / repair, fill in the world configuration, save settings, then Write game config before first launch. Configure the firewall explicitly if needed; router forwarding is separate.

## Automation

| Setting | Default |
| --- | --- |
| Automatic update checks and installation | Off |
| Update interval | 60 minutes |
| Wait for an empty server | On; 60 seconds |
| Daily maintenance window | Off; optional 03:00–05:00 local time |
| Scheduled backups | Off |
| Backup interval | 6 hours |
| Archives retained per installation | 20 |
| Forced-stop fallback | Off |

Schedules run while the manager is open and Windows is awake, including while minimized. Closing it pauses automation and leaves the server running. This is a desktop scheduler, not a Windows service. Use `DragonwildsManager.exe --minimized` for a minimized launch; a shortcut with that argument can be placed in your Windows Startup folder.

Next-run times survive app restarts. A missed job runs once after reopening or waking; missed intervals are not replayed as a burst. Failed jobs retry within 15 minutes. Deferred updates check again in 5 minutes. Only one operation runs at a time.

Checks use SteamCMD `app_info_update` and `app_info_print`, comparing the public branch build with the installation's `appmanifest_4019830.acf`. Legacy `steamapps/common` layouts are supported when the manifest names the matching folder. Unknown metadata is never reported as up to date; an unknown installed build never triggers automatic installation. SteamCMD is downloaded into the installation's steamcmd subfolder when needed.

Updates preserve the previous running state. Backup or shutdown failure cancels maintenance. If the stopped snapshot fails before Steam modifies files, the unchanged server is restarted. Once installation begins, an install or build-verification failure leaves the server stopped for investigation. A started process is reported separately from world readiness.

Player activity is inferred from startup, join and leave events in the server log. Missing, stale, truncated or unreadable startup information defers automatic updates under the empty-server policy. This is conservative log-based detection, not a game-supported player-count API. A small race remains between the final activity check and shutdown.

## Backups and recovery

Snapshots include existing save/configuration directories under the chosen installation, plus the current Windows user's Dragonwilds save/configuration folders. Each manifest maps archive paths to original source paths and records hashes, time, reason and whether the server was online.

Archives are written as `.partial`, checked for source changes and ZIP integrity, then published as `.zip`. Failed or changing snapshots never become completed archives and never trigger retention. Retention applies only to completed, manager-marked archives for the same installation. Other files and other installations' archives remain untouched. Moving an installation changes its retention identity.

Live backups capture files already written to disk; they cannot capture unsaved in-memory progress or guarantee a transaction across game saves. Maintenance takes another snapshot after exit. Keep additional copies on another disk. Archives contain configuration passwords and should be stored privately.

To restore: stop the server, back up its current state, inspect the archive's `manifest.json`, and copy the desired original saves to their corresponding source folders. Preserve filenames and secure another copy before resolving conflicting newer saves. Start and verify the world. The manager does not perform destructive automatic restoration.

Jagex documents game configuration and save paths in its [dedicated server guide](https://runescapedragonwilds.help.jagex.com/hc/en-gb/articles/45365343055249-Dedicated-Servers-How-to-Guide).

## Files

- Preferences: `%APPDATA%/DragonwildsManager/settings.json`
- Schedule state: `%APPDATA%/DragonwildsManager/state.json`
- Rotating logs: `%APPDATA%/DragonwildsManager/manager.log`
- Default backups: `%USERPROFILE%/DragonwildsBackups`
- New executable: `dist/DragonwildsManager.exe`

`--data-dir PATH` isolates manager preferences, schedule state and logs. It does **not** sandbox selected server or game-save paths. Tests explicitly replace game paths with temporary fixtures.

## Development and verification

```powershell
python -m unittest discover -s tests -v
python build.py
```

Tests cover schedule persistence, midnight windows, occupied/unknown-player deferral, maintenance failures, archive integrity and retention, configuration preservation, streaming-process timeouts, real Windows console shutdown using a disposable helper, and UI settings/control state. They do not install, modify or stop a real Dragonwilds server. Release CI runs tests before building.

`core/ui.py` contains the interface; `core/service.py` serializes maintenance. Scheduling, Steam operations, monitoring and backups are separate modules.

## Releases and signing

GitHub Actions builds Windows artifacts on manual dispatch or a `v*` tag. Pushing a version tag publishes a release; local builds do not publish anything.

`build.py` supports `SIGNTOOL_PATH`, `CODESIGN_CERT_SHA1`, `CODESIGN_PFX_PATH`, `CODESIGN_PFX_PASSWORD` and `CODESIGN_TIMESTAMP_URL` for optional signing. Without a configured certificate, the executable is unsigned.
