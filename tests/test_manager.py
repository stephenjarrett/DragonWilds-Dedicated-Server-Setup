import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from core import backups, installer, monitor, scheduler, settings, steam
from core.service import ManagerService


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"DRAGONWILDS_MANAGER_HOME": str(self.root / "manager")})
        self.environment.start()
        self.local_save = self.root / "local/SaveGames"
        self.local_config = self.root / "local/Config"
        self.patches = [patch.object(installer, "SAVEGAMES_DIR", self.local_save),
                        patch.object(installer, "CONFIG_WIN", self.local_config),
                        patch.object(installer, "CONFIG_FILE", self.local_config / "DedicatedServer.ini")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.environment.stop()
        self.temp.cleanup()


class SteamTests(TemporaryCase):
    def test_extracts_public_not_beta_build_with_banners(self):
        output = '''Steam Console Client\n"4019830" {
          "common" {"name" "A game with {braces}"}
          "depots" {"branches" {
            "beta" {"buildid" "987"}
            "public" {"buildid" "123" "timeupdated" "0"}
          }}}
          Unloading Steam API...OK'''
        self.assertEqual(steam.public_build(output), "123")

    def test_bad_metadata_never_means_up_to_date(self):
        for output in ('No connection', '"4019830" {', '"4019830" {"depots" {}}'):
            with self.subTest(output=output), self.assertRaises((RuntimeError, ValueError)):
                steam.public_build(output)

    def test_manifest_is_scoped_to_install(self):
        (self.root / "steamapps").mkdir()
        path = self.root / "steamapps/appmanifest_4019830.acf"
        path.write_text('"AppState" {"appid" "4019830" "buildid" "77"}')
        self.assertEqual(steam.installed_build(self.root), "77")
        self.assertIsNone(steam.installed_build(self.root / "other"))

    @patch("core.steam.command", return_value="ERROR! Download failed")
    def test_zero_exit_without_success_message_fails(self, command):
        with self.assertRaises(RuntimeError):
            steam.install(self.root, Mock())

    @patch("core.steam.run", side_effect=[(7, "self update"), (0, "done")])
    def test_retries_self_update_once(self, run):
        self.assertEqual(steam.command(self.root, ["+quit"], Mock()), "done")
        self.assertEqual(run.call_count, 2)

    def test_legacy_steam_library_manifest_must_match_folder(self):
        root = self.root / "steamapps/common/Dedicated Server"
        root.mkdir(parents=True)
        manifest = self.root / "steamapps/appmanifest_4019830.acf"
        manifest.write_text('"AppState" {"appid" "4019830" "buildid" "42" "installdir" "Dedicated Server"}')
        self.assertEqual(steam.installed_build(root), "42")
        self.assertIsNone(steam.installed_build(root.parent / "Other Server"))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.prefs = dict(settings.DEFAULTS, auto_update=True, auto_backup=True)

    def test_due_jobs_are_serial_and_reserved(self):
        state = {"next_backup": 50, "next_check_update": 50}
        self.assertEqual(scheduler.due_job(self.prefs, state, 100), "backup")
        scheduler.reserve("backup", self.prefs, state, 100)
        self.assertEqual(scheduler.due_job(self.prefs, state, 100), "check_update")
        scheduler.reserve("check_update", self.prefs, state, 100)
        self.assertIsNone(scheduler.due_job(self.prefs, state, 100))

    def test_existing_due_time_survives_restart(self):
        state = {"next_backup": 50, "next_check_update": 80}
        scheduler.reconcile(self.prefs, state, 200)
        self.assertEqual(state["next_backup"], 50)
        previous = dict(self.prefs)
        self.prefs["backup_hours"] = 2
        scheduler.reconcile(self.prefs, state, 200, previous)
        self.assertEqual(state["next_backup"], 7400)
        self.assertEqual(state["next_check_update"], 80)

    def test_disabled_jobs_do_not_run(self):
        self.prefs.update(auto_update=False, auto_backup=False)
        state = {"next_backup": 0, "next_check_update": 0}
        scheduler.reconcile(self.prefs, state, 100)
        self.assertEqual(state, {})

    def test_midnight_window(self):
        self.prefs.update(maintenance_window=True, window_start="23:00", window_end="02:00")
        for hour, expected in ((23, True), (0, True), (1, True), (2, False), (12, False)):
            self.assertEqual(scheduler.in_window(self.prefs, datetime(2026, 9, 28, hour).timestamp()), expected)

    def test_invalid_intervals_and_times(self):
        self.prefs.update(backup_hours=0, update_minutes=-1, backup_keep=0, window_start="25:00")
        self.assertEqual(len(scheduler.validate(self.prefs)), 4)


class BackupTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.local_save.mkdir(parents=True)
        (self.local_save / "world.sav").write_bytes(b"test world\x00\xff")
        self.destination = self.root / "backups"
        self.log = Mock()

    def make(self, keep=20):
        return backups.create(self.destination, "", keep, "test", self.log)

    def test_archive_contains_original_names_and_correct_hashes(self):
        result = self.make()
        with zipfile.ZipFile(result) as archive:
            content = archive.read("local/SaveGames/world.sav")
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(hashlib.sha256(content).hexdigest(), manifest["files"]["local/SaveGames/world.sav"])
        self.assertEqual(len(backups.list_backups(self.destination)), 1)
        self.assertFalse(list(self.destination.glob("*.partial")))

    def test_retention_preserves_unrelated_files_and_other_profiles(self):
        self.make()
        other = self.destination / "Dragonwilds-unrelated.zip"
        other.write_bytes(b"keep me")
        (self.destination / "notes.txt").write_text("keep me too")
        exe = self.root / "other/server.exe"
        exe.parent.mkdir()
        exe.touch()
        foreign = backups.create(self.destination, str(exe), 20, "other server", self.log)
        self.make(keep=1)
        self.assertTrue(other.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue((self.destination / "notes.txt").exists())
        self.assertEqual(len(backups.list_backups(self.destination, backups.profile_id(""))), 1)

    def test_source_containing_destination_is_refused(self):
        with self.assertRaises(ValueError):
            backups.create(self.local_save / "backups", "", 20, "test", self.log)

    def test_changed_source_never_publishes_or_prunes(self):
        existing = self.make()
        real_inventory = backups.inventory
        calls = 0

        def changing(roots):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.local_save / "world.sav").write_bytes(b"changed")
            return real_inventory(roots)

        with patch.object(backups, "inventory", side_effect=changing), self.assertRaises(RuntimeError):
            self.make(keep=1)
        self.assertTrue(existing.exists())
        self.assertEqual(len(list(self.destination.glob("*.zip"))), 1)
        self.assertFalse(list(self.destination.glob("*.partial")))


class ConfigurationTests(TemporaryCase):
    def test_preserves_unmanaged_values_comments_and_duplicates(self):
        path = installer.CONFIG_FILE
        path.parent.mkdir(parents=True)
        content = ('; comment\n[/Script/Dominion.DedicatedServerSettings]\nServerName=Before\n'
                   'AdminPlayerIds=abc\nAdminPlayerIds=def\n[Custom]\nValue=unchanged\n')
        path.write_text(content)
        prefs = dict(settings.DEFAULTS, owner_id="owner", server_name="After", world_name="World", admin_pass="a%b")
        installer.write_config(prefs, Mock())
        result = path.read_text()
        self.assertIn("; comment", result)
        self.assertIn("AdminPlayerIds=abc\nAdminPlayerIds=def", result)
        self.assertIn("[Custom]\nValue=unchanged", result)
        self.assertEqual(installer.read_config()["server_name"], "After")
        self.assertEqual(installer.read_config()["admin_pass"], "a%b")
        self.assertEqual(path.with_suffix(".ini.bak").read_text(), content)

    def test_invalid_port_and_newline_fail_before_any_write(self):
        prefs = dict(settings.DEFAULTS, owner_id="owner", server_name="name\nInjected=true", world_name="World", admin_pass="pass", port="65536")
        with self.assertRaises(ValueError):
            installer.write_config(prefs, Mock())
        self.assertFalse(installer.CONFIG_FILE.exists())

    @patch("core.installer.subprocess.run")
    def test_firewall_stdout_error_is_not_success(self, run):
        run.return_value = Mock(returncode=1, stdout="Access denied", stderr="")
        with self.assertRaisesRegex(RuntimeError, "Access denied"):
            installer.set_firewall(7777, Mock())

    def test_corrupt_settings_recover_defaults(self):
        settings.app_dir().mkdir()
        (settings.app_dir() / "settings.json").write_text("{broken")
        self.assertEqual(settings.load(), settings.DEFAULTS)
        settings.save(dict(settings.DEFAULTS, auto_backup=True))
        self.assertTrue(settings.load()["auto_backup"])


class MaintenanceTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.exe = self.root / "server/RSDragonwildsServer.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        (self.exe.parent / "steamcmd").mkdir()
        (self.exe.parent / "steamcmd/steamcmd.exe").touch()
        self.service = ManagerService(dict(settings.DEFAULTS, install_dir=str(self.exe.parent),
                                          server_exe=str(self.exe), backup_dir=str(self.root / "backups")))
        self.service._poll = Mock()
        self.service._backup = Mock()
        self.service._stop_server = Mock(side_effect=lambda: self.service.monitor.processes.clear())
        self.service._start_server = Mock()
        self.current = patch.object(steam, "installed_build", return_value="100")
        self.latest = patch.object(steam, "latest_build", return_value="200")
        self.install = patch.object(steam, "install")
        self.current_mock, self.latest_mock, self.install_mock = self.current.start(), self.latest.start(), self.install.start()

    def tearDown(self):
        self.current.stop()
        self.latest.stop()
        self.install.stop()
        for handler in self.service.logger.handlers[:]:
            handler.close()
            self.service.logger.removeHandler(handler)
        super().tearDown()

    def online(self):
        self.service.monitor.processes = [Mock()]
        self.service.monitor.player_count_known = True
        self.service.monitor.empty_since = time.time() - 120

    def test_no_update_does_not_back_up_stop_or_install(self):
        self.latest_mock.return_value = "100"
        self.online()
        self.service._check_update(apply=True, scheduled=True)
        self.service._backup.assert_not_called()
        self.service._stop_server.assert_not_called()
        self.install_mock.assert_not_called()

    def test_online_players_defer_update(self):
        self.online()
        self.service.monitor.players = {"Player"}
        self.service._check_update(apply=True, scheduled=True)
        self.install_mock.assert_not_called()
        self.assertIn("Waiting for players", self.service.state["update_status"])

    def test_unknown_players_defer_update(self):
        self.online()
        self.service.monitor.player_count_known = False
        self.service._check_update(apply=True, scheduled=True)
        self.service._stop_server.assert_not_called()
        self.assertIn("unknown", self.service.state["update_status"])

    def test_unknown_installed_build_never_triggers_install(self):
        self.current_mock.return_value = None
        self.service._check_update(apply=True, scheduled=True)
        self.install_mock.assert_not_called()

    def test_backup_failure_prevents_stop(self):
        self.online()
        self.service._backup.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            self.service._check_update(apply=True, scheduled=True)
        self.service._stop_server.assert_not_called()
        self.install_mock.assert_not_called()

    def test_failed_stop_prevents_install(self):
        self.online()
        self.service._stop_server.side_effect = RuntimeError("still running")
        with self.assertRaises(RuntimeError):
            self.service._check_update(apply=True, scheduled=True)
        self.install_mock.assert_not_called()

    def test_install_failure_leaves_server_stopped(self):
        self.online()
        self.install_mock.side_effect = RuntimeError("network failed")
        with self.assertRaises(RuntimeError):
            self.service._check_update(apply=True, scheduled=True)
        self.service._start_server.assert_not_called()

    def test_successful_update_restarts_only_previously_online_server(self):
        for online in (False, True):
            with self.subTest(online=online):
                self.service.monitor.processes = []
                if online:
                    self.online()
                self.service._start_server.reset_mock()
                self.current_mock.side_effect = ["100", "200"]
                self.service._check_update(apply=True, scheduled=True)
                self.assertEqual(self.service._start_server.call_count, int(online))
                self.assertEqual(self.service.state["update_status"], "Up to date")

    def test_backup_after_stop_failure_recovers_unchanged_server(self):
        self.online()
        self.service._backup.side_effect = [None, OSError("disk failure")]
        with self.assertRaises(OSError):
            self.service._check_update(apply=True, scheduled=True)
        self.service._start_server.assert_called_once()
        self.install_mock.assert_not_called()

    def test_operation_failure_releases_lock_and_retains_error(self):
        self.service.busy = True
        self.service._backup.side_effect = OSError("disk failure")
        self.service._execute("backup", scheduled=True)
        self.assertFalse(self.service.busy)
        self.assertEqual(self.service.state["last_result_level"], "ERROR")
        self.assertLessEqual(self.service.state["next_backup"], time.time() + 901)

    def test_overlapping_submissions_are_rejected(self):
        self.assertTrue(self.service.submit("backup"))
        self.assertFalse(self.service.submit("apply_update"))
        self.assertFalse(self.service.close())

    def test_manual_backup_does_not_enable_a_schedule(self):
        self.service._execute("backup", scheduled=False)
        self.assertNotIn("next_backup", self.service.state)

    def test_failed_metadata_check_clears_stale_up_to_date_status(self):
        self.service.state["update_status"] = "Up to date"
        self.latest_mock.side_effect = RuntimeError("network unavailable")
        with self.assertRaises(RuntimeError):
            self.service._check_update()
        self.assertIn("failed", self.service.state["update_status"])


class MonitorTests(TemporaryCase):
    def test_partial_log_lines_rotation_and_unknown_startup(self):
        exe = self.root / "RSDragonwildsServer.exe"
        log = self.root / "RSDragonwilds/Saved/Logs/RSDragonwilds.log"
        log.parent.mkdir(parents=True)
        process = Mock()
        process.create_time.return_value = time.time() - 100
        mon = monitor.ServerMonitor()
        mon.exe, mon.processes = str(exe), [process]
        log.write_bytes(b"Join succeeded: Alice\n")
        mon._read_players()
        self.assertFalse(mon.player_count_known)
        log.write_bytes(b"LogInit: Game Engine Initialized\nJoin succeeded: Alice\nJoin succeeded: Bo")
        mon._reset()
        mon._read_players()
        self.assertEqual(mon.players, {"Alice"})
        self.assertTrue(mon.player_count_known)
        with log.open("ab") as stream:
            stream.write(b"b\nPlayer Removed from session [session]-[Alice]\n")
        mon._read_players()
        self.assertEqual(mon.players, {"Bob"})
        log.write_bytes(b"rotated\n")
        mon._read_players()
        self.assertFalse(mon.player_count_known)
        self.assertEqual(mon.players, set())

    @patch("core.monitor.psutil.Process")
    def test_pid_reuse_is_not_the_same_process(self, process):
        process.return_value.create_time.return_value = 500
        self.assertIsNone(monitor.same_process({"pid": 10, "created": 100}))


if __name__ == "__main__":
    unittest.main()
