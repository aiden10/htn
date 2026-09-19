"""Local Web Serial bridge with bundled 28x28 Shutterdex sprites.

Run alongside the existing FastAPI server, then open http://127.0.0.1:8765.
"""
from __future__ import annotations

import argparse
import json
import struct
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "assets" / "sprites"
BADGE = ROOT / "assets" / "badge"
SPRITES = {
    "mon_mugmite_v1": "mugmite-v1.png",
    "mon_spriglet_v1": "spriglet-v1.png",
    "test_coilkit_v1": "coilkit-v1.png",
    "test_mossbyte_v1": "mossbyte-v1.png",
}
SPRITE_SIZE = 28


def encode_lvgl(image: Image.Image) -> bytes:
    canvas = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE))
    scaled = ImageOps.contain(
        image.convert("RGBA"), (SPRITE_SIZE, SPRITE_SIZE), Image.Resampling.LANCZOS
    )
    canvas.alpha_composite(
        scaled, ((SPRITE_SIZE - scaled.width) // 2, (SPRITE_SIZE - scaled.height) // 2)
    )
    rgb, alpha = bytearray(), bytearray()
    for red, green, blue, opacity in canvas.getdata():
        rgb.extend(struct.pack("<H", ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)))
        alpha.append(opacity)
    return struct.pack(
        "<IBBHHH", 0x19, 14, 0, SPRITE_SIZE, SPRITE_SIZE, SPRITE_SIZE * 2
    ) + rgb + alpha


def make_sprites() -> None:
    BADGE.mkdir(parents=True, exist_ok=True)
    for key, filename in SPRITES.items():
        with Image.open(SOURCE / filename) as image:
            (BADGE / f"{key}.bin").write_bytes(encode_lvgl(image))


class Handler(SimpleHTTPRequestHandler):
    server_base = "http://127.0.0.1:8000"

    def send_bytes(self, payload: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.send_bytes((ROOT / "bridge.html").read_bytes(), "text/html; charset=utf-8")
        if parsed.path == "/snapshot":
            world = parse_qs(parsed.query).get("world", ["test"])[0]
            prefix = "" if world == "live" else "/simulation/test"
            try:
                with urlopen(f"{self.server_base}{prefix}/badge/inbox", timeout=10) as response:
                    inbox = response.read().decode("utf-8")
                with urlopen(f"{self.server_base}{prefix}/badge/ready", timeout=10) as response:
                    ready = response.read().decode("utf-8")
            except OSError as exc:
                self.send_error(502, f"FastAPI server unavailable: {exc}")
                return
            return self.send_bytes(json.dumps({"inbox": inbox, "ready": ready}).encode(), "application/json")
        if parsed.path.startswith("/sprites/") and parsed.path.endswith(".bin"):
            key = Path(parsed.path).name.removesuffix(".bin")
            path = BADGE / f"{key}.bin"
            if path.is_file():
                return self.send_bytes(path.read_bytes(), "application/octet-stream")
            # Future Pokemon can use the server's normal sprite-upload route.
            # This fallback keeps the browser same-origin while it retrieves the
            # server-generated binary on the bridge's behalf.
            try:
                with urlopen(f"{self.server_base}/sprites/{key}.bin", timeout=10) as response:
                    return self.send_bytes(response.read(), "application/octet-stream")
            except OSError:
                pass
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/reset-test":
            request = Request(
                f"{self.server_base}/simulation/test/reset",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=15) as response:
                    return self.send_bytes(response.read(), "application/json")
            except OSError as exc:
                self.send_error(502, f"Could not reset test world: {exc}")
                return

        if parsed.path != "/advance":
            self.send_error(404)
            return

        world = parse_qs(parsed.query).get("world", ["test"])[0]
        is_live_world = world == "live"
        endpoint = "/simulation/tick" if is_live_world else "/simulation/test/tick"
        # Test advances drive the visible badge demo with the local director,
        # rather than waiting for a Backboard response.  The real/live world
        # continues to use the configured AI director.
        payload = b"{}" if is_live_world else b'{"prefer_jev": false}'
        request = Request(
            f"{self.server_base}{endpoint}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=90 if is_live_world else 15) as response:
                return self.send_bytes(response.read(), "application/json")
        except OSError as exc:
            self.send_error(502, f"Could not advance simulation: {exc}")

    def log_message(self, *_: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Shutterdex Web Serial bridge.")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    make_sprites()
    Handler.server_base = args.server.rstrip("/")
    print("Open http://127.0.0.1:8765 in Chrome or Edge.")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()


if __name__ == "__main__":
    main()
