from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse
from dotenv import load_dotenv

from badge_export import badge_snapshot_text
from badge_renderer import ScreenRenderer, SpriteImageResolver
from badge_store import Badge, BadgeStore, ConflictError, pokemon_from_dict
from badge_ui import BadgeUi
from battle_service import BattleResolutionResult, BattleService
from credential_vault import CredentialError, FernetCredentialVault, UnavailableCredentialVault
from htn_gateway import HTNBadgeGateway, HTNServiceWebSocketTransport, InMemoryBadgeTransport
from models import Pokemon, SimulationTickRequest, SimulationTickResult, WorldSnapshot
from player_simulation import PlayerSimulationService
from shutterdex_api import router as shutterdex_router
from shutterdex_runtime import ShutterdexRuntime
from simulation import SimulationService, WorldStore
from sprite_assets import SpriteError, SpriteStore


SERVER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVER_DIR.parent
IMAGE_PROCESSING_DIR = PROJECT_DIR / "image-processing"
load_dotenv(SERVER_DIR / ".env")
DATA_DIR = SERVER_DIR / "data"
UPLOAD_DIR = SERVER_DIR / "uploads"
SPRITE_DIR = DATA_DIR / "sprites"
HABITAT_ASSET_DIR = SERVER_DIR / "assets" / "habitat"
POKEBALL_CAPTURE_DIR = IMAGE_PROCESSING_DIR / "captures"
POKEBALL_OUTPUT_DIR = IMAGE_PROCESSING_DIR / "creatures"
POKEBALL_GAME_DATA_DIR = IMAGE_PROCESSING_DIR / "gamedata"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_POKEBALL_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SPRITE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

LOGGER = logging.getLogger(__name__)


def make_badge_gateway() -> HTNBadgeGateway:
    """Choose the Wi-Fi badge transport without changing legacy USB paths.

    The HTN app WebSocket is the normal runtime. Set
    ``SHUTTERDEX_BADGE_TRANSPORT=memory`` for local dashboard/UI testing with
    no physical badge. The firmware's device token is never read by this
    server.
    """

    mode = os.getenv("SHUTTERDEX_BADGE_TRANSPORT", "htn").strip().lower()
    if mode in {"memory", "local", "test"}:
        transport = InMemoryBadgeTransport()
    elif mode in {"htn", "wifi", "websocket"}:
        transport = HTNServiceWebSocketTransport()
    else:
        raise RuntimeError(
            "SHUTTERDEX_BADGE_TRANSPORT must be 'memory' or 'htn'."
        )
    return HTNBadgeGateway(transport)


def make_credential_vault() -> FernetCredentialVault | UnavailableCredentialVault:
    """Avoid storing a Wi-Fi app key until deployment encryption is configured."""

    try:
        return FernetCredentialVault.from_environment()
    except CredentialError as exc:
        LOGGER.warning("Wi-Fi badge pairing is disabled: %s", exc)
        return UnavailableCredentialVault()


def configured_badges() -> tuple[tuple[str, str], ...]:
    """Read per-badge HTN IDs and app keys from ``SHUTTERDEX_BADGES``.

    The on-device key is normally different for every badge. JSON avoids a
    fragile separator format because app keys may contain any printable
    character. A deliberately shared key may simply be repeated in entries.
    """

    raw = os.getenv("SHUTTERDEX_BADGES", "").strip()
    if not raw:
        return ()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SHUTTERDEX_BADGES must be a JSON list.") from exc
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("SHUTTERDEX_BADGES must be a non-empty JSON list.")

    configured: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RuntimeError("Each SHUTTERDEX_BADGES entry must be an object.")
        htn_id = entry.get("htn_id")
        app_key = entry.get("app_key")
        if not isinstance(htn_id, str) or not htn_id.strip():
            raise RuntimeError("Each configured badge needs a non-empty htn_id.")
        if not isinstance(app_key, str) or not app_key:
            raise RuntimeError("Each configured badge needs an app_key.")
        htn_id = htn_id.strip()
        if htn_id in seen_ids:
            raise RuntimeError("SHUTTERDEX_BADGES cannot contain duplicate HTN-IDs.")
        seen_ids.add(htn_id)
        configured.append((htn_id, app_key))
    return tuple(configured)


def configured_player_id(htn_id: str) -> str:
    """Return a stable player ID without assuming an HTN-ID format."""

    digest = sha256(htn_id.encode("utf-8")).hexdigest()[:20]
    return f"configured_{digest}"


def configured_pokeball_badge_id() -> str:
    """Return the one badge currently receiving physical Poké Ball captures."""

    htn_id = os.getenv("POKEBALL_BADGE_ID", "").strip()
    if not htn_id:
        raise RuntimeError(
            "POKEBALL_BADGE_ID must name the connected badge receiving camera captures."
        )
    return htn_id


def pokeball_capture_badge(app: FastAPI) -> Badge:
    """Resolve the configured capture badge and its current player owner."""

    badge = app.state.shutterdex_store.require_badge_by_htn_id(
        configured_pokeball_badge_id()
    )
    if badge.player_id is None:
        raise RuntimeError("POKEBALL_BADGE_ID must refer to a badge assigned to a player.")
    return badge


def process_pokeball_photo(photo_path: Path) -> dict[str, object]:
    """Run the proven image-processing pipeline without importing it at boot."""

    module_path = str(IMAGE_PROCESSING_DIR)
    if module_path not in sys.path:
        sys.path.insert(0, module_path)
    import generate  # Imported lazily: it requires the vision/image dependencies.

    POKEBALL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    POKEBALL_GAME_DATA_DIR.mkdir(parents=True, exist_ok=True)
    creature = generate.process(
        str(photo_path), str(POKEBALL_OUTPUT_DIR), str(POKEBALL_GAME_DATA_DIR)
    )
    if not isinstance(creature, Mapping):
        raise RuntimeError("Image pipeline returned an invalid Pokemon record.")
    return dict(creature)


def pokeball_profile(creature: Mapping[str, object]) -> dict[str, object]:
    """Project image-pipeline output onto the durable Pokemon schema."""

    fields = (
        "name",
        "species",
        "type",
        "stats",
        "moves",
        "battle_natures",
        "flavour",
        "sprite_prompt",
        "rarity",
    )
    missing = [field for field in fields if field not in creature]
    if missing:
        raise RuntimeError(
            "Image pipeline did not provide required fields: " + ", ".join(missing)
        )
    # Pydantic enforces the battle-ready capture contract here: four distinct
    # moves and two to four distinct nature tags before SQLite sees the record.
    return Pokemon.model_validate({field: creature[field] for field in fields}).model_dump(
        by_alias=True, mode="json"
    )


def generated_sprite_path(creature: Mapping[str, object]) -> Path | None:
    """Resolve only the generated sprite's filename inside the safe data bank."""

    name = creature.get("sprite")
    if not isinstance(name, str) or not name.strip():
        return None
    path = (POKEBALL_GAME_DATA_DIR / Path(name).name).resolve()
    try:
        path.relative_to(POKEBALL_GAME_DATA_DIR.resolve())
    except ValueError as exc:
        raise RuntimeError("Generated sprite path escaped image-processing data.") from exc
    if not path.is_file():
        raise RuntimeError("Image pipeline reported a missing sprite.")
    return path


async def store_pokeball_creature(
    app: FastAPI,
    *,
    capture_id: str,
    capture_badge: Badge,
    creature: Mapping[str, object],
) -> None:
    """Persist one generated capture, attach its sprite, and refresh its owner."""

    assert capture_badge.player_id is not None
    profile = pokeball_profile(creature)
    profile["metadata"] = {
        "source": "pokeball-camera",
        "capture_id": capture_id,
    }
    record = pokemon_from_dict(
        profile,
        owner_player_id=capture_badge.player_id,
        captured_by_badge_id=capture_badge.badge_id,
    )
    created = await asyncio.to_thread(app.state.shutterdex_store.create_pokemon, record)

    sprite = generated_sprite_path(creature)
    if sprite is not None:
        image_bytes = await asyncio.to_thread(sprite.read_bytes)
        sprite_key = await asyncio.to_thread(
            app.state.sprites.save_png, created.pokemon_id, image_bytes
        )
        await asyncio.to_thread(
            app.state.shutterdex_store.update_pokemon_sprite,
            created.pokemon_id,
            sprite_key,
            expected_owner_player_id=capture_badge.player_id,
        )

    await app.state.shutterdex_runtime.refresh_player(capture_badge.player_id)
    LOGGER.info(
        "Poké Ball capture %s stored as %s for badge %s.",
        capture_id,
        created.pokemon_id,
        capture_badge.htn_id,
    )


async def process_and_store_pokeball_capture(
    app: FastAPI, *, capture_id: str, photo_path: Path, capture_badge: Badge
) -> None:
    """Serialize one camera's generation jobs, then add the result to its Dex.

    The image pipeline is CPU/network-bound and may take a while, so it runs
    in this task rather than in the request handler. The one badge paired to
    the physical Poké Ball gets an animated, input-locked overlay for exactly
    the duration of its own queued capture. This deliberately does not lock
    the gateway, event loop, or any other player's badge.
    """

    loading_started = False
    try:
        async with app.state.pokeball_generation_lock:
            await app.state.shutterdex_runtime.begin_capture_loading(
                capture_badge.htn_id
            )
            loading_started = True
            creature = await asyncio.to_thread(process_pokeball_photo, photo_path)
            await store_pokeball_creature(
                app,
                capture_id=capture_id,
                capture_badge=capture_badge,
                creature=creature,
            )
            # ``refresh_player`` deliberately leaves the capture overlay in
            # place. Restore once the new Pokémon is durably saved so the
            # first normal frame already contains the updated Dex/Habitat.
            await app.state.shutterdex_runtime.end_capture_loading(
                capture_badge.htn_id, restore=True
            )
            loading_started = False
    except Exception:
        # The source photo remains in captures/ for diagnosis/replay. Avoid
        # logging its image data or any badge credential.
        LOGGER.exception("Poké Ball capture %s could not be processed.", capture_id)
    finally:
        if loading_started:
            # A processing error must never leave the physical capture badge
            # permanently locked behind the loading screen.
            await app.state.shutterdex_runtime.end_capture_loading(
                capture_badge.htn_id, restore=True
            )


async def start_configured_badges(app: FastAPI) -> None:
    """Create, pair, connect, and launch every badge declared in ``.env``."""

    badges = configured_badges()
    if not badges:
        return

    for htn_id, app_key in badges:
        player_id = configured_player_id(htn_id)
        if app.state.shutterdex_store.get_player(player_id) is None:
            app.state.shutterdex_store.create_player(
                f"Badge {htn_id}", player_id=player_id
            )
        try:
            await app.state.shutterdex_runtime.pair_badge(
                player_id=player_id,
                htn_id=htn_id,
                app_key=app_key,
            )
            await app.state.shutterdex_runtime.reset_to_home(htn_id)
            await app.state.shutterdex_runtime.launch(htn_id)
            LOGGER.info("Configured Shutterdex badge %s is connected.", htn_id)
        except GatewayError as exc:
            # Pairing is persisted before connection. A later server restart
            # or retry can use the saved credential without re-entry.
            LOGGER.warning(
                "Configured Shutterdex badge %s is stored but currently offline: %s",
                htn_id,
                exc,
            )
            app.state.configured_badge_retry_tasks.append(
                asyncio.create_task(
                    retry_configured_badge(app, htn_id),
                    name=f"shutterdex-configured-badge-{htn_id}",
                )
            )


async def retry_configured_badge(app: FastAPI, htn_id: str) -> None:
    """Retry an offline configured badge without needing a dashboard action."""

    while True:
        await asyncio.sleep(10)
        try:
            await app.state.shutterdex_runtime.launch(htn_id)
        except GatewayError:
            continue
        LOGGER.info("Configured Shutterdex badge %s reconnected.", htn_id)
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up legacy serial services and the isolated HTN OS app runtime."""

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    POKEBALL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    POKEBALL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    POKEBALL_GAME_DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    app.state.shutterdex_store = BadgeStore(DATA_DIR / "shutterdex.sqlite3")
    app.state.shutterdex_gateway = make_badge_gateway()
    app.state.player_simulation = PlayerSimulationService(
        app.state.shutterdex_store,
        backboard_api_key=os.getenv("BACKBOARD_API_KEY"),
    )

    async def publish_battle_resolution_started(battle, turn) -> None:
        """Show both badges the durable Director lock before it resolves."""

        del turn
        active_runtime = getattr(app.state, "shutterdex_runtime", None)
        if active_runtime is not None:
            await active_runtime.present_battle(battle)

    app.state.battle_service = BattleService(
        app.state.shutterdex_store,
        backboard_api_key=os.getenv("BACKBOARD_API_KEY"),
        on_resolution_started=publish_battle_resolution_started,
    )

    async def handle_badge_battle_action(
        player_id: str,
        battle_id: str | None,
        action: str,
        move_index: int | None,
    ):
        """Translate a trusted badge UI intent into one store-backed action."""

        if not battle_id:
            raise ValueError("Choose a waiting badge before starting a battle.")
        service = app.state.battle_service
        if action in {"accept_challenge", "ready"}:
            return await service.ready(battle_id, player_id)
        if action == "cancel_battle":
            return await service.cancel(battle_id, player_id)
        if action == "select_move":
            if move_index is None:
                raise ValueError("Choose one of the four moves first.")
            result = await service.resolve_move(battle_id, player_id, move_index)
            if not isinstance(result, BattleResolutionResult):
                raise RuntimeError("Battle service returned an invalid resolution.")
            return result.battle
        raise ValueError("That Battle action is not available right now.")

    async def handle_badge_battle_discovery_selection(
        player_id: str, badge_id: str, opponent_htn_id: str
    ):
        """Create a battle only after two waiting badges choose each other.

        The lobby stores a harmless HTN ID choice in each badge's own session.
        This handler treats that state as a consent signal, revalidates it on
        the server, then immediately snapshots and readies both rosters. No
        dashboard request, credential, or BLE/NFC transport participates in
        challenge creation.
        """

        store = app.state.shutterdex_store
        source = store.require_badge(badge_id)
        if source.player_id != player_id:
            raise ValueError("This badge is not paired to the selecting player.")
        target = store.require_badge_by_htn_id(opponent_htn_id)
        if target.player_id is None or target.player_id == player_id:
            raise ValueError("Choose another player's waiting badge.")

        source_session = store.get_session(source.badge_id)
        target_session = store.get_session(target.badge_id)
        if (
            source_session is None
            or target_session is None
            or not source_session.canvas_active
            or not target_session.canvas_active
            or source_session.active_app != "battle"
            or target_session.active_app != "battle"
        ):
            return None

        def selected_target(session) -> str:
            battle_state = session.app_state.get("battle", {})
            if not isinstance(battle_state, Mapping):
                return ""
            target_id = battle_state.get("challenge_target", "")
            return target_id.strip() if isinstance(target_id, str) else ""

        # The initiating selection was durably written before this callback.
        # The other badge must independently have chosen this exact HTN ID.
        if (
            selected_target(source_session) != target.htn_id
            or selected_target(target_session) != source.htn_id
        ):
            return None

        service = app.state.battle_service
        try:
            battle = await service.challenge(
                player_id,
                target.player_id,
                challenger_badge_id=source.badge_id,
                opponent_badge_id=target.badge_id,
                notice="Both badges selected each other. Battle starting.",
            )
            # Reciprocal selection is the ready check: neither player needs
            # a second, redundant confirmation after selecting the same peer.
            battle = await service.ready(battle.battle_id, player_id)
            return await service.ready(battle.battle_id, target.player_id)
        except ConflictError:
            # Both A presses can arrive in either order. If the other task
            # created this exact pair first, present its one shared battle;
            # otherwise preserve the conflict rather than attaching a badge
            # to an unrelated match.
            existing = store.get_open_battle_for_player(player_id)
            if (
                existing is not None
                and {existing.challenger_player_id, existing.opponent_player_id}
                == {player_id, target.player_id}
                and {existing.challenger_badge_id, existing.opponent_badge_id}
                == {source.badge_id, target.badge_id}
            ):
                return existing
            raise

    async def handle_badge_battle_disconnect(player_id: str):
        return await app.state.battle_service.disconnect_player(player_id)

    async def handle_badge_battle_reconnect(player_id: str):
        battle = app.state.shutterdex_store.get_open_battle_for_player(player_id)
        if (
            battle is None
            or battle.status != "disconnected"
            or battle.disconnected_player_id != player_id
        ):
            return None
        return await app.state.battle_service.reconnect(
            battle.battle_id, player_id, expected_revision=battle.revision
        )

    app.state.shutterdex_runtime = ShutterdexRuntime(
        store=app.state.shutterdex_store,
        gateway=app.state.shutterdex_gateway,
        vault=make_credential_vault(),
        ui=BadgeUi.standard(),
        renderer=ScreenRenderer(
            SpriteImageResolver(app.state.sprites, habitat_directory=HABITAT_ASSET_DIR)
        ),
        sprites=app.state.sprites,
        on_habitat_advance=app.state.player_simulation.tick,
        on_battle_action=handle_badge_battle_action,
        on_battle_discovery_select=handle_badge_battle_discovery_selection,
        on_battle_disconnect=handle_badge_battle_disconnect,
        on_battle_reconnect=handle_badge_battle_reconnect,
        on_battle_expire=app.state.battle_service.expire_due,
    )
    app.state.configured_badge_retry_tasks = []
    app.state.pokeball_generation_lock = asyncio.Lock()
    app.state.pokeball_capture_tasks = set()
    await app.state.shutterdex_runtime.start()
    await start_configured_badges(app)
    try:
        yield
    finally:
        for task in tuple(app.state.pokeball_capture_tasks):
            task.cancel()
        if app.state.pokeball_capture_tasks:
            await asyncio.gather(
                *app.state.pokeball_capture_tasks, return_exceptions=True
            )
        app.state.pokeball_capture_tasks.clear()
        for task in app.state.configured_badge_retry_tasks:
            task.cancel()
        await asyncio.gather(
            *app.state.configured_badge_retry_tasks, return_exceptions=True
        )
        await app.state.shutterdex_runtime.close()
        app.state.shutterdex_store.close()
        app.state.simulation.store.close()
        app.state.test_simulation.store.close()


app = FastAPI(
    title="Shutterdex Pokemon Generator and Simulation Server",
    description=(
        "Receives Poké Ball images, maintains the authoritative Pokemon world, "
        "and exports complete badge snapshots."
    ),
    version="0.3.0",
    lifespan=lifespan,
)
app.include_router(shutterdex_router)


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
        "pokeball_upload_endpoint": "POST /upload (raw JPEG)",
        "badge_export": "GET /badge/inbox",
    }


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload", include_in_schema=False, status_code=status.HTTP_202_ACCEPTED)
@app.post("/pokeball/upload", status_code=status.HTTP_202_ACCEPTED, tags=["pokeball"])
async def receive_pokeball_camera_image(request: Request) -> dict[str, object]:
    """Accept the ESP32 camera's raw JPEG and queue one player-owned capture.

    The branch's camera already sends an ``image/jpeg`` request body to
    ``/upload``. This preserves that wire format while moving the resulting
    Pokemon into the Wi-Fi Shutterdex SQLite store instead of a separate
    card-rendering process.
    """

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type and content_type not in {"image/jpeg", "application/octet-stream"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Poké Ball uploads must be raw JPEG data.",
        )
    try:
        capture_badge = pokeball_capture_badge(request.app)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="POKEBALL_BADGE_ID is not paired to a player.",
        ) from exc

    capture_id = uuid4().hex
    destination = POKEBALL_CAPTURE_DIR / f"{capture_id}.jpg"
    total_bytes = 0
    try:
        with destination.open("wb") as output:
            async for chunk in request.stream():
                total_bytes += len(chunk)
                if total_bytes > MAX_POKEBALL_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Poké Ball image must be 8 MiB or smaller.",
                    )
                output.write(chunk)
        image_bytes = await asyncio.to_thread(destination.read_bytes)
        if not image_bytes.startswith(b"\xff\xd8"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Poké Ball upload was not a JPEG image.",
            )
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to save Poké Ball image.",
        ) from exc

    task = asyncio.create_task(
        process_and_store_pokeball_capture(
            request.app,
            capture_id=capture_id,
            photo_path=destination,
            capture_badge=capture_badge,
        ),
        name=f"shutterdex-pokeball-{capture_id}",
    )
    request.app.state.pokeball_capture_tasks.add(task)
    task.add_done_callback(request.app.state.pokeball_capture_tasks.discard)
    return {
        "capture_id": capture_id,
        "status": "accepted",
        "target_badge": capture_badge.htn_id,
        "next_step": "The image is generating a Pokemon for this badge's player.",
    }


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
        jev_used=decision.source,
        jev_note=decision.note,
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
        "jev_used": decision.source,
        "jev_note": decision.note,
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
        "jev_used": decision.source,
        "jev_note": decision.note,
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
