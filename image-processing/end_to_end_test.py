"""Run one saved camera capture through Shutterdex and publish it to a badge.

This is the same route a Pokeball capture takes after its JPEG reaches the PC:

    saved JPEG -> POST /pokemon -> vision -> sprite -> test world -> badge files

The script deliberately uses FastAPI's isolated ``/simulation/test`` world.
It never adds a test capture to the live collection.  A connected badge receives
the test-world inbox in both Shutterdex apps, plus the generated sprite in the
Pokédex app.

Examples (from this directory):

    py .\end_to_end_test.py --port COM3
    py .\end_to_end_test.py --capture .\captures\photo.jpg --no-badge
    py .\end_to_end_test.py --port COM3 --no-cache
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import requests

ROOT = Path(__file__).resolve().parent
SERVER_DIR = ROOT.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import generate  # noqa: E402  -- kept beside this script on purpose
from badge_bridge import BadgeProtocolError, BadgeSerialBridge, revision_from  # noqa: E402


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def choose_capture(requested: str | None, capture_dir: Path) -> Path:
    if requested:
        path = Path(requested).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Capture does not exist: {path}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise RuntimeError("Capture must be a JPEG, PNG, or WebP image.")
        return path

    candidates = [
        path for path in capture_dir.glob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    if not candidates:
        raise RuntimeError(f"No camera captures found in {capture_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def response_json(response: requests.Response, step: str) -> dict[str, object]:
    if response.ok:
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{step} returned invalid JSON: {response.text[:500]}") from exc
    raise RuntimeError(f"{step} failed ({response.status_code}): {response.text[:1000]}")


def post_photo(session: requests.Session, server: str, photo: Path, device_id: str) -> dict[str, object]:
    content_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
    with photo.open("rb") as source:
        response = session.post(
            f"{server}/pokemon",
            files={"image": (photo.name, source, content_type)},
            data={"device_id": device_id},
            timeout=45,
        )
    return response_json(response, "Camera-image upload")


def pokemon_payload(creature: dict[str, object]) -> tuple[str, dict[str, object]]:
    pokemon_id = f"mon_test_{uuid4().hex[:12]}"
    required = ("name", "species", "type", "stats", "moves", "flavour", "sprite_prompt", "rarity")
    missing = [field for field in required if field not in creature]
    if missing:
        raise RuntimeError(f"Image-processing result is missing: {', '.join(missing)}")
    return pokemon_id, {"pokemon_id": pokemon_id, **{field: creature[field] for field in required}}


def post_json(
    session: requests.Session, method: str, url: str, step: str, body: dict[str, object] | None = None
) -> dict[str, object]:
    response = session.request(method, url, json=body, timeout=45)
    return response_json(response, step)


def fetch_text(session: requests.Session, url: str, step: str) -> str:
    response = session.get(url, timeout=45)
    if not response.ok:
        raise RuntimeError(f"{step} failed ({response.status_code}): {response.text[:1000]}")
    return response.text


def publish_to_badge(
    *,
    port: str,
    raw_delay: float,
    inbox: str,
    ready: str,
    sprite_key: str | None,
    sprite_bin: bytes | None,
) -> int:
    revision = revision_from(ready)
    if revision_from(inbox) != revision:
        raise BadgeProtocolError("Server returned mismatched inbox and ready revisions.")
    with BadgeSerialBridge(port, "pokedex", raw_delay=raw_delay) as bridge:
        if sprite_key and sprite_bin:
            bridge.put_app_file("pokedex", f"{sprite_key}.bin", sprite_bin)
            bridge.put_app_file("pokedex", "sprites.ready", f"revision={revision}\nsprite|{sprite_key}\n")
        else:
            bridge.put_app_file("pokedex", "sprites.ready", f"revision={revision}\n")
        bridge.put_app_file("pokedex", "inbox.tmp", inbox)
        bridge.put_app_file("pokedex", "inbox.ready", ready)
        bridge.put_app_file("habitat", "inbox.tmp", inbox)
        bridge.put_app_file("habitat", "inbox.ready", ready)
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay a saved Pokeball capture through generation, the test server world, and a badge."
    )
    parser.add_argument("--capture", help="Exact image to replay; defaults to the newest file in captures/.")
    parser.add_argument("--captures", default=str(ROOT / "captures"), help="Folder used when --capture is omitted.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="FastAPI base URL.")
    parser.add_argument("--port", help="Badge USB JTAG/serial COM port (for example COM3).")
    parser.add_argument("--no-badge", action="store_true", help="Stop after verifying the server test-world export.")
    parser.add_argument("--raw-delay", type=float, default=0.5, help="Seconds to wait after each badge READY response.")
    parser.add_argument("--device-id", default="pokeball-e2e-test", help="Device label sent with the camera-style upload.")
    parser.add_argument("--no-cache", action="store_true", help="Force fresh vision/sprite generation instead of reusing this photo's cache.")
    parser.add_argument("--no-sprite", action="store_true", help="Run capture + vision + badge data without paying for sprite generation.")
    parser.add_argument("--out", default=str(ROOT / "end-to-end-runs"), help="Folder for this run's generated JSON and PNG copies.")
    parser.add_argument("--data", default=str(ROOT / "gamedata"), help="Persistent generator registry, cache, and sprite bank.")
    args = parser.parse_args()

    if args.no_badge and args.port:
        parser.error("Use either --port or --no-badge, not both.")
    if not args.no_badge and not args.port:
        parser.error("--port is required unless --no-badge is set.")
    if args.raw_delay < 0:
        parser.error("--raw-delay must be zero or greater.")

    server = args.server.rstrip("/")
    photo = choose_capture(args.capture, Path(args.captures).expanduser().resolve())
    data_dir = Path(args.data).expanduser().resolve()
    run_dir = Path(args.out).expanduser().resolve() / datetime.now().strftime("%Y%m%d-%H%M%S")
    data_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Replaying camera capture: {photo.name}")
    session = requests.Session()
    try:
        uploaded = post_photo(session, server, photo, args.device_id)
        print(f"1/6 uploaded image {uploaded.get('image_id')} ({uploaded.get('size_bytes')} bytes)")

        print("2/6 running the real vision and sprite pipeline…")
        creature = generate.process(
            str(photo), str(run_dir), str(data_dir), cache=not args.no_cache, make_sprite=not args.no_sprite
        )
        print(f"3/6 generated {creature['name']} ({creature['species']})")

        post_json(session, "POST", f"{server}/simulation/test/reset", "Test-world reset")
        pokemon_id, payload = pokemon_payload(creature)
        post_json(
            session,
            "POST",
            f"{server}/simulation/test/pokemon/manual",
            "Test-world Pokemon creation",
            payload,
        )
        print(f"4/6 added {creature['name']} to the isolated test world")

        sprite_key: str | None = None
        sprite_bin: bytes | None = None
        sprite_name = creature.get("sprite")
        if sprite_name:
            sprite_path = data_dir / str(sprite_name)
            if not sprite_path.is_file():
                raise RuntimeError(f"Generated sprite is missing: {sprite_path}")
            with sprite_path.open("rb") as source:
                response = session.post(
                    f"{server}/simulation/test/pokemon/{pokemon_id}/sprite",
                    files={"image": (sprite_path.name, source, "image/png")},
                    timeout=45,
                )
            sprite_result = response_json(response, "Test-world sprite upload")
            sprite_key = str(sprite_result["sprite_key"])
            response = session.get(f"{server}/sprites/{sprite_key}.bin", timeout=45)
            if not response.ok:
                raise RuntimeError(f"Badge-sprite download failed ({response.status_code}): {response.text[:500]}")
            sprite_bin = response.content
            print(f"5/6 prepared sprite {sprite_key}.bin ({len(sprite_bin)} bytes)")
        else:
            print("5/6 sprite generation skipped")

        inbox = fetch_text(session, f"{server}/simulation/test/badge/inbox", "Test badge inbox")
        ready = fetch_text(session, f"{server}/simulation/test/badge/ready", "Test badge ready marker")
        revision = revision_from(ready)
        if revision_from(inbox) != revision:
            raise RuntimeError("Test-world badge export had mismatched revisions.")
        if args.no_badge:
            print(f"6/6 server export verified at revision {revision}; no badge was requested.")
        else:
            published = publish_to_badge(
                port=args.port,
                raw_delay=args.raw_delay,
                inbox=inbox,
                ready=ready,
                sprite_key=sprite_key,
                sprite_bin=sprite_bin,
            )
            print(f"6/6 published test-world revision {published} to Shutterdex Dex and Habitat.")
        print(f"Run artifacts: {run_dir}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BadgeProtocolError, RuntimeError, requests.RequestException) as exc:
        print(f"End-to-end test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
