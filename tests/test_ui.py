"""Widget tests with the service mocked; no live server actions."""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import settings


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        from core.ui import DragonwildsManager
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DRAGONWILDS_MANAGER_HOME": self.temp.name})
        self.env.start()
        self.service_patch = patch("core.ui.ManagerService")
        self.service_class = self.service_patch.start()
        self.app = DragonwildsManager(show_window=False)

    def tearDown(self):
        self.app.destroy()
        self.service_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_automation_defaults_are_safe_and_editable(self):
        self.assertFalse(self.app.variables["auto_update"].get())
        self.assertFalse(self.app.variables["auto_backup"].get())
        self.assertTrue(self.app.variables["wait_for_empty"].get())
        self.assertFalse(self.app.variables["allow_force_stop"].get())
        self.app.variables["backup_hours"].set("12")
        self.app.variables["auto_backup"].set(True)
        self.app._save()
        submitted = self.service_class.return_value.submit.call_args.args
        self.assertEqual(submitted[0], "save_settings")
        self.assertEqual(submitted[1]["backup_hours"], 12)
        self.assertTrue(submitted[1]["auto_backup"])

    def test_all_pages_render_and_busy_state_disables_actions(self):
        for name in self.app.pages:
            self.app._show(name)
            self.app.update_idletasks()
            self.assertEqual(self.app.current_page, name)
        self.app._set_busy(True)
        self.assertTrue(all(str(button.cget("state")) == "disabled" for button in self.app.action_buttons))
        self.app._set_busy(False)
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")

    def test_invalid_schedule_does_not_submit(self):
        self.app.variables["update_minutes"].set("0")
        with patch("core.ui.messagebox.showerror") as error:
            self.app._save()
            error.assert_called_once()
        self.service_class.return_value.submit.assert_not_called()

    def test_overview_switches_between_connection_and_server_views(self):
        data = {"prefs": dict(settings.DEFAULTS), "state": {}, "busy": False, "online": False,
                "pids": [], "started": None, "players": [], "players_known": False,
                "monitor_error": "", "backups": []}
        self.app._status(data)
        self.assertEqual(self.app.empty_section.winfo_manager(), "pack")
        self.assertEqual(self.app.server_section.winfo_manager(), "")
        data["prefs"]["server_exe"] = "C:/test/RSDragonwildsServer.exe"
        data["prefs"]["server_name"] = "Test world"
        self.app._status(data)
        self.assertEqual(self.app.empty_section.winfo_manager(), "")
        self.assertEqual(self.app.server_section.winfo_manager(), "pack")
        self.assertEqual(self.app.server_title.cget("text"), "Test world")
        self.service_class.return_value.submit.assert_not_called()

    def test_unchanged_status_ticks_do_not_repack_or_redraw_controls(self):
        data = {"prefs": dict(settings.DEFAULTS), "state": {}, "busy": False, "online": False,
                "pids": [], "started": None, "players": [], "players_known": False,
                "monitor_error": "", "backups": []}
        self.app._status(data)
        with patch.object(self.app.empty_section, "pack") as pack, \
             patch.object(self.app.server_section, "pack_forget") as forget, \
             patch.object(self.app.start_button, "configure") as configure, \
             patch.object(self.app.server_title, "configure") as title:
            for _ in range(10):
                self.app._status(data)
            pack.assert_not_called()
            forget.assert_not_called()
            configure.assert_not_called()
            title.assert_not_called()


if __name__ == "__main__":
    unittest.main()
