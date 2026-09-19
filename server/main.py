from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse

from badge_export import badge_snapshot_text
from models import Pokemon, SimulationTickRequest, SimulationTickResult, WorldSnapshot
from simulation import SimulationService, WorldStore
from sprite_assets import SpriteError, SpriteStore


SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = SERVER_DIR / "data"
UPLOAD_DIR = SERVER_DIR / "uploads"
SPRITE_DIR = DATA_DIR / "sprites"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SPRITE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up the persistent world and optional local badge-mirror publisher."""

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    mirror_setting = os.getenv("BADGE_INBOX_DIR", "").strip()
    app.state.simulation = SimulationService(
        WorldStore(DATA_DIR / "world.sqlite3"),
        backboard_api_key=os.getenv("BACKBOARD_API_KEY"),
        badge_mirror_dir=Path(mirror_setting) if mirror_setting else None,
    )
    # The test service deliberately has a different database and no badge mirror.
    # It can exercise Jev without changing a real collection or connected badge.
    app.state.test_simulation = SimulationService(
        WorldStore(DATA_DIR / "test-world.sqlite3"),
        backboard_api_key=os.getenv("BACKBOARD_API_KEY"),
    )
    app.state.sprites = SpriteStore(SPRITE_DIR)
    try:
        yield
    finally:
        app.state.simulation.store.close()
        app.state.test_simulation.store.close()


app = FastAPI(
    title="Pokemon Generator and Simulation Server",
    description=(
        "Receives Poké Ball images, maintains the authoritative Pokemon world, "
        "and exports complete badge snapshots."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


def simulation_service(request: Request) -> SimulationService:
    return request.app.state.simulation


def sprite_store(request: Request) -> SpriteStore:
    return request.app.state.sprites


def test_simulation_service(request: Request) -> SimulationService:
    return request.app.state.test_simulation


async def save_sprite_for_service(
    pokemon_id: str,
    request: Request,
    image: UploadFile,
    service: SimulationService,
) -> dict[str, object]:
    """Store a sprite, then attach it to a Pokemon in the chosen world."""

    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Sprite must be a JPEG, PNG, or WebP image.",
        )

    try:
        image_bytes = await image.read(MAX_SPRITE_BYTES + 1)
    finally:
        await image.close()
    if len(image_bytes) > MAX_SPRITE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Sprite image must be 5 MiB or smaller.",
        )

    world = await service.snapshot()
    if not any(pokemon.pokemon_id == pokemon_id for pokemon in world.pokemon):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pokemon was not found.")
    try:
        key = sprite_store(request).save_png(pokemon_id, image_bytes)
    except SpriteError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    world = await service.attach_sprite(pokemon_id, key)
    return {
        "pokemon_id": pokemon_id,
        "sprite_key": key,
        "badge_sprite_url": f"/sprites/{key}.bin",
        "world_revision": world.revision,
    }


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {
        "service": "pokemon-generator-and-simulation",
        "upload_endpoint": "POST /pokemon",
        "badge_export": "GET /badge/inbox",
    }


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/bridge", include_in_schema=False)
async def browser_badge_bridge() -> FileResponse:
    """Serve the local Chrome/Web Serial publisher from the same server origin."""

    return FileResponse(SERVER_DIR / "badge_web_bridge.html", media_type="text/html")


@app.post("/pokemon", status_code=status.HTTP_201_CREATED, tags=["pokemon"])
async def receive_pokemon_image(image: UploadFile = File(...)) -> dict[str, object]:
    """Accept a Poké Ball image for a later Pokemon-generation worker."""

    content_type = (image.content_type or "").lower()
    extension = ALLOWED_IMAGE_TYPES.get(content_type)
    if extension is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG, or WebP image.",
        )

    image_id = uuid4().hex
    destination = UPLOAD_DIR / f"{image_id}{extension}"
    total_bytes = 0

    try:
        with destination.open("wb") as output:
            while chunk := await image.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Image must be 10 MiB or smaller.",
                    )
                output.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to store uploaded image.",
        ) from exc
    finally:
        await image.close()

    return {
        "image_id": image_id,
        "status": "received",
        "filename": image.filename,
        "content_type": content_type,
        "size_bytes": total_bytes,
        "next_step": "Generate a Pokemon profile, then POST it to /pokemon/manual.",
    }


@app.post(
    "/pokemon/manual",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    tags=["pokemon"],
)
async def add_pokemon(pokemon: Pokemon, request: Request) -> WorldSnapshot:
    """Add a generated Pokemon profile to the canonical simulation world."""

    try:
        return await simulation_service(request).add_pokemon(pokemon)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.post(
    "/simulation/test/pokemon/manual",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    tags=["simulation test"],
)
async def add_test_pokemon(pokemon: Pokemon, request: Request) -> WorldSnapshot:
    """Add a generated Pokemon to the disposable end-to-end test world."""

    try:
        return await test_simulation_service(request).add_pokemon(pokemon)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.post(
    "/simulation/demo",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    tags=["simulation"],
)
async def seed_simulation_demo(request: Request) -> WorldSnapshot:
    """Seed Mugmite and Spriglet once so the full producer-consumer path is testable."""

    return await simulation_service(request).seed_demo_world()


@app.get(
    "/world",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    tags=["simulation"],
)
async def get_world(request: Request) -> WorldSnapshot:
    return await simulation_service(request).snapshot()


@app.post(
    "/simulation/tick",
    response_model=SimulationTickResult,
    response_model_by_alias=True,
    tags=["simulation"],
)
async def simulation_tick(
    request: Request, body: SimulationTickRequest | None = None
) -> SimulationTickResult:
    """Commit one new event and a fully synchronized world revision."""

    try:
        world, event, decision = await simulation_service(request).tick(
            prefer_jev=True if body is None else body.prefer_jev
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SimulationTickResult(
        world=world,
        event=event,
        director_used=decision.source,
        director_note=decision.note,
    )


@app.post(
    "/simulation/test/reset",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    tags=["simulation test"],
)
async def reset_simulation_test(request: Request) -> WorldSnapshot:
    """Reset an isolated dummy world; production Pokemon are untouched."""

    return await test_simulation_service(request).reset_test_world()


@app.post("/simulation/test/run", tags=["simulation test"])
async def run_simulation_test(
    request: Request, body: SimulationTickRequest | None = None
) -> dict[str, object]:
    """Reset dummy data, execute one real simulation tick, and report Jev status."""

    service = test_simulation_service(request)
    await service.reset_test_world()
    world, event, decision = await service.tick(
        prefer_jev=True if body is None else body.prefer_jev
    )
    return {
        "isolated_test_world": True,
        "backboard_configured": bool(service.backboard_api_key),
        "backboard_verified": decision.source == "jev",
        "director_used": decision.source,
        "director_note": decision.note,
        "event": event.model_dump(mode="json"),
        "world_revision": world.revision,
        "badge_inbox": "/simulation/test/badge/inbox",
        "badge_ready": "/simulation/test/badge/ready",
    }


@app.post("/simulation/test/tick", tags=["simulation test"])
async def tick_simulation_test(
    request: Request, body: SimulationTickRequest | None = None
) -> dict[str, object]:
    """Advance the persistent dummy world once without resetting its story."""

    service = test_simulation_service(request)
    if not (await service.snapshot()).pokemon:
        await service.reset_test_world()
    world, event, decision = await service.tick(
        prefer_jev=True if body is None else body.prefer_jev
    )
    return {
        "isolated_test_world": True,
        "director_used": decision.source,
        "director_note": decision.note,
        "event": event.model_dump(mode="json"),
        "world_revision": world.revision,
    }


@app.get(
    "/simulation/test/world",
    response_model=WorldSnapshot,
    response_model_by_alias=True,
    tags=["simulation test"],
)
async def get_simulation_test_world(request: Request) -> WorldSnapshot:
    return await test_simulation_service(request).snapshot()


@app.get("/badge/inbox", response_class=PlainTextResponse, tags=["badge"])
async def badge_inbox(request: Request) -> str:
    """Return the complete inbox.tmp text for the bridge to publish to the badge."""

    return badge_snapshot_text(await simulation_service(request).snapshot())


@app.get("/badge/ready", response_class=PlainTextResponse, tags=["badge"])
async def badge_ready(request: Request) -> str:
    world = await simulation_service(request).snapshot()
    return f"revision={world.revision}\n"


@app.get("/simulation/test/badge/inbox", response_class=PlainTextResponse, tags=["simulation test"])
async def simulation_test_badge_inbox(request: Request) -> str:
    return badge_snapshot_text(await test_simulation_service(request).snapshot())


@app.get("/simulation/test/badge/ready", response_class=PlainTextResponse, tags=["simulation test"])
async def simulation_test_badge_ready(request: Request) -> str:
    world = await test_simulation_service(request).snapshot()
    return f"revision={world.revision}\n"


@app.post("/pokemon/{pokemon_id}/sprite", tags=["sprites"])
async def upload_sprite(
    pokemon_id: str,
    request: Request,
    image: UploadFile = File(...),
) -> dict[str, object]:
    """Store a generated sprite PNG and prepare its 32x32 LVGL .bin companion."""

    return await save_sprite_for_service(pokemon_id, request, image, simulation_service(request))


@app.post("/simulation/test/pokemon/{pokemon_id}/sprite", tags=["simulation test"])
async def upload_test_sprite(
    pokemon_id: str,
    request: Request,
    image: UploadFile = File(...),
) -> dict[str, object]:
    """Attach a generated sprite to the isolated end-to-end test world."""

    return await save_sprite_for_service(
        pokemon_id, request, image, test_simulation_service(request)
    )


@app.get("/sprites/{sprite_key}.bin", response_class=FileResponse, tags=["sprites"])
async def download_badge_sprite(sprite_key: str, request: Request) -> FileResponse:
    try:
        path = sprite_store(request).badge_path(sprite_key)
    except SpriteError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite was not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
