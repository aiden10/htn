"""Sprite storage and conversion for a future binary-capable badge bridge."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import struct

from PIL import Image, ImageOps, UnidentifiedImageError


LVGL_IMAGE_MAGIC = 0x19
# This is the enum value 0x14 (decimal 20), not decimal 14.  Decimal 14 is
# LV_COLOR_FORMAT_A8, which makes LVGL interpret the RGB bytes as an alpha map.
LVGL_COLOR_FORMAT_RGB565A8 = 0x14
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
        self.rebuild_badge_assets()

    def rebuild_badge_assets(self) -> None:
        """Re-encode persisted PNGs when the on-badge binary format changes."""

        for source in self.source_dir.glob("*.png"):
            try:
                with Image.open(source) as image:
                    rgba = ImageOps.fit(
                        image.convert("RGBA"),
                        (BADGE_SPRITE_SIZE, BADGE_SPRITE_SIZE),
                        method=Image.Resampling.LANCZOS,
                    )
                self.badge_path(source.stem).write_bytes(to_lvgl_rgb565a8(rgba))
            except (OSError, SpriteError):
                # A damaged cache entry should not prevent the API from
                # starting; the next successful sprite upload replaces it.
                continue

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

    def source_path(self, sprite_key: str) -> Path:
        """Return the validated PNG source used by the HTN OS renderer."""

        if not _KEY_RE.fullmatch(sprite_key):
            raise SpriteError("Invalid sprite key.")
        return self.source_dir / f"{sprite_key}.png"

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

    def delete(self, sprite_key: str) -> None:
        """Remove a released Pokemon's private source and badge sprite cache."""

        self.source_path(sprite_key).unlink(missing_ok=True)
        self.badge_path(sprite_key).unlink(missing_ok=True)


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

    # LVGL v9 binary header: u8 magic, u8 color format, then five u16s
    # (flags, width, height, stride, reserved). It is 12 bytes in total.
    # The previous encoder had the same length but put fields at wrong offsets,
    # resulting in a valid image widget with a blank decoded bitmap.
    header = struct.pack(
        "<BBHHHHH",
        LVGL_IMAGE_MAGIC,
        LVGL_COLOR_FORMAT_RGB565A8,
        0,
        width,
        height,
        width * 2,
        0,
    )
    return header + rgb565 + alpha
