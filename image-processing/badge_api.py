#!/usr/bin/env python3
"""
badge_api.py -- client for the HTN OS badge service.

The 2026 badge does not take Lua apps over USB. It runs HTN OS, joins wifi,
registers itself with https://badge.solana-htn.com and gets a permanent
five-character HTN-ID. Everything a laptop wants to do goes:

    our code --HTTPS--> badge.solana-htn.com --WSS--> HTN OS on the badge

so there is no serial port, no LVGL packing, no 44-byte radio budget, and no
sprite encoder. We POST a PNG and the service converts it to RGB565 for us.

Auth is one shared secret per badge: the owner sets an app key on the badge
itself (Settings -> App key), and we send it as the X-Badge-Key header.

Docs: https://solana-htn.com/badge/docs
"""

import argparse
import json
import os
import sys
import time

import requests

DEFAULT_BASE = os.environ.get("BADGE_API", "https://badge.solana-htn.com")

# The service rate-limits at 20 commands/s (burst 40) and 400 KB/s per badge.
# Nothing here comes close, but the pacing below keeps bursts of LED writes
# from tripping it.
MIN_INTERVAL_S = 0.06


class BadgeError(RuntimeError):
    """A badge command the service refused or the badge could not answer."""

    def __init__(self, status, code, message=None):
        self.status = status
        self.code = code
        super().__init__(f"{status} {code}" + (f": {message}" if message else ""))

    @property
    def offline(self):
        return self.code in ("badge_offline", "badge_timeout")


class Badge:
    """One badge, addressed by HTN-ID and app key."""

    def __init__(self, badge_id, key, base=DEFAULT_BASE, timeout=12, label=None):
        self.id = badge_id
        self.key = key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.label = label or badge_id
        self._session = requests.Session()
        self._last_call = 0.0

    def __repr__(self):
        return f"<Badge {self.label} ({self.id})>"

    # ------------------------------------------------------------ plumbing --

    def _url(self, path):
        return f"{self.base}/v1/badges/{self.id}{path}"

    def _pace(self):
        gap = time.monotonic() - self._last_call
        if gap < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - gap)
        self._last_call = time.monotonic()

    def _request(self, method, path, *, keyed=True, **kw):
        self._pace()
        headers = dict(kw.pop("headers", {}))
        if keyed:
            headers["X-Badge-Key"] = self.key
        kw.setdefault("timeout", self.timeout)
        r = self._session.request(method, self._url(path), headers=headers, **kw)

        try:
            body = r.json()
        except ValueError:
            body = {}

        if not r.ok:
            raise BadgeError(
                r.status_code,
                body.get("error", "http_error"),
                body.get("message") or (r.text[:120] if not body else None),
            )
        return body

    # ------------------------------------------------------------- queries --

    def status(self):
        """Registration + online state. Needs no key, so it is the right
        thing to call before deciding a badge is usable."""
        return self._request("GET", "", keyed=False)

    def online(self):
        try:
            return bool(self.status().get("online"))
        except (BadgeError, requests.RequestException):
            return False

    def info(self):
        return self._request("GET", "/info")

    def buttons(self):
        return self._request("GET", "/buttons").get("buttons", {})

    def accel(self):
        return self._request("GET", "/accel")

    # -------------------------------------------------------------- screen --

    def clear(self, color="#000000"):
        return self._request("POST", "/clear", json={"color": color})

    def text(self, text, x=8, y=8, size=2, color="#ffffff", background=None,
             clear=False):
        body = {"text": text, "x": x, "y": y, "size": size, "color": color,
                "clear": clear}
        if background is not None:
            body["background"] = background
        return self._request("POST", "/text", json=body)

    def rect(self, x, y, w, h, color="#ffffff"):
        return self._request(
            "POST", "/rect", json={"x": x, "y": y, "w": w, "h": h, "color": color}
        )

    def image(self, data, x=0, y=0, fit="contain", settle=1.4):
        """Send a PNG or JPEG. `data` may be bytes, a path, or a PIL Image.

        Raw binary beats the base64 JSON form: no 33% inflation against the
        512 KiB cap, and nothing to escape.

        The POST returns as soon as the *service* has the image; the badge is
        still being fed pixels over its websocket in 8-row chunks for about a
        second after that. Any command sent into that window cuts the stream
        and you get half a picture with a hard horizontal seam. `settle` holds
        the line until the panel has caught up -- do not set it to 0 unless
        the next thing you do is nothing.
        """
        data = _as_png_bytes(data)
        if len(data) > 512 * 1024:
            raise BadgeError(413, "image_too_large", f"{len(data)} bytes")
        r = self._request(
            "POST",
            "/image",
            params={"x": x, "y": y, "fit": fit},
            headers={"content-type": "image/png"},
            data=data,
        )
        if settle:
            time.sleep(settle)
            self._last_call = time.monotonic()
        return r

    # ---------------------------------------------------------------- leds --

    def leds(self, colors=None, all=None):
        """`colors` is a 6-list of colour-or-None; `all` sets every LED.

        LEDs here are 0-indexed and ordered upper-left, upper-right,
        middle-right, bottom-right, bottom-left, middle-left.
        """
        if all is not None:
            body = {"all": all}
        else:
            body = {"leds": list(colors)[:6]}
        return self._request("POST", "/leds", json=body)

    def leds_off(self):
        return self.leds(all="#000000")

    def home(self):
        """Kick the badge back to the HTN OS menu."""
        return self._request("POST", "/home")

    # -------------------------------------------------------------- events --

    def events(self, reconnect=True):
        """Yield decoded SSE events: button presses, accel, mode, online/offline.

        Generator. Blocks. Reconnects on a dropped stream by default, because
        the stream dies whenever the badge sleeps or roams between APs.
        """
        while True:
            try:
                with self._session.get(
                    self._url("/events"),
                    params={"key": self.key},
                    stream=True,
                    timeout=(10, None),
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            yield json.loads(line[5:].strip())
                        except ValueError:
                            continue
            except (requests.RequestException, OSError) as exc:
                if not reconnect:
                    raise
                print(f"[badge {self.label}] event stream dropped: {exc}",
                      file=sys.stderr)
                time.sleep(2)


def _as_png_bytes(data):
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        with open(data, "rb") as f:
            return f.read()
    # PIL Image, duck-typed so this module does not hard-depend on Pillow.
    if hasattr(data, "save"):
        import io

        buf = io.BytesIO()
        data.save(buf, format="PNG")
        return buf.getvalue()
    raise TypeError(f"cannot send {type(data).__name__} as an image")


# ------------------------------------------------------------------ roster --


def load_roster(path=None):
    """Read the badge roster.

    Order of precedence:
      1. BADGE_ID / BADGE_KEY environment variables (single badge, quickest)
      2. badges.json next to this file, or the path given
      3. nothing -> empty list, callers should degrade rather than crash

    badges.json:
      {"badges": [{"id": "xb2b9", "key": "hunter2", "label": "Amar"}]}
    """
    env_id, env_key = os.environ.get("BADGE_ID"), os.environ.get("BADGE_KEY")
    if env_id and env_key:
        return [Badge(env_id, env_key, label=os.environ.get("BADGE_LABEL"))]

    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "badges.json")
    if not os.path.exists(path):
        return []

    with open(path) as f:
        cfg = json.load(f)
    base = cfg.get("base", DEFAULT_BASE)
    return [
        Badge(b["id"], b["key"], base=base, label=b.get("label"))
        for b in cfg.get("badges", [])
        if b.get("id") and b.get("key")
    ]


# --------------------------------------------------------------------- cli --


def main():
    ap = argparse.ArgumentParser(description="poke the HTN badge service")
    ap.add_argument("--id", default=os.environ.get("BADGE_ID"))
    ap.add_argument("--key", default=os.environ.get("BADGE_KEY"))
    ap.add_argument("--base", default=DEFAULT_BASE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="service health (no badge needed)")
    sub.add_parser("status", help="is this badge registered and online")
    sub.add_parser("info", help="firmware, ip, rssi, heap")
    sub.add_parser("buttons")
    sub.add_parser("watch", help="stream events until ctrl-c")
    sub.add_parser("selftest", help="clear, text, leds, image round trip")
    sub.add_parser("home")

    p = sub.add_parser("text")
    p.add_argument("body")
    p.add_argument("--size", type=int, default=3)
    p.add_argument("--color", default="#14f195")
    p.add_argument("--x", type=int, default=8)
    p.add_argument("--y", type=int, default=8)

    p = sub.add_parser("image")
    p.add_argument("path")
    p.add_argument("--fit", default="contain", choices=["contain", "none"])

    p = sub.add_parser("leds")
    p.add_argument("color", help="#rrggbb, or 'off'")

    p = sub.add_parser("clear")
    p.add_argument("--color", default="#000000")

    a = ap.parse_args()

    if a.cmd == "health":
        print(json.dumps(requests.get(f"{a.base}/v1/health", timeout=10).json(),
                         indent=2))
        return

    if not a.id or not a.key:
        ap.error("need --id and --key (or BADGE_ID / BADGE_KEY)")
    b = Badge(a.id, a.key, base=a.base)

    try:
        if a.cmd == "status":
            print(json.dumps(b.status(), indent=2))
        elif a.cmd == "info":
            print(json.dumps(b.info(), indent=2))
        elif a.cmd == "buttons":
            print(json.dumps(b.buttons(), indent=2))
        elif a.cmd == "home":
            b.home()
            print("sent home")
        elif a.cmd == "clear":
            b.clear(a.color)
            print("cleared")
        elif a.cmd == "text":
            b.text(a.body, x=a.x, y=a.y, size=a.size, color=a.color, clear=True)
            print("sent")
        elif a.cmd == "image":
            print(json.dumps(b.image(a.path, fit=a.fit), indent=2))
        elif a.cmd == "leds":
            b.leds_off() if a.color == "off" else b.leds(all=a.color)
            print("sent")
        elif a.cmd == "watch":
            print(f"watching {a.id}, ctrl-c to stop")
            for ev in b.events():
                print(json.dumps(ev))
        elif a.cmd == "selftest":
            st = b.status()
            print(f"registered={st.get('registeredAt')} online={st.get('online')} "
                  f"mode={st.get('mode')}")
            if not st.get("online"):
                print("badge is offline -- check its wifi", file=sys.stderr)
                sys.exit(1)
            b.clear("#000010")
            b.text("HTN OS\nlink up", x=12, y=90, size=3, color="#14f195")
            b.leds(["#14f195", None, None, None, None, "#9945ff"])
            print("screen + leds sent; check the badge")
    except BadgeError as exc:
        print(f"badge error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
