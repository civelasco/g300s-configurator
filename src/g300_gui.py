#!/usr/bin/env python3
"""
G300s Configurator - a point-and-click app for the Logitech G300/G300s.

Starts a small web server on this computer only (127.0.0.1) and opens the
settings page in your browser. Uses the same mouse protocol as g300.py.

    python3 g300_gui.py           # talk to the real mouse
    python3 g300_gui.py --demo    # try it with a simulated mouse
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import g300

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "g300_gui.html")


class DemoDevice:
    """Pretends to be a G300s so the app can be tried without the mouse."""

    def __init__(self):
        # Same shape as a real G300s found in the wild: two empty profiles, one set up.
        self.store = {
            0xF0: bytes([0xF0, 0x22, 0, 0]),
            0xF3: bytes([0xF3, 0x04]) + bytes(33),
            0xF4: bytes([0xF4, 0x07]) + bytes(33),
            0xF5: bytes.fromhex("f5040302840608440100000200000300000400000500000a00000b00000c0000000010"),
        }

    def get_feature_report(self, rid, size):
        return list(self.store[rid])

    def send_feature_report(self, data):
        data = bytes(data)
        if data[0] == g300.REPORT_ACTIVE:
            if data[1] & 0x80:
                self.store[0xF0] = bytes([0xF0, data[1] & 0x30, 0, 0])
        else:
            self.store[data[0]] = data
        return len(data)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Profile <-> JSON for the page
# ---------------------------------------------------------------------------

def profile_to_dict(p):
    return {
        "dpis": p.dpis,
        "default_dpi": p.default_dpi if p.default_dpi is not None else 0,
        "rate": p.rate or 1000,
        "color": p.color if p.color in g300.COLORS else "off",
        "buttons": [b.describe() for b in p.buttons],
    }


def apply_dict(p, d, number):
    """Write the page's settings for one profile into Profile p."""
    try:
        p.dpis = [int(x) for x in d["dpis"]]
        p.default_dpi = int(d["default_dpi"])
        p.rate = int(d["rate"])
        p.color = d["color"]
    except ValueError as e:
        raise ValueError("Profile %d: %s" % (number, e))
    for i, text in enumerate(d["buttons"]):
        try:
            p.buttons[i] = g300.Binding.parse(text)
        except ValueError as e:
            msg = str(e).replace(" (try `g300.py keys`)", "")
            raise ValueError("Profile %d, %s: %s" % (number, g300.BUTTON_LABELS[i], msg))


class Controller:
    """Owns the mouse connection; one request at a time."""

    def __init__(self, demo):
        self.demo = demo
        self.mouse = None
        self.lock = threading.Lock()

    def _ensure(self):
        if self.mouse:
            return
        if self.demo:
            self.mouse = g300.Mouse(DemoDevice(), {})
            return
        try:
            self.mouse = g300.Mouse.open()
        except SystemExit as e:  # g300.Mouse.open reports problems this way
            raise IOError(str(e))

    def _drop(self):
        if self.mouse:
            self.mouse.close()
        self.mouse = None

    def state(self, reconnect=False):
        with self.lock:
            if reconnect:
                self._drop()
            try:
                self._ensure()
                active, level = self.mouse.read_active()
                profiles = self.mouse.read_all()
            except (IOError, OSError, ValueError) as e:
                self._drop()
                return {"connected": False, "error": str(e)}
            return {"connected": True, "demo": self.demo, "active": active, "level": level,
                    "profiles": [profile_to_dict(p) for p in profiles],
                    "backup": g300.profiles_to_json(profiles)}

    def save(self, wanted):
        with self.lock:
            self._ensure()
            before = self.mouse.read_all()
            after = [g300.Profile(p.to_bytes()) for p in before]
            for i, (p, d) in enumerate(zip(after, wanted)):
                # Leave untouched profiles byte-for-byte as they are (blank ones can't be re-encoded).
                if d != profile_to_dict(before[i]):
                    apply_dict(p, d, i + 1)
            changed = [i for i in range(g300.NUM_PROFILES) if after[i].to_bytes() != before[i].to_bytes()]
            backup = None
            if changed:
                backup = g300.auto_backup(before)
                for i in changed:
                    self.mouse.write_profile(after[i])
            readback = self.mouse.read_all()
            lost = [i + 1 for i in changed if readback[i].to_bytes() != after[i].to_bytes()]
            return {"changed": [i + 1 for i in changed], "backup": backup, "not_kept": lost}

    def activate(self, index):
        with self.lock:
            self._ensure()
            self.mouse.set_active_profile(index)

    def set_dpi_level(self, index):
        with self.lock:
            self._ensure()
            self.mouse.set_active_dpi(index)


def decode_backup(obj):
    return [profile_to_dict(p) for p in g300.profiles_from_json(obj)]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(ctl):
    meta = {
        "buttons": g300.BUTTON_LABELS,
        "actions": sorted(g300.ACTIONS.values(), key=lambda a: g300.ACTION_CODES[a]),
        "colors": list(g300.COLORS),
        "rates": sorted(g300.RATE_CODES),
        "dpi": {"min": g300.DPI_MIN, "max": g300.DPI_MAX, "step": g300.DPI_STEP},
        "keys": list(g300.KEYS),
        "modifiers": list(g300.MODIFIERS),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local_only(self):
            # Refuse requests that a random website could make on your behalf.
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            if host not in ("127.0.0.1", "localhost"):
                return False
            if origin and not origin.startswith(("http://127.0.0.1:", "http://localhost:")):
                return False
            return True

        def do_GET(self):
            if not self._local_only():
                return self._send(403, {"error": "forbidden"})
            if self.path == "/":
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/api/meta":
                return self._send(200, meta)
            if self.path.startswith("/api/state"):
                return self._send(200, ctl.state(reconnect="reconnect" in self.path))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only() or self.headers.get("Content-Type") != "application/json":
                return self._send(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/save":
                    return self._send(200, ctl.save(body["profiles"]))
                if self.path == "/api/activate":
                    ctl.activate(int(body["profile"]))
                    return self._send(200, {"ok": True})
                if self.path == "/api/dpi-level":
                    ctl.set_dpi_level(int(body["level"]))
                    return self._send(200, {"ok": True})
                if self.path == "/api/decode":
                    return self._send(200, {"profiles": decode_backup(body)})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            except (IOError, OSError) as e:
                return self._send(503, {"error": str(e)})
            self._send(404, {"error": "not found"})

    return Handler


def _exit_with_parent(pid):
    while True:
        time.sleep(2)
        try:
            os.kill(pid, 0)
        except OSError:
            os._exit(0)


def main():
    ap = argparse.ArgumentParser(description="Open the G300s configurator in your browser.")
    ap.add_argument("--demo", action="store_true", help="use a simulated mouse")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--parent-pid", type=int, help="quit when this process exits (used by the Mac app)")
    args = ap.parse_args()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Controller(args.demo)))
    except OSError:
        # Most likely the app is already running: just show it again.
        url = "http://127.0.0.1:%d/" % args.port
        print("The configurator seems to be running already - opening", url)
        if not args.no_browser:
            webbrowser.open(url)
        return
    url = "http://127.0.0.1:%d/" % server.server_address[1]  # --port 0 picks a free port
    if args.parent_pid:
        threading.Thread(target=_exit_with_parent, args=(args.parent_pid,), daemon=True).start()
    print("G300s Configurator running at %s%s" % (url, "  (demo mode)" if args.demo else ""), flush=True)
    print("Leave this window open while you use it. Press Ctrl+C to quit.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
