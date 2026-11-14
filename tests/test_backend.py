import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fakes
from fakes import FakeMouse, make_fake_hid

import volt_backend as vb
from volt_backend import Config, DpiStage, LedSettings


def cfg(dpis=(400, 800, 1600, 3200, 6400), mode="Static", rgb=(0, 255, 0), bright=2, speed=3, poll=1000,
        enabled=(1, 1, 1, 1, 1)):
    return Config([DpiStage(bool(e), d) for e, d in zip(enabled, dpis)], LedSettings(mode, rgb, bright, speed), poll, True)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mouse = FakeMouse()
        vb.hid = make_fake_hid(self.mouse)
        self.logs = []
        self.link = vb.MouseLink(self.logs.append)
        self.link.open()

    def tearDown(self):
        self.link.close()

    def led(self, p):
        return list(self.mouse.mem[0x449 + 8 * p: 0x449 + 8 * p + 7])

    def test_handshake_and_codes(self):
        self.assertEqual(vb.handshake(self.link), 0)
        self.assertEqual(vb.dpi_to_code(800), (18, 0, 800))
        self.assertEqual(vb.dpi_to_code(500)[2], 489)
        self.assertEqual(vb.dpi_to_code(10000)[1], 1)

    def test_apply_writes_everything_and_verifies(self):
        res = vb.apply_config(self.link, cfg(), [0, 1, 2, 3, 4], True, self.tmp, refresh=False)
        self.assertEqual(res["mismatches"], [])
        self.assertTrue(res["changed"] and res["dpi_changed"])
        for p in range(5):
            self.assertEqual(self.led(p), [0, 255, 0, 0x01, 3, 0x02, 2])
            self.assertEqual([self.mouse.dpi_record(p, i)[1] for i in range(5)], [9, 18, 36, 72, 72])
            self.assertEqual(self.mouse.mem[0x32 + 2 * p], 1)
        self.assertEqual(self.mouse.mem[0x33], 2 * 0 + self.mouse.mem[0x33])
        self.assertEqual(self.mouse.violations, [])
        self.assertTrue(os.path.isfile(res["backup"]))

    def test_x2_to_x1_drop_is_written_in_two_steps_and_never_hangs(self):
        res = vb.apply_config(self.link, cfg(dpis=(800, 1600, 2400, 3200, 800)), [0], True, self.tmp, refresh=False)
        self.assertFalse(self.mouse.hung)
        self.assertEqual(res["mismatches"], [])
        rec5 = vb.dpi_rec_addr(0, 4)
        writes = [d for a, d in self.mouse.write_log if a == rec5]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][2] & 1, 1)
        self.assertEqual(writes[1][2] & 1, 0)
        m = FakeMouse()
        m.unlocked = True
        m.send_feature_report(vb.build_write(rec5, [1, 9, 0, 9, 0, 0]))
        self.assertTrue(m.hung)

    def test_unchanged_config_writes_nothing(self):
        vb.apply_config(self.link, cfg(), [0, 1, 2, 3, 4], True, self.tmp, refresh=False)
        n = len(self.mouse.write_log)
        res = vb.apply_config(self.link, cfg(), [0, 1, 2, 3, 4], True, self.tmp, refresh=False)
        self.assertFalse(res["changed"])
        self.assertFalse(res["dpi_changed"])
        self.assertEqual(len(self.mouse.write_log), n)

    def test_colour_only_change_is_not_a_dpi_change(self):
        vb.apply_config(self.link, cfg(), [0], True, self.tmp, refresh=False)
        res = vb.apply_config(self.link, cfg(rgb=(1, 2, 3)), [0], True, self.tmp, refresh=False)
        self.assertTrue(res["changed"])
        self.assertFalse(res["dpi_changed"])

    def test_colour_ignoring_mode_keeps_the_stored_colour(self):
        before = self.led(0)[:3]
        res = vb.apply_config(self.link, cfg(mode="Rainbow", rgb=(9, 9, 9)), [0], True, self.tmp, refresh=False)
        self.assertEqual(self.led(0)[:3], before)
        self.assertEqual(res["mismatches"], [])

    def test_all_stages_disabled_is_rejected(self):
        with self.assertRaises(ValueError):
            vb.apply_config(self.link, cfg(enabled=(0, 0, 0, 0, 0)), [0], True, self.tmp, refresh=False)
        self.assertEqual(self.mouse.write_log, [])

    def test_backup_restore_roundtrip(self):
        original = bytes(self.mouse.mem)
        res = vb.apply_config(self.link, cfg(dpis=(400, 400, 400, 400, 400)), [0, 1, 2, 3, 4], True, self.tmp, refresh=False)
        self.assertNotEqual(bytes(self.mouse.mem), original)
        n = vb.restore_backup(self.link, res["backup"])
        self.assertEqual(n, 5)
        self.assertEqual(bytes(self.mouse.mem[0x449:0x449 + 40]), original[0x449:0x449 + 40])
        for p in range(5):
            for i in range(5):
                self.assertEqual(self.mouse.dpi_record(p, i)[:3], original[vb.dpi_rec_addr(p, i):vb.dpi_rec_addr(p, i) + 3])

    def test_recover_led(self):
        vb.recover_led(self.link, [0, 1])
        self.assertEqual(self.led(0), [255, 255, 255, 0x01, 2, 0x02, 3])

    def test_session_always_relocks(self):
        with self.assertRaises(RuntimeError):
            with self.link.session():
                raise RuntimeError("boom")
        self.assertFalse(self.mouse.unlocked)

    def test_dry_run_sends_nothing(self):
        link = vb.MouseLink(self.logs.append, dry_run=True)
        link.open()
        res = vb.apply_config(link, cfg(), [0, 1, 2, 3, 4], True, self.tmp, refresh=False)
        self.assertEqual(self.mouse.write_log, [])
        self.assertEqual(vb.restart_mouse(link), 0)
        self.assertTrue(res["changed"])

    def test_restart_refused_without_windows_or_admin(self):
        if os.name == "nt":
            self.skipTest("Windows host")
        with self.assertRaises(vb.RestartError):
            vb.restart_mouse(self.link)

    def test_dump_and_diff(self):
        before = vb.dump_memory(self.link)
        vb.apply_config(self.link, cfg(), [0], True, self.tmp, refresh=False)
        diff = vb.diff_dumps(before, vb.dump_memory(self.link))
        self.assertTrue(diff)
        self.assertIn("0x", vb.format_diff(diff)[0])

    def test_settings_roundtrip(self):
        vb.save_settings({"auto_restart": True}, self.tmp)
        self.assertEqual(vb.load_settings(self.tmp), {"auto_restart": True})
        self.assertEqual(vb.load_settings(os.path.join(self.tmp, "nope")), {})

    def test_resource_path_source_and_frozen(self):
        p = vb.resource_path("assets/volt.ico")
        self.assertTrue(os.path.isfile(p), p)
        with mock.patch.object(sys, "_MEIPASS", self.tmp, create=True):
            self.assertEqual(vb.resource_path("assets/volt.ico"), os.path.join(self.tmp, "assets", "volt.ico"))

    def test_relaunch_as_admin_off_windows(self):
        if os.name != "nt":
            self.assertFalse(vb.relaunch_as_admin())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.mouse = FakeMouse()
        vb.hid = make_fake_hid(self.mouse)

    def run_cli(self, *argv):
        import volt
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = volt.main(list(argv))
        return rc, out.getvalue()

    def test_debug_probe_dump(self):
        rc, out = self.run_cli("--debug")
        self.assertEqual(rc, 0)
        self.assertIn("config interface", out)
        rc, out = self.run_cli("--probe")
        self.assertEqual(rc, 0)
        self.assertIn("Handshake OK", out)
        self.assertIn("DPI 5", out)
        rc, out = self.run_cli("--dump")
        self.assertEqual(rc, 0)
        self.assertIn("saved:", out)

    def test_probe_without_mouse_fails_cleanly(self):
        vb.hid = make_fake_hid(self.mouse, present=False)
        rc, out = self.run_cli("--probe")
        self.assertEqual(rc, 1)
        self.assertIn("PROBE FAILED", out)

    def test_cli_works_without_a_console(self):
        """--noconsole PyInstaller exe: sys.stdout/stderr/stdin are None."""
        appdata = tempfile.mkdtemp()
        code = ("import sys; sys.stdout = None; sys.stderr = None; sys.stdin = None\n"
                "sys.path.insert(0, %r)\nimport volt\nrc = volt.main(['--debug'])\n"
                "rc2 = volt.main(['--find-active'])\nsys.exit(rc * 10 + rc2)" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "APPDATA": appdata},
                           capture_output=True, text=True)
        self.assertEqual(r.stderr, "")
        self.assertTrue(os.path.isfile(os.path.join(appdata, "RedragonM711", "volt-cli.log")))
        with open(os.path.join(appdata, "RedragonM711", "volt-cli.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("hid", log.lower())


if __name__ == "__main__":
    unittest.main()
