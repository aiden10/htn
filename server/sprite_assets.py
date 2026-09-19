"""Sprite storage and conversion for a future binary-capable badge bridge."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import struct

from PIL import Image, ImageOps, UnidentifiedImageError


LVGL_IMAGE_MAGIC = 0x19
LVGL_COLOR_FORMAT_RGB565A8 = 14
BADGE_SPRITE_SIZE = 32
_KEY_RE = re.compile(r"^[a-z0-9_-]{3,64}$")


class SpriteError(ValueError):
    pass


class SpriteStore:
    """Keep source PNGs and 32x32 LVGL RGB565A8 files side by side."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_dir = root / "source"
        self.badge_dir = root / "badge"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.badge_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sprite_key_for(pokemon_id: str) -> str:
        key = f"{pokemon_id}_v1"
        if not _KEY_RE.fullmatch(key):
            raise SpriteError("Pokemon ID cannot be used as a sprite key.")
        return key

    def badge_path(self, sprite_key: str) -> Path:
        if not _KEY_RE.fullmatch(sprite_key):
            raise SpriteError("Invalid sprite key.")
        return self.badge_dir / f"{sprite_key}.bin"

    def save_png(self, pokemon_id: str, image_bytes: bytes) -> str:
        """Validate a PNG/WebP/JPEG image and generate its badge-ready copy."""

        if not image_bytes:
            raise SpriteError("Sprite image was empty.")
        try:
            image = Image.open(BytesIO(image_bytes))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise SpriteError("Sprite must be a readable image.") from exc

        key = self.sprite_key_for(pokemon_id)
        rgba = ImageOps.fit(
            image.convert("RGBA"),
            (BADGE_SPRITE_SIZE, BADGE_SPRITE_SIZE),
            method=Image.Resampling.LANCZOS,
        )
        rgba.save(self.source_dir / f"{key}.png", format="PNG")
        self.badge_path(key).write_bytes(to_lvgl_rgb565a8(rgba))
        return key


def to_lvgl_rgb565a8(image: Image.Image) -> bytes:
    """Encode a 32x32 RGBA image in the LVGL binary format used by the badge."""

    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width != BADGE_SPRITE_SIZE or height != BADGE_SPRITE_SIZE:
        raise SpriteError("Badge sprites must be 32 by 32 pixels.")

    rgb565 = bytearray()
    alpha = bytearray()
    for red, green, blue, opacity in rgba.getdata():
        packed = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
        rgb565.extend(struct.pack("<H", packed))
        alpha.append(opacity)

    header = struct.pack(
        "<IBBHHH",
        LVGL_IMAGE_MAGIC,
        LVGL_COLOR_FORMAT_RGB565A8,
        0,
        width,
        height,
        width * 2,
    )
    return header + rgb565 + alpha
