#!/usr/bin/env python3
"""
ball_server.py -- the wire between the ball and the badges.

    ESP32-CAM  --POST /upload (image/jpeg)-->  this server  :8080
                                                    |
                                          generate.process()
                                                    |
                                             card.render_card()
                                                    |
                    POST /v1/badges/<id>/image --> badge.solana-htn.com --> badge

Nothing here changes the camera firmware: it already POSTs a raw JPEG body to
/upload on port 8080, and that is exactly what this accepts. Nothing here
touches a serial port either -- the 2026 badge is reached over HTTPS.

    python3 ball_server.py                  # pick the badge from the web page
    python3 ball_server.py --assign rotate  # or let the server alternate
    python3 ball_server.py --replay captures/foo.jpg   # no camera needed

Who gets the creature (--assign):
  pick    (default) hold it, choose on the status page. Nothing is guessed.
  rotate  alternate badges on each squeeze
  hold    the badge with a button held at capture time claims it
  all     every badge shows every creature
"""

import argparse
import datetime
import html
import io
import os
import queue
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import badge_api
import card
import generate

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8080
CAPTURE_DIR = os.path.join(HERE, "captures")
DATA_DIR = os.path.join(HERE, "gamedata")
OUT_DIR = os.path.join(HERE, "creatures")
MAX_UPLOAD = 8 * 1024 * 1024

state = {
    "uploads": 0,
    "generated": 0,
    "failures": 0,
    "queued": 0,
    "last": None,       # most recent creature dict
    "last_card": None,  # most recent card PNG bytes
    "pending": None,    # {"creature":…, "png":…} awaiting a pick
    "log": [],
}
state_lock = threading.Lock()
jobs = queue.Queue()


def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with state_lock:
        state["log"].append(line)
        del state["log"][:-40]


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


# ------------------------------------------------------------------ badges --


class Roster:
    """The badges we can draw to, and the rule for choosing between them."""

    def __init__(self, badges, assign="pick"):
        self.badges = badges
        self.assign = assign
        self._next = 0
        self._lock = threading.Lock()

    def __bool__(self):
        return bool(self.badges)

    def by_id(self, badge_id):
        for b in self.badges:
            if b.id == badge_id:
                return b
        return None

    def select(self):
        """Which badges get this capture, decided at squeeze time.

        Returns [] in pick mode -- the human decides once they can see what
        they caught.
        """
        if not self.badges or self.assign == "pick":
            return []
        if len(self.badges) == 1 or self.assign == "all":
            return list(self.badges)

        if self.assign == "hold":
            for b in self.badges:
                try:
                    if any(b.buttons().values()):
                        log(f"{b.label} claimed it (button held)")
                        return [b]
                except (badge_api.BadgeError, OSError):
                    continue
            log("nobody was holding a button, falling back to rotation")

        with self._lock:
            b = self.badges[self._next % len(self.badges)]
            self._next += 1
        return [b]

    # Status text goes over the /text command, not as a full-screen PNG.
    # A PNG is 150 KB of pixels streamed to the panel; a line of text is a
    # few dozen bytes. For something we redraw on every squeeze that
    # difference is the difference between reliable and not.
    def status(self, targets, title, subtitle=None, led=None,
               colour="#f7deb4"):
        for b in targets:
            try:
                if led is not None:
                    b.leds(all=led)
                b.text(title.upper(), x=14, y=96, size=3, color=colour,
                       clear=True)
                if subtitle:
                    b.text(subtitle, x=14, y=136, size=1, color="#808088")
            except badge_api.BadgeError as exc:
                log(f"{b.label}: {exc}")
            except OSError as exc:
                log(f"{b.label}: network error: {exc}")

    def show_card(self, targets, png, led=None):
        """LEDs first, then the image, then nothing. See Badge.image()'s
        settle argument -- anything sent while the panel is still being fed
        chops the picture in half."""
        ok = []
        for b in targets:
            try:
                if led is not None:
                    b.leds(all=led)
                b.image(png, fit="none")
                ok.append(b)
            except badge_api.BadgeError as exc:
                log(f"{b.label}: {exc}")
            except OSError as exc:
                log(f"{b.label}: network error: {exc}")
        return ok


# ------------------------------------------------------------------ worker --


def handle(photo_path, targets, roster, forward_url=None):
    name = os.path.basename(photo_path)
    pick_mode = roster.assign == "pick"

    # In pick mode nobody owns it yet, so tell every badge something is
    # happening; in the auto modes only the badge that will receive it.
    watching = roster.badges if pick_mode else targets
    with state_lock:
        depth = state["queued"]
    roster.status(watching, "Analysing",
                  name if depth <= 1 else f"{depth} in the queue",
                  led="#201040")

    try:
        creature = generate.process(photo_path, OUT_DIR, DATA_DIR)
    except Exception as exc:  # noqa: BLE001 - one bad photo must not kill the loop
        log(f"generation failed for {name}: {exc}")
        traceback.print_exc()
        with state_lock:
            state["failures"] += 1
        roster.status(watching, "Jammed", str(exc)[:40], led="#300000",
                      colour="#d65c46")
        return None

    owner = targets[0].label if len(targets) == 1 else None
    img = card.render_card(creature, creature.get("sprite"), DATA_DIR,
                           owner=owner)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    with state_lock:
        state["generated"] += 1
        state["last"] = creature
        state["last_card"] = png

    accent = _hex(card.TYPE_COLOUR.get(creature.get("type"), (128, 128, 136)))

    if pick_mode:
        with state_lock:
            state["pending"] = {"creature": creature, "png": png}
        roster.status(roster.badges, creature["name"], "waiting to be claimed",
                      led=accent, colour=accent)
        log(f"{creature['name']} ({creature['type']}) ready -- pick a badge "
            f"at http://localhost:{state.get('port', PORT)}/")
    else:
        sent = roster.show_card(targets, png, led=accent)
        log(f"{creature['name']} ({creature['type']}) -> "
            f"{', '.join(b.label for b in sent) or 'nobody'}")

    if forward_url:
        forward(creature, forward_url)
    return creature


def send_to(roster, badge_ids, png, creature, clear_others=True):
    """Push an already-rendered card to the chosen badges."""
    targets = [b for b in (roster.by_id(i) for i in badge_ids) if b]
    if not targets:
        return []
    accent = _hex(card.TYPE_COLOUR.get((creature or {}).get("type"),
                                       (128, 128, 136)))
    sent = roster.show_card(targets, png, led=accent)

    if clear_others:
        others = [b for b in roster.badges if b not in targets]
        roster.status(others, "Ready", "squeeze to catch", led="#101030")

    log(f"{(creature or {}).get('name', 'card')} -> "
        f"{', '.join(b.label for b in sent) or 'nobody'}")
    return sent


def forward(creature, url):
    """Hand the creature to Aiden's FastAPI world so the web gallery sees it.

    models.Pokemon is extra="forbid" and aliases type->element, so project the
    fields it accepts rather than posting our record wholesale.
    """
    import requests

    payload = {
        "name": creature["name"],
        "species": creature["species"],
        "type": creature["type"],
        "stats": creature["stats"],
        "moves": creature["moves"],
        "flavour": creature["flavour"],
        "sprite_prompt": creature["sprite_prompt"],
        "rarity": creature["rarity"],
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            log(f"forwarded {creature['name']} to the world server")
        else:
            log(f"forward rejected {r.status_code}: {r.text[:120]}")
    except requests.RequestException as exc:
        log(f"forward failed: {exc}")


def worker(roster, forward_url):
    while True:
        photo_path, targets = jobs.get()
        try:
            handle(photo_path, targets, roster, forward_url)
        finally:
            with state_lock:
                state["queued"] -= 1
            jobs.task_done()


# -------------------------------------------------------------------- http --


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    roster = None

    # ------------------------------------------------------------ requests --

    def do_POST(self):
        path, params = self._split()

        if path in ("/upload", "/"):
            return self._upload()
        if path == "/send":
            return self._send(params)
        if path == "/clearbadge":
            return self._clear_badge(params)
        return self.reply(404, "no such path")

    def do_GET(self):
        path, params = self._split()

        if path == "/last.png":
            with state_lock:
                png = state["last_card"]
            if not png:
                return self.reply(404, "nothing generated yet")
            return self.binary(png, "image/png")

        if path == "/status":
            import json

            with state_lock:
                body = json.dumps(
                    {
                        "uploads": state["uploads"],
                        "generated": state["generated"],
                        "failures": state["failures"],
                        "queued": state["queued"],
                        "pending": bool(state["pending"]),
                        "last": state["last"],
                        "log": state["log"],
                    },
                    indent=2, default=str).encode()
            return self.binary(body, "application/json")

        if path == "/send":  # allow GET so the buttons can be plain links
            return self._send(params)

        return self._page()

    # ------------------------------------------------------------- actions --

    def _upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.reply(411, "need a Content-Length")
        if length > MAX_UPLOAD:
            return self.reply(413, "too big")

        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(min(65536, length - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != length:
            log(f"short upload: {len(data)} of {length} bytes")
            return self.reply(400, "incomplete upload")

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = os.path.join(CAPTURE_DIR, f"{stamp}.jpg")
        with open(path, "wb") as f:
            f.write(data)

        with state_lock:
            state["uploads"] += 1
        log(f"caught {os.path.basename(path)} ({len(data)} bytes) "
            f"from {self.client_address[0]}")

        # Chosen here, not in the worker: 'hold' needs the buttons sampled
        # while the squeeze is still happening.
        targets = self.roster.select()

        with state_lock:
            state["queued"] += 1
        jobs.put((path, targets))

        # Answer the ball straight away -- it only waits 6 s, and generation
        # takes ten or more.
        return self.reply(200, os.path.basename(path))

    def _send(self, params):
        want = params.get("badge", [""])[0]
        with state_lock:
            pending = state["pending"]
            png = (pending or {}).get("png") or state["last_card"]
            creature = (pending or {}).get("creature") or state["last"]

        if not png:
            return self._redirect()

        ids = ([b.id for b in self.roster.badges] if want == "all"
               else [want])
        # Sending blocks for a couple of seconds per badge (see the settle in
        # Badge.image); do it off the request thread so the page comes back now.
        threading.Thread(
            target=send_to,
            args=(self.roster, ids, png, creature),
            daemon=True,
        ).start()

        with state_lock:
            state["pending"] = None
        return self._redirect()

    def _clear_badge(self, params):
        want = params.get("badge", [""])[0]
        targets = ([self.roster.by_id(want)] if want != "all"
                   else list(self.roster.badges))
        targets = [b for b in targets if b]
        threading.Thread(
            target=self.roster.status,
            args=(targets, "Ready", "squeeze to catch"),
            kwargs={"led": "#101030"},
            daemon=True,
        ).start()
        return self._redirect()

    # ---------------------------------------------------------------- page --

    def _page(self):
        with state_lock:
            s = dict(state)
            pending = state["pending"]

        creature = (pending or {}).get("creature") or s["last"]
        waiting = pending is not None

        rows = []
        for b in self.roster.badges:
            rows.append(
                f'<a class="btn{" hot" if waiting else ""}" '
                f'href="/send?badge={html.escape(b.id)}">'
                f'{"Claim for " if waiting else "Resend to "}'
                f'<b>{html.escape(b.label)}</b></a>'
            )
        rows.append('<a class="btn" href="/send?badge=all">Both</a>')
        clears = "".join(
            f'<a class="mini" href="/clearbadge?badge={html.escape(b.id)}">'
            f'reset {html.escape(b.label)}</a>'
            for b in self.roster.badges
        )

        if waiting:
            banner = (f'<div class="banner">Caught '
                      f'<b>{html.escape(creature["name"])}</b> &mdash; '
                      f'who is claiming it?</div>')
        elif s["queued"]:
            banner = '<div class="banner">Analysing&hellip;</div>'
        else:
            banner = '<div class="banner dim">Squeeze the ball</div>'

        art = (f'<img src="/last.png?v={s["generated"]}">'
               if s["last_card"] else "")

        body = f"""<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=4>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ball server</title>
<style>
body{{background:#1a1423;color:#f7deb4;font:15px ui-monospace,monospace;
padding:20px;max-width:700px;margin:auto}}
img{{image-rendering:pixelated;width:100%;max-width:640px;
border:1px solid #5e4250;display:block;margin:14px 0}}
.banner{{font-size:20px;margin:10px 0}}.banner.dim{{color:#808088}}
.btn{{display:inline-block;padding:11px 18px;margin:4px 6px 4px 0;
background:#3a2c40;color:#f7deb4;text-decoration:none;border-radius:6px;
border:1px solid #5e4250}}
.btn.hot{{background:#4ea888;color:#1a1423;border-color:#8cd496}}
.mini{{color:#808088;font-size:12px;margin-right:12px;text-decoration:none}}
.stat{{color:#78a8d0}}pre{{color:#808088;line-height:1.5;font-size:12px;
white-space:pre-wrap}}
</style>
<h2>ball server</h2>
{banner}
<div>{"".join(rows)}</div>
<div style="margin-top:6px">{clears}</div>
{art}
<p>uploads <span class=stat>{s['uploads']}</span> &middot;
generated <span class=stat>{s['generated']}</span> &middot;
failed <span class=stat>{s['failures']}</span> &middot;
queued <span class=stat>{s['queued']}</span> &middot;
mode <span class=stat>{self.roster.assign}</span></p>
<pre>{html.escape(chr(10).join(s['log'][-14:]))}</pre>"""
        return self.reply(200, body, "text/html; charset=utf-8")

    # ------------------------------------------------------------- helpers --

    def _split(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _redirect(self, to="/"):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def binary(self, raw, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def reply(self, code, body, ctype="text/plain"):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass


# -------------------------------------------------------------------- main --


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--assign", default="pick",
                    choices=["pick", "rotate", "hold", "all"])
    ap.add_argument("--badges", help="path to badges.json")
    ap.add_argument("--forward-url",
                    help="also POST creatures here, e.g. "
                         "http://localhost:8000/pokemon/manual")
    ap.add_argument("--replay", metavar="JPG",
                    help="run one photo through the whole path and exit")
    ap.add_argument("--no-badge", action="store_true",
                    help="generate only, do not draw to any badge")
    a = ap.parse_args()

    for d in (CAPTURE_DIR, DATA_DIR, OUT_DIR):
        os.makedirs(d, exist_ok=True)

    badges = [] if a.no_badge else badge_api.load_roster(a.badges)
    roster = Roster(badges, a.assign)
    state["port"] = a.port

    if not badges and not a.no_badge:
        print("no badges configured.\n"
              "  export BADGE_ID=xb2b9 BADGE_KEY=hunter2\n"
              "  or write badges.json (see badges.example.json)\n"
              "running anyway; creatures will generate but nothing will display.\n",
              file=sys.stderr)

    for b in badges:
        try:
            st = b.status()
            log(f"{b.label} ({b.id}): "
                f"{'online' if st.get('online') else 'OFFLINE'} "
                f"mode={st.get('mode')} fw={st.get('fw')}")
        except (badge_api.BadgeError, OSError) as exc:
            log(f"{b.label} ({b.id}): unreachable: {exc}")

    if a.replay:
        with state_lock:
            state["queued"] = 1
        handle(a.replay, roster.select(), roster, a.forward_url)
        return

    threading.Thread(target=worker, args=(roster, a.forward_url),
                     daemon=True).start()

    roster.status(badges, "Ready", "squeeze to catch", led="#101030")

    Handler.roster = roster
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    log(f"listening on :{a.port}  (the ball POSTs /upload)")
    log(f"pick badges at http://localhost:{a.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
