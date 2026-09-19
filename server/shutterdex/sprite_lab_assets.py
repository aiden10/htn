"""Generate and install Shutterdex Sprite Lab's LVGL image test assets.

The badge Lua runtime only accepts installed relative .bin files for image
widgets.  This creates three otherwise-identical RGB565A8 images at the three
sizes under investigation, then writes them directly to the Sprite Lab app.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
SERVER_DIR = ROOT.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from badge_bridge import BadgeProtocolError, BadgeSerialBridge  # noqa: E402


ASSET_DIR = ROOT / "assets" / "sprite-lab"
SIZES = (42, 32, 28)


def encode_lvgl_rgb565a8(image: Image.Image) -> bytes:
    """Encode the exact 12-byte-header RGB565A8 format used by IDE icon.bin."""

    rgba = image.convert("RGBA")
    width, height = rgba.size
    rgb, alpha = bytearray(), bytearray()
    for red, green, blue, opacity in rgba.getdata():
        rgb.extend(struct.pack("<H", ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)))
        alpha.append(opacity)
    # LVGL v9's 12-byte binary header is u8 magic, u8 color format, followed
    # by u16 flags, width, height, stride, and reserved fields.
    return struct.pack("<BBHHHHH", 0x19, 0x14, 0, width, height, width * 2, 0) + rgb + alpha


def diagnostic_image(size: int) -> Image.Image:
    """Make a high-contrast image with transparent corners and a pixel motif."""

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = max(2, size // 14)
    margin = unit
    draw.rectangle((margin, margin, size - margin - 1, size - margin - 1), fill=(22, 34, 40, 255))
    draw.rectangle((margin, margin, size - margin - 1, size - margin - 1), outline=(245, 248, 235, 255), width=unit)
    draw.rectangle((size // 2 - unit * 3, size // 2 - unit * 2, size // 2 + unit * 3 - 1, size // 2 + unit * 3 - 1), fill=(85, 212, 148, 255))
    draw.rectangle((size // 2 - unit * 2, size // 2 - unit, size // 2 - 1, size // 2 + unit - 1), fill=(13, 25, 18, 255))
    draw.rectangle((size // 2 + unit, size // 2 - unit, size // 2 + unit * 2 - 1, size // 2 + unit - 1), fill=(13, 25, 18, 255))
    draw.rectangle((size // 2 - unit, size // 2 + unit, size // 2 + unit - 1, size // 2 + unit * 2 - 1), fill=(244, 201, 76, 255))
    return image


def build_assets() -> list[tuple[str, bytes]]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    assets: list[tuple[str, bytes]] = []
    for size in SIZES:
        name = f"rgb565a8_{size}.bin"
        data = encode_lvgl_rgb565a8(diagnostic_image(size))
        (ASSET_DIR / name).write_bytes(data)
        assets.append((name, data))
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and install Shutterdex Sprite Lab images.")
    parser.add_argument("--port", help="Badge USB JTAG/serial COM port, for example COM3.")
    parser.add_argument("--raw-delay", type=float, default=0.8, help="Seconds to wait after READY (default: 0.8).")
    parser.add_argument("--build-only", action="store_true", help="Create local .bin files without touching a badge.")
    args = parser.parse_args()
    if not args.build_only and not args.port:
        parser.error("--port is required unless --build-only is set")
    if args.raw_delay < 0:
        parser.error("--raw-delay must be zero or greater")

    assets = build_assets()
    for name, data in assets:
        print(f"Built {name} ({len(data)} bytes)")
    if args.build_only:
        print(f"Assets written to {ASSET_DIR}")
        return 0

    with BadgeSerialBridge(args.port, "sprite_lab", raw_delay=args.raw_delay) as bridge:
        for name, data in assets:
            print(f"Writing sprite_lab/{name} ({len(data)} bytes)…")
            bridge.put_app_file("sprite_lab", name, data)
    print("Sprite Lab assets installed. Open or reopen Shutterdex Sprite Lab on the badge.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BadgeProtocolError, OSError) as exc:
        print(f"Sprite Lab installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
