"""UI logic tests: the real volt_ui code runs against a strict stub of customtkinter (no display needed)."""
import argparse
import os
import time
import unittest
from unittest import mock

import fakes
from fakes import FakeMouse, make_fake_hid

import volt_backend as vb
import volt_ui
from volt_ui import C


def settle(app, timeout=8.0):
    """Pump the UI queue until the background worker is done."""
    t0, quiet = time.time(), 0
    while time.time() - t0 < timeout:
        app._pump()
        app.run_idle()
        quiet = quiet + 1 if not app.busy else 0
        if quiet >= 3:
            return
        time.sleep(0.01)
    raise AssertionError("UI stayed busy")


class UITests(unittest.TestCase):
    def launch(self, dry_run=True, present=True, mouse=None, force_write=False):
        fakes.install()
        self.addCleanup(fakes.uninstall)
        fakes.MESSAGEBOX.calls.clear()
        vb.save_settings({})
        self.mouse = mouse or FakeMouse()
        vb.hid = make_fake_hid(self.mouse, present=present)
        args = argparse.Namespace(dry_run=dry_run, interface=None, force_write=force_write)
        self.assertEqual(volt_ui.run_gui(args), 0)
        app = fakes.CTk.LAST
        self.app = app
        app.run_idle()
        app._poll_presence()
        settle(app)
        settle(app)
        return app

    def pill_visible(self, app):
        return app.state_pill.grid_state == "on"

    def test_dry_run_flow(self):
        app = self.launch()
        self.assertTrue(app.connected)
        self.assertEqual(app.conn_text.kw["text"], "Dry run")
        self.assertFalse(self.pill_visible(app))
        self.assertEqual(app.apply_btn.kw["fg_color"], C.SURFACE2)

        app._on_slider(0, 1250)
        self.assertEqual(app.stage_var[0].get(), "1300")
        self.assertEqual(app.stage_eff[0].kw["text"], "stored: 1289 DPI")
        self.assertTrue(self.pill_visible(app))
        self.assertEqual(app.state_text.kw["text"], "Unsaved changes")
        self.assertEqual(app.apply_btn.kw["fg_color"], C.ACCENT)

        app._on_apply()
        settle(app)
        self.assertEqual(fakes.MESSAGEBOX.calls, [])
        self.assertFalse(self.pill_visible(app))
        self.assertIn("Dry run", app.toast_msg.kw["text"])
        self.assertEqual(self.mouse.write_log, [])
        self.assertTrue(any(f.endswith(".json") for f in os.listdir(vb.BACKUP_DIR)))

    def test_mode_picker_hides_colour_for_colourless_modes(self):
        app = self.launch()
        for name, (_a, _b, uses) in vb.MODES.items():
            app._select_mode(name)
            self.assertEqual(app.col_body.grid_state, "on" if uses else "removed", name)
            self.assertEqual(app.col_note.grid_state, "removed" if uses else "on", name)
        app._select_mode("Static")
        self.assertFalse(self.pill_visible(app))
        app._select_mode("Wave")
        self.assertTrue(self.pill_visible(app))
        app._set_color("#123456")
        app._select_mode("Static")
        self.assertTrue(self.pill_visible(app))

    def test_colour_entry_and_swatches(self):
        app = self.launch()
        app.hex_var.set("00ff88")
        app._on_hex_key()
        self.assertEqual(app._color, "#00ff88")
        self.assertEqual(app.preview_swatch.kw["fg_color"], "#00ff88")
        app._set_color("#ff2d2d")
        self.assertEqual(app.swatch_btns["#ff2d2d"].kw["border_color"], C.TEXT)
        app.hex_var.set("zzz")
        app._on_hex()
        self.assertEqual(app.hex_var.get(), "#ff2d2d")
        for name in vb.MODES:
            self.assertEqual(sum(1 for it in app.mode_tiles[name][1].items if it[0] == "oval"), 12)

    def test_cannot_disable_the_last_stage(self):
        app = self.launch()
        for i in range(4):
            app.stage_enabled[i].set(False)
        app.stage_enabled[4].set(False)
        self.assertTrue(app.stage_enabled[4].get())
        self.assertIn("At least one", app.toast_msg.kw["text"])
        self.assertEqual(app.stage_slider[0].kw["state"], "disabled")

    def test_invalid_dpi_text_is_reported_and_blocks_apply(self):
        app = self.launch()
        app.stage_var[0].set("abc")
        app._on_apply()
        self.assertEqual(fakes.MESSAGEBOX.calls[-1][0], "error")
        self.assertFalse(app.busy)

    def test_pages_ladder_and_misc(self):
        app = self.launch()
        for key, title, _s in volt_ui.PAGES:
            app._show_page(key)
            self.assertEqual(app.page_title.kw["text"], title)
            self.assertIn("tkraise", app.page_frames[key].calls)
        app._schedule_ladder()
        app.run_idle()
        self.assertGreater(len(app.ladder.items), 10)
        app.autorestart_var.set(True)
        app._on_autorestart_toggle()
        self.assertTrue(vb.load_settings()["auto_restart"])
        app._on_dump()
        settle(app)
        self.assertIn("memory dump", app.dbg_text.text)
        app._copy_log()
        self.assertIn("memory dump", app.clip)
        app._clear_log()
        self.assertEqual(app.dbg_text.text, "")

    def test_icon_and_missing_assets(self):
        with mock.patch.object(volt_ui, "IS_WINDOWS", True), mock.patch.object(volt_ui, "is_admin", return_value=False):
            app = self.launch()
            self.assertTrue(str(app.icon).endswith("volt.ico"))
            self.assertEqual(app.banner.grid_state, "on")
            with mock.patch.object(volt_ui, "relaunch_as_admin", return_value=True):
                app._on_relaunch_admin()
            self.assertTrue(app.destroyed)
        with mock.patch.object(volt_ui, "resource_path", lambda r: "/nonexistent/" + r):
            app = self.launch()
            self.assertIsNotNone(app)
        app = self.launch()
        self.assertEqual(app.banner.grid_state, "removed")

    def test_real_flow_load_save_restart(self):
        app = self.launch(dry_run=False)
        self.assertEqual(app.conn_text.kw["text"], "Connected")
        self.assertEqual([v.get() for v in app.stage_var], ["800", "1600", "2400", "3200", "10000"])
        self.assertEqual((app.mode, app._color, app.bright.get(), app.speed.get(), app.poll_hz),
                         ("Static", "#ff0000", 3, 2, 500))
        self.assertFalse(self.pill_visible(app))

        app._set_color("#00ff00")
        app._on_apply()
        settle(app)
        self.assertEqual(self.mouse.mem[0x449:0x44C], bytes([0, 255, 0]))
        self.assertTrue(app.pending_reload)
        self.assertEqual(app.state_text.kw["text"], "Saved \u00B7 not loaded yet")
        self.assertEqual(app.restart_btn.kw["border_color"], C.ACCENT)
        self.assertEqual(app.toast_msg.kw["text"], "Saved. Restart the mouse to load it.")
        self.assertEqual(fakes.MESSAGEBOX.calls, [])

        app._on_restart_now()
        settle(app)
        self.assertEqual(fakes.MESSAGEBOX.calls[-1][0], "error")
        self.assertTrue(app.pending_reload)

        def good_restart(link):
            link.close()
            link.open()
            return vb.handshake(link)
        with mock.patch.object(volt_ui, "restart_mouse", good_restart):
            app._on_restart_now()
            settle(app)
            self.assertFalse(app.pending_reload)
            self.assertFalse(self.pill_visible(app))
            self.assertEqual(app.toast_msg.kw["text"], "Mouse restarted & reloaded")

            app._on_slider(4, 800)
            app._on_apply()
            settle(app)
            self.assertFalse(self.mouse.hung)
            self.assertEqual(app.toast_msg.kw["text"], "Applied & Reloaded Live!")
            self.assertEqual(self.mouse.dpi_record(0, 4)[1], 18)
            self.assertFalse(app.pending_reload)

            app.autorestart_var.set(True)
            app._set_poll(1000)
            app._on_apply()
            settle(app)
            self.assertEqual(self.mouse.mem[0x32], 1)
            self.assertEqual(app.toast_msg.kw["text"], "Applied & Reloaded Live!")

    def test_failed_apply_after_a_write_triggers_recovery_restart(self):
        app = self.launch(dry_run=False)
        calls = []

        def restart(link):
            calls.append(1)
            return 0
        orig = self.mouse.send_feature_report
        state = {"writes": 0}

        def flaky(buf):
            if bytes(buf)[1] == 0xF3:
                state["writes"] += 1
                if state["writes"] >= 3:
                    raise IOError("usb glitch")
            return orig(buf)
        self.mouse.send_feature_report = flaky
        app._set_color("#0000ff")
        with mock.patch.object(volt_ui, "restart_mouse", restart):
            app._on_apply()
            settle(app)
        self.assertEqual(calls, [1])
        self.assertEqual(fakes.MESSAGEBOX.calls[-1][0], "error")
        self.assertIn("restarted to recover", fakes.MESSAGEBOX.calls[-1][1])

    def test_handshake_failure_locks_writes(self):
        m = FakeMouse()
        m.mem[0x2C] = 9
        app = self.launch(dry_run=False, mouse=m)
        self.assertEqual(app.conn_text.kw["text"], "Handshake failed")
        app._set_color("#0000ff")
        app._on_apply()
        self.assertEqual(fakes.MESSAGEBOX.calls[-1][0], "warning")
        self.assertEqual(m.write_log, [])

    def test_no_mouse(self):
        app = self.launch(dry_run=False, present=False)
        self.assertFalse(app.connected)
        self.assertEqual(app.conn_text.kw["text"], "Not connected")
        app._on_apply()
        self.assertEqual(app.toast_msg.kw["text"], "Mouse not connected.")

    def test_restore_backup_flow(self):
        app = self.launch(dry_run=False)
        app._set_color("#0000ff")
        app._on_apply()
        settle(app)
        backup = sorted(f for f in os.listdir(vb.BACKUP_DIR) if f.startswith("backup-"))[-1]
        fakes.FILEDIALOG.result = os.path.join(vb.BACKUP_DIR, backup)
        app._on_restore()
        settle(app)
        settle(app)
        self.assertEqual(self.mouse.mem[0x449:0x44C], bytes([255, 0, 0]))
        self.assertTrue(app.pending_reload)
        self.assertEqual(app._color, "#ff0000")


if __name__ == "__main__":
    unittest.main()
