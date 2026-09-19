#!/usr/bin/env python3
"""
receiver.py - runs on the laptop.

Saves every photo the ball sends into ./photos/.

    python3 receiver.py

Open http://soufz.local:8080/ in a browser to check it is alive.
Standard library only, nothing to install.
"""

import datetime
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.text(411, "need a Content-Length")
            return

        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(min(65536, length - len(data)))
            if not chunk:
                break
            data.extend(chunk)

        if len(data) != length:
            print(f"short read: {len(data)} of {length} bytes")
            self.text(400, "incomplete upload")
            return

        name = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + ".jpg"
        with open(os.path.join(SAVE_DIR, name), "wb") as f:
            f.write(data)

        print(f"saved {name}  {len(data)} bytes  from {self.client_address[0]}")
        self.text(200, name)

    def do_GET(self):
        n = len([f for f in os.listdir(SAVE_DIR) if f.endswith(".jpg")])
        self.text(200, f"receiver is up. {n} photos so far.\n")

    def text(self, code, body):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.makedirs(SAVE_DIR, exist_ok=True)
    print(f"listening on port {PORT}, saving into {SAVE_DIR}")
    print("ctrl-c to stop\n")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
