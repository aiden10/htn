"""Replay one saved capture through the Wi-Fi Shutterdex flow.

This is the HTN OS counterpart to ``end_to_end_test.py``.  It uses a saved
Pokeball capture as though it had just arrived from the camera:

    capture -> POST /pokemon -> vision + sprite -> player-owned Pokemon -> Wi-Fi badge

The last step is server-to-badge: this program never opens a serial port and
never handles the firmware's device token or an HTN app key.  Pair a badge in
the Shutterdex dashboard/API first, then pass only its public HTN ID here when
you want the server to redraw it.

Examples (run from ``image-processing``):

    py .\end_to_end_wifi_test.py --player-id player_abc
    py .\end_to_end_wifi_test.py --player-id player_abc --htn-id A1B2C
    py .\end_to_end_wifi_test.py --player-id player_abc --no-sprite --no-launch
    py .\end_to_end_wifi_test.py --capture .\captures\photo.jpg --player-id player_abc

Prerequisites:

* the FastAPI server is running;
* ``player_abc`` already exists (create it with ``POST /shutterdex/players``);
* if ``--htn-id`` is used, that public HTN ID is already paired to the player.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

try:
    import requests
except ImportError as exc:  # pragma: no cover - environment setup guard
    raise SystemExit("requests is not installed. Run: py -m pip install requests") from exc


ROOT = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def choose_capture(requested: str | None, capture_dir: Path) -> Path:
    """Use an explicit image, or the newest valid saved camera capture."""

    if requested:
        path = Path(requested).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Capture does not exist: {path}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise RuntimeError("Capture must be a JPEG, PNG, or WebP image.")
        return path

    if not capture_dir.is_dir():
        raise RuntimeError(f"Capture folder does not exist: {capture_dir}")
    candidates = [
        path
        for path in capture_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    if not candidates:
        raise RuntimeError(f"No camera captures found in {capture_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def response_json(response: requests.Response, step: str) -> dict[str, Any]:
    """Turn a useful HTTP failure into one concise command-line error."""

    if response.ok:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{step} returned invalid JSON: {response.text[:500]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{step} returned a JSON value instead of an object.")
        return payload
    raise RuntimeError(f"{step} failed ({response.status_code}): {response.text[:1000]}")


def post_camera_ingress(
    session: requests.Session, server: str, photo: Path, device_id: str
) -> dict[str, Any]:
    """Match the real Pokeball camera's multipart upload exactly."""

    content_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
    with photo.open("rb") as source:
        response = session.post(
            f"{server}/pokemon",
            files={"image": (photo.name, source, content_type)},
            # ``device_id`` is retained for the physical camera's provenance.
            # The current ingress endpoint may ignore the optional form field.
            data={"device_id": device_id},
            timeout=45,
        )
    return response_json(response, "Camera-image upload")


def generated_pokemon_payload(creature: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Convert the generator's validated creature into the Wi-Fi API shape."""

    required = (
        "name", "species", "type", "stats", "moves", "battle_natures",
        "flavour", "sprite_prompt", "rarity",
    )
    missing = [field for field in required if field not in creature]
    if missing:
        raise RuntimeError(f"Image-processing result is missing: {', '.join(missing)}")

    pokemon_id = f"mon_e2e_{uuid4().hex[:12]}"
    pokemon = {"pokemon_id": pokemon_id, **{field: creature[field] for field in required}}
    return pokemon_id, {"pokemon": pokemon}


def post_json(
    session: requests.Session,
    method: str,
    url: str,
    step: str,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    response = session.request(method, url, json=body, timeout=45)
    return response_json(response, step)


def create_owned_pokemon(
    session: requests.Session,
    server: str,
    player_id: str,
    creature: Mapping[str, Any],
    captured_by_htn_id: str | None,
) -> dict[str, Any]:
    """Add the generated creature to exactly one player's authoritative Dex."""

    _generated_id, body = generated_pokemon_payload(creature)
    if captured_by_htn_id:
        body["captured_by_htn_id"] = captured_by_htn_id
    payload = post_json(
        session,
        "POST",
        f"{server}/shutterdex/players/{player_id}/pokemon",
        "Player-owned Pokemon creation",
        body,
    )
    pokemon = payload.get("pokemon")
    if not isinstance(pokemon, dict) or not isinstance(pokemon.get("pokemon_id"), str):
        raise RuntimeError("Player-owned Pokemon creation returned no pokemon_id.")
    return pokemon


def upload_sprite(
    session: requests.Session,
    server: str,
    pokemon_id: str,
    sprite_path: Path,
) -> dict[str, Any]:
    """Upload the source PNG; the server prepares its own HTN render asset."""

    content_type = mimetypes.guess_type(sprite_path.name)[0] or "image/png"
    with sprite_path.open("rb") as source:
        response = session.post(
            f"{server}/shutterdex/pokemon/{pokemon_id}/sprite",
            files={"image": (sprite_path.name, source, content_type)},
            timeout=45,
        )
    return response_json(response, "Sprite upload")


def generated_sprite_path(creature: Mapping[str, Any], data_dir: Path) -> Path | None:
    """Find the safe, local source sprite recorded by ``generate.process``."""

    sprite_name = creature.get("sprite")
    if not isinstance(sprite_name, str) or not sprite_name:
        return None
    # The generator records just a file name.  Keeping that invariant here
    # avoids turning a stale cache value into an arbitrary file upload.
    sprite_path = (data_dir / Path(sprite_name).name).resolve()
    try:
        sprite_path.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("Generated sprite path escaped the sprite data folder.") from exc
    if not sprite_path.is_file():
        raise RuntimeError(f"Generated sprite is missing: {sprite_path}")
    return sprite_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay a saved Pokeball capture into a player-owned Wi-Fi Shutterdex collection."
    )
    parser.add_argument("--player-id", required=True, help="Existing Shutterdex player ID that will own this Pokemon.")
    parser.add_argument(
        "--htn-id",
        help="Optional public HTN badge ID to redraw after creation; no badge credential is accepted here.",
    )
    parser.add_argument(
        "--captured-by-htn-id",
        help="Optional public HTN ID recorded as the capture badge (defaults to --htn-id when present).",
    )
    parser.add_argument("--capture", help="Exact image to replay; defaults to the newest file in captures/.")
    parser.add_argument("--captures", default=str(ROOT / "captures"), help="Folder used when --capture is omitted.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="FastAPI base URL.")
    parser.add_argument(
        "--device-id",
        default="pokeball-e2e-wifi-test",
        help="Camera provenance label sent with the ingress upload.",
    )
    parser.add_argument("--no-cache", action="store_true", help="Force fresh vision/sprite generation instead of reusing this photo's cache.")
    parser.add_argument("--no-sprite", action="store_true", help="Skip sprite generation and its upload.")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Do not redraw a badge, even when --htn-id is supplied.",
    )
    parser.add_argument("--out", default=str(ROOT / "end-to-end-runs"), help="Folder for this run's generated JSON and PNG copies.")
    parser.add_argument("--data", default=str(ROOT / "gamedata"), help="Persistent generator registry, cache, and sprite bank.")
    args = parser.parse_args()

    if args.no_launch and not args.htn_id:
        parser.error("--no-launch only applies when --htn-id is supplied.")
    if args.captured_by_htn_id and not args.htn_id:
        # Captures can be attributed without an immediate redraw, but make the
        # explicit choice visible rather than silently losing an ID typo.
        parser.error("--captured-by-htn-id requires --htn-id; use --no-launch to skip the redraw.")

    server = args.server.rstrip("/")
    photo = choose_capture(args.capture, Path(args.captures).expanduser().resolve())
    data_dir = Path(args.data).expanduser().resolve()
    run_dir = Path(args.out).expanduser().resolve() / datetime.now().strftime("%Y%m%d-%H%M%S")
    data_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    capture_badge = args.captured_by_htn_id or args.htn_id

    # Imported after argparse so ``--help`` does not require the generator's
    # Backboard/Pillow dependencies to be initialized.
    import generate  # noqa: PLC0415

    print(f"Replaying camera capture: {photo.name}")
    session = requests.Session()
    try:
        ingress = post_camera_ingress(session, server, photo, args.device_id)
        print(f"1/5 uploaded image {ingress.get('image_id')} ({ingress.get('size_bytes')} bytes)")

        print("2/5 running the real vision and sprite pipeline…")
        creature = generate.process(
            str(photo),
            str(run_dir),
            str(data_dir),
            cache=not args.no_cache,
            make_sprite=not args.no_sprite,
        )
        print(f"3/5 generated {creature['name']} ({creature['species']})")

        pokemon = create_owned_pokemon(
            session,
            server,
            args.player_id,
            creature,
            capture_badge,
        )
        pokemon_id = str(pokemon["pokemon_id"])
        print(f"4/5 added {pokemon['name']} to player {args.player_id}")

        if args.no_sprite:
            print("5/5 sprite generation and upload skipped")
        else:
            sprite_path = generated_sprite_path(creature, data_dir)
            if sprite_path is None:
                raise RuntimeError("The generator completed without a sprite. Re-run without --no-sprite.")
            uploaded_sprite = upload_sprite(session, server, pokemon_id, sprite_path)
            print(f"5/5 uploaded sprite {uploaded_sprite.get('sprite_key')} from {sprite_path.name}")

        if args.htn_id and not args.no_launch:
            launch = post_json(
                session,
                "POST",
                f"{server}/shutterdex/badges/{args.htn_id}/launch",
                "Paired badge launch",
            )
            render = launch.get("render")
            queued = render.get("queued_commands") if isinstance(render, dict) else None
            print(f"Badge {args.htn_id} redraw queued ({queued} commands).")
        elif args.htn_id:
            print(f"Badge {args.htn_id} was not redrawn (--no-launch).")
        else:
            print("No badge was requested; the Pokemon is stored in the player's Wi-Fi collection.")

        print(f"Run artifacts: {run_dir}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, requests.RequestException) as exc:
        print(f"Wi-Fi end-to-end test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
