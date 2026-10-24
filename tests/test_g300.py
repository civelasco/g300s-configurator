"""Tests against a simulated mouse - no hardware needed.  Run:  python3 -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import g300  # noqa: E402
import g300_gui  # noqa: E402

g300.time.sleep = lambda s: None  # skip the mouse's flash-save delay


def fake_profile(rid, led):
    """Red/green/blue LED (+ unknown bits set), 1000 Hz, DPI 500,[1000],1500,2500."""
    raw = bytes([rid, led | 0xF8, 0x00, 2, 4 | 0x80, 6, 10, 0x21])
    buttons = [(1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0), (5, 0, 0),
               (0x0A, 0, 0), (0x0B, 0, 0), (0x0D, 0, 0), (0, 0x08, 0x06)]
    return raw + bytes(x for b in buttons for x in b)


class FakeDevice:
    def __init__(self):
        self.store = {0xF0: bytes([0xF0, 0x12, 0, 0])}
        for rid, led in zip(g300.REPORT_PROFILES, (1, 2, 4)):
            self.store[rid] = fake_profile(rid, led)
        self.writes = []

    def get_feature_report(self, rid, size):
        return list(self.store[rid])

    def send_feature_report(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data[0] != g300.REPORT_ACTIVE:
            self.store[data[0]] = data
        return len(data)

    def close(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        g300.BACKUP_DIR = self.tmp.name
        self.dev = FakeDevice()
        g300.Mouse.open = classmethod(lambda cls: cls(self.dev, {}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        with redirect_stdout(StringIO()):
            g300.main(list(args))


class ProfileTests(Base):
    def test_round_trip_and_decoding(self):
        raw = fake_profile(0xF3, 1)
        p = g300.Profile(raw)
        self.assertEqual(p.to_bytes(), raw)
        self.assertEqual(p.dpis, [500, 1000, 1500, 2500])
        self.assertEqual(p.default_dpi, 1)
        self.assertEqual((p.color, p.rate), ("red", 1000))
        self.assertEqual(p.buttons[8].describe(), "key:cmd+c")

    def test_bindings(self):
        self.assertEqual(g300.Binding.parse("key:ctrl+shift+tab").to_bytes(), bytes([0, 3, 0x2B]))
        self.assertEqual(g300.Binding.parse("Cmd+Option+Escape").to_bytes(), bytes([0, 0x0C, 0x29]))
        self.assertEqual(g300.Binding.parse("dpi-cycle").to_bytes(), bytes([0x0C, 0, 0]))
        for text in ["key:cmd+c", "key:shift+cmd+4", "key:ctrl", "key:ctrl+up"]:
            self.assertEqual(g300.Binding.parse(text).describe(), text)
        for bad in ["key:hyper+a", "frobnicate"]:
            with self.assertRaises(ValueError):
                g300.Binding.parse(bad)

    def test_invalid_dpi(self):
        p = g300.Profile(fake_profile(0xF3, 1))
        with self.assertRaises(ValueError):
            p.dpis = [300, 1000, 1500, 2500]


class CliTests(Base):
    def test_set_preserves_unknown_bits(self):
        self.run_cli("set", "-p", "2", "--dpi", "250,750,1250,2000", "--default-dpi", "3",
                     "--rate", "500", "--color", "cyan")
        q = g300.Profile(self.dev.store[0xF4])
        self.assertEqual(q.dpis, [250, 750, 1250, 2000])
        self.assertEqual((q.default_dpi, q.rate, q.color), (2, 500, "cyan"))
        self.assertEqual(q.led_byte & 0xF8, 0xF8)
        self.assertEqual(q.unknown2, 0x21)

    def test_bind_dry_run_activate(self):
        self.run_cli("bind", "-p", "1", "G6", "key:cmd+shift+4")
        self.assertEqual(self.dev.store[0xF3][23:26], bytes([0, 0x0A, 0x21]))
        n = len(self.dev.writes)
        self.run_cli("bind", "-p", "1", "G7", "disabled", "-n")
        self.assertEqual(len(self.dev.writes), n)
        self.run_cli("activate", "3")
        self.assertEqual(self.dev.writes[-1], bytes([0xF0, 0xA0, 0, 0]))

    def test_backup_reset_restore(self):
        path = os.path.join(self.tmp.name, "b.json")
        before = {rid: self.dev.store[rid] for rid in g300.REPORT_PROFILES}
        self.run_cli("backup", path)
        self.run_cli("reset")
        self.run_cli("restore", path)
        self.assertEqual({rid: self.dev.store[rid] for rid in g300.REPORT_PROFILES}, before)
        self.assertTrue(any(f.startswith("g300-") for f in os.listdir(self.tmp.name)), "auto-backup written")


class AppTests(Base):
    def test_save_only_touches_changed_profiles(self):
        ctl = g300_gui.Controller(demo=True)
        state = ctl.state()
        dev = ctl.mouse.dev
        blank_before = dev.store[0xF3]  # the demo mouse has empty profiles 1 and 2
        state["profiles"][2]["color"] = "green"
        result = ctl.save(state["profiles"])
        self.assertEqual(result["changed"], [3])
        self.assertEqual(dev.store[0xF3], blank_before)
        self.assertEqual(g300.Profile(dev.store[0xF5]).color, "green")

    def test_preset_into_blank_profile(self):
        ctl = g300_gui.Controller(demo=True)
        state = ctl.state()
        state["profiles"][1] = {"color": "white", "rate": 1000, "dpis": [500, 1000, 1500, 2500], "default_dpi": 1,
                                "buttons": ["left", "right", "middle", "back", "forward",
                                            "dpi-down", "dpi-up", "profile-cycle", "dpi-shift"]}
        self.assertEqual(ctl.save(state["profiles"])["not_kept"], [])
        self.assertEqual(ctl.state()["profiles"][1], state["profiles"][1])

    def test_bad_shortcut_is_rejected(self):
        ctl = g300_gui.Controller(demo=True)
        state = ctl.state()
        state["profiles"][2]["buttons"][6] = "key:cmd+banana"
        with self.assertRaises(ValueError):
            ctl.save(state["profiles"])

    def test_backup_decode(self):
        ctl = g300_gui.Controller(demo=True)
        backup = ctl.state()["backup"]
        self.assertEqual(g300_gui.decode_backup(json.loads(json.dumps(backup))), ctl.state()["profiles"])


if __name__ == "__main__":
    unittest.main()
