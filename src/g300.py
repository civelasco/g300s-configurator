#!/usr/bin/env python3
"""
g300 - configure a Logitech G300 / G300s mouse without Logitech's software.

The G300s stores everything on the mouse itself (3 profiles, each with 4 DPI
levels, a report rate, an LED colour and 9 button bindings). This tool reads and
writes those profiles over USB HID feature reports, so once you've configured
the mouse it keeps working on any computer with no driver installed.

Protocol reference: libratbag's driver-logitech-g300.c
  0xF0      4 bytes   active profile / active DPI level (get + set)
  0xF3-F5  35 bytes   profile 0-2

Requires:  pip install hidapi
"""

import argparse
import datetime as _dt
import json
import os
import sys
import time

VENDOR_ID = 0x046D
PRODUCT_IDS = (0xC246,)  # G300 and G300s share this ID

REPORT_ACTIVE = 0xF0
REPORT_PROFILES = (0xF3, 0xF4, 0xF5)
SIZE_ACTIVE = 4
SIZE_PROFILE = 35

NUM_PROFILES = 3
NUM_DPI = 4
NUM_BUTTONS = 9
DPI_STEP = 250
DPI_MIN, DPI_MAX = 250, 2500

BACKUP_DIR = os.path.expanduser("~/.g300/backups")

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

BUTTON_LABELS = [
    "G1 (left click)", "G2 (right click)", "G3 (wheel click)",
    "G4", "G5", "G6", "G7", "G8", "G9",
]

# Raw "code" byte -> action name. 0x00 means "key" or "disabled".
ACTIONS = {
    0x01: "left", 0x02: "right", 0x03: "middle", 0x04: "back", 0x05: "forward",
    0x06: "button6", 0x07: "button7", 0x08: "button8", 0x09: "button9",
    0x0A: "dpi-up", 0x0B: "dpi-down", 0x0C: "dpi-cycle", 0x0D: "profile-cycle",
    0x0E: "dpi-shift", 0x0F: "dpi-default",
}
ACTION_CODES = {name: code for code, name in ACTIONS.items()}
ACTION_CODES.update({"button1": 1, "button2": 2, "button3": 3, "button4": 4, "button5": 5})

RATES = {0: 1000, 1: 125, 2: 250, 3: 500}  # raw -> Hz
RATE_CODES = {hz: raw for raw, hz in RATES.items()}

# LED byte: bit0 red, bit1 green, bit2 blue
COLORS = {
    "off": (0, 0, 0), "red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1),
    "yellow": (1, 1, 0), "cyan": (0, 1, 1), "magenta": (1, 0, 1), "white": (1, 1, 1),
}

MODIFIERS = {  # name -> bit in the modifier byte
    "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "cmd": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rcmd": 0x80,
}
MODIFIER_ALIASES = {
    "control": "ctrl", "lctrl": "ctrl", "lshift": "shift", "option": "alt",
    "opt": "alt", "lalt": "alt", "command": "cmd", "meta": "cmd", "win": "cmd",
    "super": "cmd", "gui": "cmd", "lcmd": "cmd", "roption": "ralt", "rmeta": "rcmd",
}


def _build_keys():
    """HID keyboard usage IDs (USB HID Usage Tables, page 0x07)."""
    keys = {}
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        keys[ch] = 0x04 + i
    for i, ch in enumerate("1234567890"):
        keys[ch] = 0x1E + i
    for i in range(12):
        keys["f%d" % (i + 1)] = 0x3A + i
    for i in range(12):
        keys["f%d" % (i + 13)] = 0x68 + i
    keys.update({
        "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
        "minus": 0x2D, "equal": 0x2E, "leftbracket": 0x2F, "rightbracket": 0x30,
        "backslash": 0x31, "semicolon": 0x33, "quote": 0x34, "grave": 0x35,
        "comma": 0x36, "period": 0x37, "slash": 0x38, "capslock": 0x39,
        "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49,
        "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53,
        "kp_divide": 0x54, "kp_multiply": 0x55, "kp_minus": 0x56, "kp_plus": 0x57,
        "kp_enter": 0x58, "kp_0": 0x62, "kp_period": 0x63,
        "mute": 0x7F, "volumeup": 0x80, "volumedown": 0x81,
    })
    for i in range(9):
        keys["kp_%d" % (i + 1)] = 0x59 + i
    return keys


KEYS = _build_keys()
KEY_NAMES = {code: name for name, code in KEYS.items()}
KEY_ALIASES = {
    "return": "enter", "escape": "esc", "del": "delete", "-": "minus", "=": "equal",
    "[": "leftbracket", "]": "rightbracket", "\\": "backslash", ";": "semicolon",
    "'": "quote", "`": "grave", ",": "comma", ".": "period", "/": "slash",
    "pgup": "pageup", "pgdn": "pagedown", "ins": "insert",
}


# ---------------------------------------------------------------------------
# Profile model
# ---------------------------------------------------------------------------

class Binding:
    """One button: either a built-in action, a key combo, or disabled."""

    def __init__(self, code=0, modifier=0, key=0):
        self.code, self.modifier, self.key = code, modifier, key

    @classmethod
    def parse(cls, text):
        """Parse 'right', 'dpi-up', 'disabled', 'key:cmd+shift+4', 'ctrl+c'."""
        t = text.strip().lower()
        if t in ("disabled", "none", "off"):
            return cls()
        if t in ACTION_CODES:
            return cls(ACTION_CODES[t])
        if t.startswith("key:"):
            t = t[4:]
        parts = [p for p in t.replace(" ", "").split("+") if p]
        if not parts:
            raise ValueError("empty key binding")
        mods = 0
        for p in parts[:-1]:
            p = MODIFIER_ALIASES.get(p, p)
            if p not in MODIFIERS:
                raise ValueError("unknown modifier %r (use: %s)" % (p, ", ".join(MODIFIERS)))
            mods |= MODIFIERS[p]
        last = parts[-1]
        last = MODIFIER_ALIASES.get(last, last)
        if last in MODIFIERS:  # a bare modifier, e.g. "shift"
            return cls(0, mods | MODIFIERS[last], 0)
        last = KEY_ALIASES.get(last, last)
        if last not in KEYS:
            raise ValueError("unknown action or key %r (try `g300.py keys`)" % parts[-1])
        return cls(0, mods, KEYS[last])

    def describe(self):
        if self.code in ACTIONS:
            return ACTIONS[self.code]
        if self.code == 0 and (self.modifier or self.key):
            names = [n for n, bit in MODIFIERS.items() if self.modifier & bit]
            if self.key:
                names.append(KEY_NAMES.get(self.key, "0x%02x" % self.key))
            return "key:" + "+".join(names)
        if self.code == 0:
            return "disabled"
        return "unknown(0x%02x)" % self.code

    def to_bytes(self):
        return bytes([self.code, self.modifier, self.key])


class Profile:
    """A decoded 35-byte profile report. Unknown bits are preserved."""

    def __init__(self, raw):
        raw = bytes(raw)
        if len(raw) != SIZE_PROFILE:
            raise ValueError("profile report must be %d bytes, got %d" % (SIZE_PROFILE, len(raw)))
        if raw[0] not in REPORT_PROFILES:
            raise ValueError("unexpected report id 0x%02x" % raw[0])
        self.report_id = raw[0]
        self.led_byte = raw[1]
        self.rate_raw = raw[2]
        self.dpi_bytes = list(raw[3:7])
        self.unknown2 = raw[7]
        self.buttons = [Binding(*raw[8 + 3 * i: 11 + 3 * i]) for i in range(NUM_BUTTONS)]

    @property
    def index(self):
        return REPORT_PROFILES.index(self.report_id)

    # -- LED --
    @property
    def color(self):
        rgb = (self.led_byte & 1, (self.led_byte >> 1) & 1, (self.led_byte >> 2) & 1)
        for name, value in COLORS.items():
            if value == rgb:
                return name
        return "?"

    @color.setter
    def color(self, name):
        if name not in COLORS:
            raise ValueError("colour must be one of: " + ", ".join(COLORS))
        r, g, b = COLORS[name]
        self.led_byte = (self.led_byte & ~0x07) | r | (g << 1) | (b << 2)

    # -- report rate --
    @property
    def rate(self):
        return RATES.get(self.rate_raw)

    @rate.setter
    def rate(self, hz):
        if hz not in RATE_CODES:
            raise ValueError("report rate must be one of: %s Hz" % ", ".join(map(str, sorted(RATE_CODES))))
        self.rate_raw = RATE_CODES[hz]

    # -- DPI --
    @property
    def dpis(self):
        return [(b & 0x7F) * DPI_STEP for b in self.dpi_bytes]

    @dpis.setter
    def dpis(self, values):
        if len(values) != NUM_DPI:
            raise ValueError("give exactly %d DPI values" % NUM_DPI)
        new = []
        for v, old in zip(values, self.dpi_bytes):
            if v % DPI_STEP or not DPI_MIN <= v <= DPI_MAX:
                raise ValueError("DPI %d invalid: must be %d-%d in steps of %d" % (v, DPI_MIN, DPI_MAX, DPI_STEP))
            new.append((old & 0x80) | (v // DPI_STEP))
        self.dpi_bytes = new

    @property
    def default_dpi(self):
        for i, b in enumerate(self.dpi_bytes):
            if b & 0x80:
                return i
        return None

    @default_dpi.setter
    def default_dpi(self, index):
        if not 0 <= index < NUM_DPI:
            raise ValueError("default DPI level must be 1-%d" % NUM_DPI)
        self.dpi_bytes = [(b & 0x7F) | (0x80 if i == index else 0) for i, b in enumerate(self.dpi_bytes)]

    def to_bytes(self):
        out = bytes([self.report_id, self.led_byte, self.rate_raw]) + bytes(self.dpi_bytes)
        out += bytes([self.unknown2]) + b"".join(b.to_bytes() for b in self.buttons)
        assert len(out) == SIZE_PROFILE
        return out

    def describe(self, active=False):
        lines = ["Profile %d%s" % (self.index + 1, "  (active)" if active else "")]
        lines.append("  LED colour : %s" % self.color)
        lines.append("  Report rate: %s Hz" % (self.rate if self.rate else "unknown(0x%02x)" % self.rate_raw))
        levels = []
        for i, d in enumerate(self.dpis):
            levels.append(("[%d]" if i == self.default_dpi else "%d") % d)
        lines.append("  DPI levels : %s   ([..] = default)" % "  ".join(levels))
        lines.append("  Buttons:")
        for label, b in zip(BUTTON_LABELS, self.buttons):
            lines.append("    %-18s -> %s" % (label, b.describe()))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# USB / HID access
# ---------------------------------------------------------------------------

def _use_shared_mode(hid):
    """On macOS hidapi grabs devices exclusively by default, which fails for the mouse's
    keyboard interface (and would steal input from the OS). Switch to shared access."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        fn = ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive
        fn.argtypes = [ctypes.c_int]
        fn(0)
    except (OSError, AttributeError):
        pass


class Mouse:
    def __init__(self, dev, info):
        self.dev = dev
        self.info = info

    @classmethod
    def open(cls):
        try:
            import hid
        except ImportError:
            sys.exit("The 'hidapi' Python package is missing. Install it with:\n    pip3 install hidapi")

        _use_shared_mode(hid)
        candidates = [d for d in hid.enumerate(VENDOR_ID) if d["product_id"] in PRODUCT_IDS]
        if not candidates:
            sys.exit("No Logitech G300/G300s found. Is it plugged in directly (not through a KVM)?")

        # The mouse exposes several HID interfaces. On macOS, interface 0 (the pointer) answers
        # reads but rejects writes; interface 1's keyboard collection accepts both. Try it first.
        candidates.sort(key=lambda d: (d.get("interface_number") != 1, d.get("usage_page") != 1))
        errors = []
        for info in candidates:
            dev = hid.device()
            try:
                dev.open_path(info["path"])
                data = dev.get_feature_report(REPORT_ACTIVE, SIZE_ACTIVE)
                if len(data) >= 2 and data[0] == REPORT_ACTIVE and (data[1] >> 4) < NUM_PROFILES:
                    return cls(dev, info)
                errors.append("interface %s: unexpected reply %r" % (info.get("interface_number"), data))
            except (IOError, OSError, ValueError) as e:
                errors.append("interface %s: %s" % (info.get("interface_number"), e))
            dev.close()
        hint = ""
        if sys.platform == "darwin":
            hint = ("\nOn macOS, allow G300s Configurator (or the Terminal you ran this from) under\n"
                    "System Settings > Privacy & Security > Input Monitoring, then reopen it.")
        sys.exit("Found the mouse but couldn't talk to it:\n  " + "\n  ".join(errors) + hint)

    def close(self):
        self.dev.close()

    def _get(self, report_id, size):
        data = bytes(self.dev.get_feature_report(report_id, size))
        # Some hidapi backends omit the report id byte; normalise.
        if len(data) == size - 1:
            data = bytes([report_id]) + data
        if len(data) != size or data[0] != report_id:
            raise IOError("bad reply to report 0x%02x: %s" % (report_id, data.hex()))
        return data

    def _set(self, data):
        n = self.dev.send_feature_report(bytes(data))
        if n < 0:
            raise IOError("writing report 0x%02x failed" % data[0])

    def read_active(self):
        """Returns (profile_index, dpi_level_index)."""
        data = self._get(REPORT_ACTIVE, SIZE_ACTIVE)
        return (data[1] >> 4) & 0x0F, (data[1] >> 1) & 0x07

    def read_profile(self, index):
        return Profile(self._get(REPORT_PROFILES[index], SIZE_PROFILE))

    def read_all(self):
        return [self.read_profile(i) for i in range(NUM_PROFILES)]

    def write_profile(self, profile):
        self._set(profile.to_bytes())
        # The mouse saves to flash slowly: reading back too soon returns the old profile, and
        # sending another write before it's done can leave a corrupted profile. Give it time.
        time.sleep(2.0)

    def set_active_profile(self, index):
        self._set([REPORT_ACTIVE, 0x80 | (index << 4), 0, 0])

    def set_active_dpi(self, index):
        self._set([REPORT_ACTIVE, 0x40 | (index << 1), 0, 0])


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def profiles_to_json(profiles):
    return {"device": "Logitech G300s", "format": 1,
            "profiles": [p.to_bytes().hex() for p in profiles]}


def profiles_from_json(obj):
    raws = obj["profiles"]
    if len(raws) != NUM_PROFILES:
        raise ValueError("backup must contain %d profiles" % NUM_PROFILES)
    return [Profile(bytes.fromhex(h)) for h in raws]


def auto_backup(profiles):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, _dt.datetime.now().strftime("g300-%Y%m%d-%H%M%S-%f.json"))
    with open(path, "w") as f:
        json.dump(profiles_to_json(profiles), f, indent=2)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def profile_arg(value):
    n = int(value)
    if not 1 <= n <= NUM_PROFILES:
        raise argparse.ArgumentTypeError("profile must be 1-%d" % NUM_PROFILES)
    return n - 1


def button_arg(value):
    v = value.lower().lstrip("g")
    n = int(v)
    if not 1 <= n <= NUM_BUTTONS:
        raise argparse.ArgumentTypeError("button must be 1-%d (or G1-G9)" % NUM_BUTTONS)
    return n - 1


def commit(mouse, before, after, dry_run):
    changed = [a for b, a in zip(before, after) if b.to_bytes() != a.to_bytes()]
    if not changed:
        print("Nothing changed.")
        return
    for p in changed:
        print(p.describe())
    if dry_run:
        print("\n(dry run - nothing written)")
        return
    print("\nBacked up previous settings to", auto_backup(before))
    for p in changed:
        mouse.write_profile(p)
    print("Saved to the mouse.")


def cmd_show(mouse, args):
    active, level = mouse.read_active()
    profiles = mouse.read_all()
    which = [args.profile] if args.profile is not None else range(NUM_PROFILES)
    for i in which:
        print(profiles[i].describe(active=(i == active)))
        if i == active:
            print("  Current DPI: %d (level %d)" % (profiles[i].dpis[level], level + 1))
        if args.raw:
            print("  Raw: " + profiles[i].to_bytes().hex(" "))
        print()


def cmd_set(mouse, args):
    before = mouse.read_all()
    after = [Profile(p.to_bytes()) for p in before]
    p = after[args.profile]
    if args.dpi:
        p.dpis = [int(x) for x in args.dpi.split(",")]
    if args.default_dpi is not None:
        p.default_dpi = args.default_dpi - 1
    if args.rate is not None:
        p.rate = args.rate
    if args.color:
        p.color = args.color.lower()
    commit(mouse, before, after, args.dry_run)


def cmd_bind(mouse, args):
    before = mouse.read_all()
    after = [Profile(p.to_bytes()) for p in before]
    after[args.profile].buttons[args.button] = Binding.parse(args.action)
    commit(mouse, before, after, args.dry_run)


def cmd_activate(mouse, args):
    mouse.set_active_profile(args.profile)
    print("Switched to profile %d." % (args.profile + 1))


def cmd_backup(mouse, args):
    with open(args.file, "w") as f:
        json.dump(profiles_to_json(mouse.read_all()), f, indent=2)
    print("Saved all 3 profiles to", args.file)


def cmd_restore(mouse, args):
    with open(args.file) as f:
        after = profiles_from_json(json.load(f))
    commit(mouse, mouse.read_all(), after, args.dry_run)


def cmd_reset(mouse, args):
    """Write a clean, sensible configuration (not necessarily Logitech's exact factory one)."""
    before = mouse.read_all()
    after = [Profile(p.to_bytes()) for p in before]
    defaults = [
        # colour, dpi levels, default level, buttons G1..G9
        ("red", [500, 1000, 1500, 2500], 1,
         ["left", "right", "middle", "back", "forward", "dpi-up", "dpi-down", "profile-cycle", "dpi-shift"]),
        ("green", [500, 1000, 1500, 2500], 1,
         ["left", "right", "middle", "back", "forward", "dpi-up", "dpi-down", "profile-cycle", "dpi-shift"]),
        ("blue", [500, 1000, 1500, 2500], 1,
         ["left", "right", "middle", "back", "forward", "dpi-up", "dpi-down", "profile-cycle", "dpi-shift"]),
    ]
    for p, (color, dpis, default, buttons) in zip(after, defaults):
        p.color, p.dpis, p.default_dpi, p.rate = color, dpis, default, 1000
        p.buttons = [Binding.parse(b) for b in buttons]
    commit(mouse, before, after, args.dry_run)


def cmd_keys(_mouse, _args):
    print("Actions : " + ", ".join(sorted(set(ACTION_CODES) | {"disabled"})))
    print("Modifiers: " + ", ".join(MODIFIERS) + "   (cmd = Command/Windows key, alt = Option)")
    print("Keys    : " + ", ".join(KEYS))
    print("\nExamples: right   dpi-cycle   key:cmd+c   key:ctrl+shift+tab   key:f13   disabled")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Configure a Logitech G300/G300s mouse (settings are stored on the mouse).")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("show", help="show the current configuration")
    s.add_argument("-p", "--profile", type=profile_arg, help="only this profile (1-3)")
    s.add_argument("--raw", action="store_true", help="also print raw bytes")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("set", help="change DPI, report rate or LED colour")
    s.add_argument("-p", "--profile", type=profile_arg, required=True, help="profile 1-3")
    s.add_argument("--dpi", help="4 comma-separated DPI levels, e.g. 500,1000,1500,2500")
    s.add_argument("--default-dpi", type=int, help="which DPI level (1-4) is the default")
    s.add_argument("--rate", type=int, help="report rate in Hz: 125, 250, 500, 1000")
    s.add_argument("--color", help="LED: " + ", ".join(COLORS))
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("bind", help="remap a button")
    s.add_argument("-p", "--profile", type=profile_arg, required=True, help="profile 1-3")
    s.add_argument("button", type=button_arg, help="button 1-9 or G1-G9")
    s.add_argument("action", help="e.g. right, dpi-up, key:cmd+c, disabled (see `keys`)")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_bind)

    s = sub.add_parser("activate", help="switch the active profile")
    s.add_argument("profile", type=profile_arg)
    s.set_defaults(func=cmd_activate)

    s = sub.add_parser("backup", help="save all profiles to a JSON file")
    s.add_argument("file")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="write profiles from a JSON backup")
    s.add_argument("file")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("reset", help="write a clean default setup to all 3 profiles")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("keys", help="list action and key names for `bind`")
    s.set_defaults(func=cmd_keys)

    args = ap.parse_args(argv)
    if args.command == "keys":
        return cmd_keys(None, args)
    mouse = Mouse.open()
    try:
        args.func(mouse, args)
    except ValueError as e:
        sys.exit("Error: %s" % e)
    finally:
        mouse.close()


if __name__ == "__main__":
    main()
