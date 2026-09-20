"""Generate the eight *static* Shutterdex Habitat backgrounds once.

This is an asset build step, never part of the FastAPI request path or a
Habitat turn. It reads ``BACKBOARD_API_KEY`` from ``server/.env`` and writes
small pixel-art PNGs that the runtime can reuse indefinitely.

Run from the repository root:

    py server/generate_habitat_backgrounds.py

Use ``--force`` only when deliberately replacing the entire art set.
"""

from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import os
from pathlib import Path
from typing import Any

from backboard import BackboardClient
from dotenv import load_dotenv
from PIL import Image
import requests


SERVER_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SERVER_DIR / "assets" / "habitat"
MASTER_PATH = OUTPUT_DIR / "_master_sunrise.png"
BADGE_WIDTH = 304
BADGE_HEIGHT = 124
IMAGE_PROVIDER = "openrouter"
IMAGE_MODEL = "google/gemini-3.1-flash-image"
IMAGE_CONFIG = {"resolution": "1K", "aspect_ratio": "16:9"}

PHASES: tuple[tuple[str, str], ...] = (
    ("sunrise", "sunrise: peach and lavender sky, the sun just appearing at the far-left horizon"),
    ("early_morning", "early morning: pale blue sky, cool soft light, small low sun"),
    ("late_morning", "late morning: clear blue sky, bright but gentle sunlight"),
    ("noon", "noon: vivid clean blue sky, strongest direct daylight, short shadows"),
    ("afternoon", "afternoon: warm blue sky, sun angled westward, longer soft shadows"),
    ("golden_hour", "golden hour: amber and coral light, long warm shadows"),
    ("dusk", "dusk: violet and indigo sky, orange fading at the horizon, first stars"),
    ("night", "night: deep navy sky, crescent moon, restrained tiny stars, cool moonlit ground"),
)

MASTER_PROMPT = """Pixel-art game background for a creature habitat, no characters and no text.
Wide landscape from a fixed straight-on side-view perspective: a grassy clearing with an open
lower-centre area where game creatures will stand; a small pond at the left; a large old tree
and dark shrubbery at the right; distant wooded hills across the horizon. Crisp 16-bit-era
pixel art, large intentional pixel clusters, 32-colour palette, calm cozy monster-collecting
game atmosphere. Keep the entire composition inside a wide central crop; do not add any UI,
buildings, people, Pokemon, logos, signs, or words. Time of day: {time_of_day}."""

EDIT_PROMPT = """Use the supplied image as the exact composition reference. Change only the
time of day, lighting, sky, shadows, and celestial details. Keep every landscape feature in the
same position and preserve the same pixel-art style, crop, camera angle, pond, tree, hills,
and open lower-centre creature area. No characters, Pokemon, UI, text, logos, signs, or words.
Target time: {time_of_day}."""


def _download(url: str) -> bytes:
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    return response.content


def _to_badge_asset(raw: bytes, destination: Path) -> None:
    """Centre-crop, pixelate, and palette-limit one provider image for Canvas."""

    with Image.open(BytesIO(raw)) as source:
        image = source.convert("RGBA")
        source_width, source_height = image.size
        target_ratio = BADGE_WIDTH / BADGE_HEIGHT
        source_ratio = source_width / source_height
        if source_ratio > target_ratio:
            crop_width = round(source_height * target_ratio)
            left = (source_width - crop_width) // 2
            image = image.crop((left, 0, left + crop_width, source_height))
        else:
            crop_height = round(source_width / target_ratio)
            top = (source_height - crop_height) // 2
            image = image.crop((0, top, source_width, top + crop_height))
        # Build an intentional 2x pixel grid then palette-limit it. This keeps
        # a generated source visually coherent with the badge sprites and
        # makes each Base64 Canvas command much smaller.
        image = image.resize((BADGE_WIDTH // 2, BADGE_HEIGHT // 2), Image.Resampling.LANCZOS)
        image = image.convert("RGB").quantize(
            colors=32, method=Image.Quantize.MEDIANCUT
        ).convert("RGBA")
        image = image.resize((BADGE_WIDTH, BADGE_HEIGHT), Image.Resampling.NEAREST)
        image.save(destination, format="PNG", optimize=True)


async def _generate(
    client: BackboardClient, *, prompt: str, input_image: Path | None
) -> bytes:
    response = await client.send_message(
        prompt,
        operation="generate_image",
        input_image=input_image,
        image_model_provider=IMAGE_PROVIDER,
        image_model_name=IMAGE_MODEL,
        image_config=IMAGE_CONFIG,
    )
    media: list[dict[str, Any]] = list(response.generated_media or [])
    if not media or not isinstance(media[0].get("url"), str):
        raise RuntimeError("Backboard did not return a generated Habitat image.")
    return await asyncio.to_thread(_download, media[0]["url"])


async def build_assets(*, force: bool) -> None:
    load_dotenv(SERVER_DIR / ".env")
    api_key = os.getenv("BACKBOARD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BACKBOARD_API_KEY is not configured in server/.env.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = tuple(OUTPUT_DIR / f"{phase}.png" for phase, _ in PHASES)
    if not force and all(path.is_file() for path in targets):
        print("All eight static Habitat backgrounds already exist; no generation requested.")
        return

    async with BackboardClient(api_key=api_key, timeout=600) as client:
        if force or not MASTER_PATH.is_file():
            print("Generating the static sunrise master…")
            raw_master = await _generate(
                client,
                prompt=MASTER_PROMPT.format(time_of_day=PHASES[0][1]),
                input_image=None,
            )
            MASTER_PATH.write_bytes(raw_master)

        for phase, time_of_day in PHASES:
            destination = OUTPUT_DIR / f"{phase}.png"
            if destination.is_file() and not force:
                print(f"Keeping {destination.name}")
                continue
            if phase == "sunrise":
                raw = MASTER_PATH.read_bytes()
            else:
                print(f"Generating static {phase.replace('_', ' ')}…")
                raw = await _generate(
                    client,
                    prompt=EDIT_PROMPT.format(time_of_day=time_of_day),
                    input_image=MASTER_PATH,
                )
            _to_badge_asset(raw, destination)
            print(f"Wrote {destination.relative_to(SERVER_DIR)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deliberately regenerate and replace all eight static PNGs.",
    )
    args = parser.parse_args()
    asyncio.run(build_assets(force=args.force))


if __name__ == "__main__":
    main()
