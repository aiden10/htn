"""Translate declarative Shutterdex screens into HTN OS commands.

The firmware has no retained application framebuffer and its text API has no
word wrapping.  This renderer is deliberately server-side: it resolves local
sprite PNGs, prepares precise thumbnail sizes, clips text safely, and emits a
complete ordered command list that can be replayed after a reconnect.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Iterable, Protocol
import unicodedata

from PIL import Image as PillowImage

from badge_ui import Clear, DrawOperation, Image, Leds, Rect, Screen, Text
from htn_gateway import BadgeCommand
from sprite_assets import SpriteStore


class RenderError(ValueError):
    """A logical screen cannot be expressed safely with HTN OS commands."""


class ImageResolver(Protocol):
    """Resolve a logical image source into a small PNG for the HTN image API."""

    def load_png(self, source: str, width: int, height: int) -> bytes:
        """Return an exact-size PNG, or raise :class:`RenderError`."""


@dataclass(frozen=True, slots=True)
class SpriteImageResolver:
    """Resolve local sprite and static Habitat image references for a badge."""

    sprites: SpriteStore
    habitat_directory: Path | None = None

    def load_png(self, source: str, width: int, height: int) -> bytes:
        if source.startswith("sprite://"):
            key = source.removeprefix("sprite://")
            try:
                path = self.sprites.source_path(key)
            except Exception as exc:
                raise RenderError("Invalid sprite reference.") from exc
        elif source.startswith("habitat://"):
            key = source.removeprefix("habitat://")
            if (
                self.habitat_directory is None
                or not key
                or not all(character.islower() or character.isdigit() or character == "_" for character in key)
            ):
                raise RenderError("Invalid static Habitat reference.")
            path = self.habitat_directory / f"{key}.png"
        else:
            raise RenderError("Only local sprite:// and habitat:// sources are permitted.")
        if not path.is_file():
            raise RenderError(f"Image asset {key!r} does not exist.")
        try:
            with PillowImage.open(path) as source_image:
                rgba = source_image.convert("RGBA")
                # These are intentional pixel-art thumbnails. Nearest-neighbor
                # keeps generated sprites crisp when rendered larger than 32px.
                scaled = rgba.resize((width, height), PillowImage.Resampling.NEAREST)
                output = BytesIO()
                scaled.save(output, format="PNG", optimize=True)
                return output.getvalue()
        except OSError as exc:
            raise RenderError(f"Could not read image asset {key!r}.") from exc


def _font_scale(pixel_size: int) -> int:
    """Map UI pixels onto HTN OS's four bitmap font scales."""

    if pixel_size <= 12:
        return 1
    if pixel_size <= 16:
        return 2
    if pixel_size <= 24:
        return 3
    return 4


def _font_width(scale: int) -> int:
    return {1: 6, 2: 8, 3: 12, 4: 16}[scale]


def _ascii(text: str) -> str:
    """Convert badge text into readable printable ASCII.

    HTN OS's bitmap font replaces non-ASCII punctuation and accents with a
    question mark. Normalize common punctuation first, then fold accented
    Latin characters (``Pokémon`` -> ``Pokemon``) instead of exposing that
    fallback glyph on the badge.
    """

    punctuation = str.maketrans(
        {
            "’": "'",
            "‘": "'",
            "“": '"',
            "”": '"',
            "–": "-",
            "—": "-",
            "…": "...",
            "•": "-",
        }
    )
    normalized = unicodedata.normalize("NFKD", text.translate(punctuation))
    return normalized.encode("ascii", errors="ignore").decode("ascii")


def _ellipsize(text: str, count: int) -> str:
    if count <= 0:
        return ""
    if len(text) <= count:
        return text
    if count <= 3:
        return text[:count]
    return text[: count - 3] + "..."


def _scroll_window(text: str, count: int, step: int) -> str:
    """Take a repeatable marquee window without retaining per-label timers."""

    if count <= 0 or len(text) <= count:
        return text
    loop = text + "    "
    offset = step % len(loop)
    repeated = loop + loop
    return repeated[offset : offset + count]


def _text_lines(operation: Text, *, scroll_step: int) -> tuple[str, ...]:
    scale = _font_scale(operation.size)
    columns = 53 if operation.max_width is None else max(1, operation.max_width // _font_width(scale))
    max_lines = operation.max_lines or 1
    raw_lines = _ascii(operation.text).split("\n")
    rendered: list[str] = []
    for line in raw_lines:
        if len(rendered) >= max_lines:
            break
        if operation.scroll and len(line) > columns:
            rendered.append(_scroll_window(line, columns, scroll_step))
        else:
            rendered.append(_ellipsize(line, columns))
    return tuple(rendered[:max_lines]) or ("",)


@dataclass(slots=True)
class ScreenRenderer:
    """Convert one complete UI screen to rate-limited gateway commands.

    Rounded corners cannot be expressed by HTN OS's primitive API. They remain
    an optional visual hint in the logical UI; the renderer produces a clean
    filled rectangle plus a one-pixel stroke when requested.
    """

    image_resolver: ImageResolver
    screen_width: int = 320
    screen_height: int = 240

    def render(self, screen: Screen, *, scroll_step: int = 0) -> tuple[BadgeCommand, ...]:
        commands: list[BadgeCommand] = []
        for operation in screen.operations:
            commands.extend(self._operation_commands(operation, scroll_step=scroll_step))
        return tuple(commands)

    def fingerprint(self, screen: Screen, *, scroll_step: int = 0) -> str:
        """Hash the actual visual plan, useful for redraw coalescing."""

        payload = {
            "screen": screen.to_dict(),
            "scroll_step": scroll_step,
            "size": [self.screen_width, self.screen_height],
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _operation_commands(
        self, operation: DrawOperation, *, scroll_step: int
    ) -> Iterable[BadgeCommand]:
        if isinstance(operation, Clear):
            return (BadgeCommand("clear", {"color": operation.color}),)
        if isinstance(operation, Rect):
            return tuple(self._rect_commands(operation))
        if isinstance(operation, Text):
            return tuple(self._text_commands(operation, scroll_step=scroll_step))
        if isinstance(operation, Image):
            return (self._image_command(operation),)
        if isinstance(operation, Leds):
            return (self._leds_command(operation),)
        raise RenderError(f"Unsupported drawing operation: {type(operation).__name__}")

    def _rect_commands(self, operation: Rect) -> Iterable[BadgeCommand]:
        x = max(0, min(operation.x, self.screen_width))
        y = max(0, min(operation.y, self.screen_height))
        width = max(0, min(operation.width, self.screen_width - x))
        height = max(0, min(operation.height, self.screen_height - y))
        if width == 0 or height == 0:
            return ()
        commands: list[BadgeCommand] = []
        if operation.stroke and operation.stroke_width > 0:
            stroke = min(operation.stroke_width, width // 2, height // 2)
            if stroke:
                commands.append(BadgeCommand("rect", {"x": x, "y": y, "w": width, "h": height, "color": operation.stroke}))
                x, y, width, height = x + stroke, y + stroke, width - stroke * 2, height - stroke * 2
        if width > 0 and height > 0:
            commands.append(BadgeCommand("rect", {"x": x, "y": y, "w": width, "h": height, "color": operation.fill}))
        return tuple(commands)

    def _text_commands(self, operation: Text, *, scroll_step: int) -> Iterable[BadgeCommand]:
        scale = _font_scale(operation.size)
        lines = _text_lines(operation, scroll_step=scroll_step)
        line_height = {1: 12, 2: 16, 3: 24, 4: 32}[scale]
        commands: list[BadgeCommand] = []
        for index, text in enumerate(lines):
            if not text:
                continue
            x = operation.x
            if operation.max_width is not None:
                used = len(text) * _font_width(scale)
                if operation.align == "center":
                    x += max(0, (operation.max_width - used) // 2)
                elif operation.align == "right":
                    x += max(0, operation.max_width - used)
            commands.append(
                BadgeCommand(
                    "text",
                    {
                        "text": text,
                        "x": x,
                        "y": operation.y + index * line_height,
                        "size": scale,
                        "color": operation.color,
                    },
                )
            )
        return tuple(commands)

    def _image_command(self, operation: Image) -> BadgeCommand:
        data = self.image_resolver.load_png(operation.source, operation.width, operation.height)
        # HTN OS's JSON image command takes raw base64, not a data-URL.
        return BadgeCommand(
            "image",
            {
                "image": base64.b64encode(data).decode("ascii"),
                "x": operation.x,
                "y": operation.y,
                "fit": "none",
            },
        )

    @staticmethod
    def _leds_command(operation: Leds) -> BadgeCommand:
        colors = list(operation.colors)
        return BadgeCommand(
            "leds",
            {"leds": [colors[index % len(colors)] for index in range(6)]},
        )


__all__ = [
    "ImageResolver",
    "RenderError",
    "ScreenRenderer",
    "SpriteImageResolver",
]
