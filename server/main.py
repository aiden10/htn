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
    app.state.sprites = SpriteStore(SPRITE_DIR)
    try:
        yield
    finally:
        app.state.simulation.store.close()


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


@app.get("/badge/inbox", response_class=PlainTextResponse, tags=["badge"])
async def badge_inbox(request: Request) -> str:
    """Return the complete inbox.tmp text for the bridge to publish to the badge."""

    return badge_snapshot_text(await simulation_service(request).snapshot())


@app.get("/badge/ready", response_class=PlainTextResponse, tags=["badge"])
async def badge_ready(request: Request) -> str:
    world = await simulation_service(request).snapshot()
    return f"revision={world.revision}\n"


@app.post("/pokemon/{pokemon_id}/sprite", tags=["sprites"])
async def upload_sprite(
    pokemon_id: str,
    request: Request,
    image: UploadFile = File(...),
) -> dict[str, object]:
    """Store a generated sprite PNG and prepare its 32x32 LVGL .bin companion."""

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

    service = simulation_service(request)
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
        "note": "The current text-only IDE import still needs a binary-capable bridge to install this .bin on the badge.",
    }


@app.get("/sprites/{sprite_key}.bin", response_class=FileResponse, tags=["sprites"])
async def download_badge_sprite(sprite_key: str, request: Request) -> FileResponse:
    try:
        path = sprite_store(request).badge_path(sprite_key)
    except SpriteError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite was not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
