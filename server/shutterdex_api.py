"""HTTP API for the server-rendered, Wi-Fi Shutterdex experience.

This router deliberately sits beside the original image/serial endpoints.
Those legacy routes remain available during the hardware migration, while the
``/shutterdex`` routes use player-owned SQLite records and the HTN OS gateway.

The endpoints below are a trusted hackathon control surface, not a completed
login system.  Put normal player authentication/authorization in front of
them before deploying to an untrusted public network.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from badge_store import (
    BadgeStore,
    BattleRecord,
    ConflictError,
    NotFoundError,
    OwnershipError,
    PokemonWorldState,
    SimulationDialogueLine,
    SimulationEventRecord,
    battle_candidate_outcome_to_dict,
    battle_record_to_dict,
    battle_turn_to_dict,
    badge_to_dict,
    player_to_dict,
    pokemon_from_dict,
    pokemon_to_dict,
    simulation_event_to_dict,
    world_state_to_dict,
)
from battle_service import (
    BattleDirectorUnavailableError,
    BattleResolutionError,
    BattleService,
    BattleServiceError,
    BattleWriterUnavailableError,
)
from credential_vault import CredentialError
from htn_gateway import GatewayError
from models import Pokemon
from player_simulation import (
    EmptyHabitatError,
    HabitatWriterUnavailableError,
    JevUnavailableError,
    PlayerSimulationError,
    PlayerSimulationService,
    WorldChangedError,
)
from shutterdex_runtime import RenderDispatch, ShutterdexRuntime, ShutterdexRuntimeError
from sprite_assets import SpriteError, SpriteStore


router = APIRouter(prefix="/shutterdex", tags=["Shutterdex Wi-Fi"])

MAX_SPRITE_BYTES = 5 * 1024 * 1024
ALLOWED_SPRITE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class PlayerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)


class PairBadgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: str = Field(min_length=3, max_length=128)
    htn_id: str = Field(min_length=1, max_length=64)
    # Never place this model in a response model or log it.  CredentialVault
    # validates exact printable characters and encrypts it before persistence.
    app_key: str = Field(min_length=4, max_length=32, repr=False)


class ButtonActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    button: str = Field(min_length=1, max_length=32)
    pressed: bool = True
    repeat: bool = False


class OwnedPokemonCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pokemon: Pokemon
    captured_by_htn_id: str | None = Field(default=None, max_length=64)


class PokemonTradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_player_id: str = Field(min_length=3, max_length=128)
    expected_owner_player_id: str | None = Field(default=None, min_length=3, max_length=128)
    reason: str = Field(default="trade", min_length=1, max_length=96)


class WorldStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0, le=100)
    y: int = Field(ge=0, le=100)
    mood: str = Field(default="calm", min_length=1, max_length=32)
    energy: int = Field(default=75, ge=0, le=100)
    activity: str = Field(default="waiting", min_length=1, max_length=96)


class DialogueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker_pokemon_id: str = Field(min_length=3, max_length=128)
    text: str = Field(min_length=1, max_length=240)


class SimulationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_pokemon_id: str = Field(min_length=3, max_length=128)
    target_pokemon_id: str | None = Field(default=None, min_length=3, max_length=128)
    kind: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=240)
    dialogue: list[DialogueRequest] = Field(default_factory=list, max_length=4)


class BattleChallengeRequest(BaseModel):
    """Start a server-authoritative match between two paired badge owners."""

    model_config = ConfigDict(extra="forbid")

    challenger_htn_id: str = Field(min_length=1, max_length=64)
    opponent_htn_id: str = Field(min_length=1, max_length=64)


class BattleReadyRequest(BaseModel):
    """One participant's ready-check response for a pending challenge."""

    model_config = ConfigDict(extra="forbid")

    htn_id: str = Field(min_length=1, max_length=64)
    ready: bool = True
    expected_revision: int | None = Field(default=None, ge=0)


class BattleMoveRequest(BaseModel):
    """A move-index press from the active participant's badge."""

    model_config = ConfigDict(extra="forbid")

    htn_id: str = Field(min_length=1, max_length=64)
    move_index: int = Field(ge=0, le=3)
    expected_revision: int | None = Field(default=None, ge=0)


class BattleCancelRequest(BaseModel):
    """Cancel an open challenge or battle from either participant badge."""

    model_config = ConfigDict(extra="forbid")

    htn_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="cancelled", min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)


class BattleConnectionRequest(BaseModel):
    """Report one participant's connection state without trusting a player ID."""

    model_config = ConfigDict(extra="forbid")

    htn_id: str = Field(min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=0)


def runtime(request: Request) -> ShutterdexRuntime:
    return request.app.state.shutterdex_runtime


def store(request: Request) -> BadgeStore:
    return request.app.state.shutterdex_store


def sprites(request: Request) -> SpriteStore:
    return request.app.state.sprites


def player_simulation(request: Request) -> PlayerSimulationService:
    return request.app.state.player_simulation


def battle_service(request: Request) -> BattleService:
    """Return the one process-wide battle coordinator.

    It is deliberately separate from the runtime: dashboard requests and
    hardware button events use the same durable service methods, while the
    runtime is responsible only for presenting the resulting shared state.
    """

    return request.app.state.battle_service


def _dispatch_payload(dispatch: RenderDispatch) -> dict[str, object]:
    return {
        "badge_id": dispatch.badge_id,
        "htn_id": dispatch.htn_id,
        "scene": dispatch.scene,
        "session_revision": dispatch.session_revision,
        "queued_commands": dispatch.queued_commands,
    }


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (ConflictError, OwnershipError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (HabitatWriterUnavailableError, JevUnavailableError)):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(
        exc, (BattleWriterUnavailableError, BattleDirectorUnavailableError)
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(exc, (BattleResolutionError, BattleServiceError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (EmptyHabitatError, WorldChangedError, PlayerSimulationError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CredentialError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(exc, (GatewayError, ShutterdexRuntimeError)):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(exc, (SpriteError, ValueError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Shutterdex failed to process the request.")


def _paired_badge_player_id(request: Request, htn_id: str) -> str:
    """Resolve a paired badge to its player without accepting a client player ID."""

    badge = store(request).require_badge_by_htn_id(htn_id)
    if badge.player_id is None:
        raise OwnershipError("This badge must be paired to a player before it can battle.")
    return badge.player_id


def _battle_participant_player_id(request: Request, battle_id: str, htn_id: str) -> str:
    """Authorize a battle action through the participant's paired badge."""

    badge = store(request).require_badge_by_htn_id(htn_id)
    player_id = _paired_badge_player_id(request, htn_id)
    battle = store(request).require_battle(battle_id)
    if player_id not in {battle.challenger_player_id, battle.opponent_player_id}:
        raise OwnershipError("This badge's player is not a participant in this battle.")
    expected_badge_id = (
        battle.challenger_badge_id
        if player_id == battle.challenger_player_id
        else battle.opponent_badge_id
    )
    if expected_badge_id is not None and badge.badge_id != expected_badge_id:
        raise OwnershipError("Only the badge selected for this battle can control it.")
    return player_id


async def _present_battle_payload(
    request: Request, battle: BattleRecord
) -> dict[str, object]:
    """Render the same durable result on both player badges and serialize it."""

    # All API-visible data passes through the safe serializer so a badge's
    # encrypted app key can never appear in a battle response.
    renders = await runtime(request).present_battle(battle)
    return {
        "battle": battle_record_to_dict(battle),
        "refreshed_badges": [_dispatch_payload(item) for item in renders],
    }


@router.post("/players", status_code=status.HTTP_201_CREATED)
async def create_player(body: PlayerCreateRequest, request: Request) -> dict[str, object]:
    try:
        return {"player": player_to_dict(store(request).create_player(body.display_name))}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/players/{player_id}")
async def get_player(player_id: str, request: Request) -> dict[str, object]:
    try:
        player = store(request).require_player(player_id)
        return {
            "player": player_to_dict(player),
            "badges": [badge_to_dict(item) for item in store(request).list_badges_for_player(player_id)],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/badges/pair", status_code=status.HTTP_201_CREATED)
async def pair_badge(body: PairBadgeRequest, request: Request) -> dict[str, object]:
    """Store an encrypted HTN app key and begin an outgoing app connection."""

    try:
        badge = await runtime(request).pair_badge(
            player_id=body.player_id, htn_id=body.htn_id, app_key=body.app_key
        )
        return {"badge": badge_to_dict(badge), "connection": "registered"}
    except GatewayError:
        # Pairing is durable before an app socket is attempted.  Surface that
        # useful state instead of making users re-enter their app key merely
        # because their badge is currently offline.
        try:
            badge = store(request).require_badge_by_htn_id(body.htn_id)
            return {"badge": badge_to_dict(badge), "connection": "stored_offline"}
        except Exception as exc:
            raise _http_error(exc) from exc
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/badges/{htn_id}")
async def get_badge(htn_id: str, request: Request) -> dict[str, object]:
    try:
        return {"badge": badge_to_dict(store(request).require_badge_by_htn_id(htn_id))}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/badges/{htn_id}/launch")
async def launch_badge(htn_id: str, request: Request) -> dict[str, object]:
    try:
        return {"render": _dispatch_payload(await runtime(request).launch(htn_id))}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/badges/{htn_id}/actions")
async def badge_action(
    htn_id: str, body: ButtonActionRequest, request: Request
) -> dict[str, object]:
    """Useful for a dashboard test button; hardware buttons use the WSS event path."""

    if not body.pressed:
        return {"handled": False, "reason": "release events do not change Shutterdex UI state"}
    try:
        return {
            "handled": True,
            "render": _dispatch_payload(
                await runtime(request).handle_button(
                    htn_id, body.button, repeat=body.repeat
                )
            ),
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/badges/{htn_id}/session")
async def badge_session(htn_id: str, request: Request) -> dict[str, object]:
    try:
        badge = store(request).require_badge_by_htn_id(htn_id)
        session = store(request).get_or_create_session(badge.badge_id)
        return {
            "badge": badge_to_dict(badge),
            "session": {
                "active_app": session.active_app,
                "app_state": session.app_state,
                "canvas_active": session.canvas_active,
                "revision": session.revision,
                "updated_at": session.updated_at.isoformat(),
            },
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/challenges", status_code=status.HTTP_201_CREATED)
async def create_battle_challenge(
    body: BattleChallengeRequest, request: Request
) -> dict[str, object]:
    """Snapshot both players' newest battle-ready Pokemon into a challenge."""

    try:
        # Purge old ready/turn/reconnect deadlines before checking the
        # one-open-battle rule.  A terminal record can then be replaced by a
        # fresh challenge without a manual database repair.
        for expired in await battle_service(request).expire_due():
            await runtime(request).present_battle(expired)

        challenger_badge = store(request).require_badge_by_htn_id(
            body.challenger_htn_id
        )
        opponent_badge = store(request).require_badge_by_htn_id(body.opponent_htn_id)
        challenger_player_id = _paired_badge_player_id(
            request, body.challenger_htn_id
        )
        opponent_player_id = _paired_badge_player_id(request, body.opponent_htn_id)
        battle = await battle_service(request).challenge(
            challenger_player_id,
            opponent_player_id,
            challenger_badge_id=challenger_badge.badge_id,
            opponent_badge_id=opponent_badge.badge_id,
        )
        return await _present_battle_payload(request, battle)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/expire")
async def expire_battles(request: Request) -> dict[str, object]:
    """Run the durable deadline sweep and update each affected badge scene."""

    try:
        expired = await battle_service(request).expire_due()
        renders: list[RenderDispatch] = []
        for battle in expired:
            renders.extend(await runtime(request).present_battle(battle))
        return {
            "expired": [battle_record_to_dict(item) for item in expired],
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/badges/{htn_id}/battle")
async def open_badge_battle(htn_id: str, request: Request) -> dict[str, object]:
    """Return the one nonterminal battle attached to a paired badge, if any."""

    try:
        badge = store(request).require_badge_by_htn_id(htn_id)
        player_id = _paired_badge_player_id(request, htn_id)
        battle = store(request).get_open_battle_for_player(player_id)
        if battle is not None:
            expected_badge_id = (
                battle.challenger_badge_id
                if player_id == battle.challenger_player_id
                else battle.opponent_badge_id
            )
            if expected_badge_id is not None and expected_badge_id != badge.badge_id:
                battle = None
        return {"battle": battle_record_to_dict(battle) if battle is not None else None}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/battles/{battle_id}")
async def get_battle(
    battle_id: str,
    request: Request,
    htn_id: str = Query(min_length=1, max_length=64),
) -> dict[str, object]:
    """Read one battle and its persisted Writer/Jev audit turns as a participant."""

    try:
        _battle_participant_player_id(request, battle_id, htn_id)
        battle = store(request).require_battle(battle_id)
        return {
            "battle": battle_record_to_dict(battle),
            "turns": [
                battle_turn_to_dict(item)
                for item in store(request).list_battle_turns(battle_id)
            ],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/{battle_id}/ready")
async def ready_battle(
    battle_id: str, body: BattleReadyRequest, request: Request
) -> dict[str, object]:
    """Record one badge's ready response; activation requires both players."""

    try:
        player_id = _battle_participant_player_id(request, battle_id, body.htn_id)
        battle = await battle_service(request).ready(
            battle_id,
            player_id,
            ready=body.ready,
            expected_revision=body.expected_revision,
        )
        return await _present_battle_payload(request, battle)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/{battle_id}/moves")
async def select_battle_move(
    battle_id: str, body: BattleMoveRequest, request: Request
) -> dict[str, object]:
    """Resolve one legal move through Writer -> Jev -> deterministic commit."""

    try:
        player_id = _battle_participant_player_id(request, battle_id, body.htn_id)
        result = await battle_service(request).resolve_move(
            battle_id,
            player_id,
            body.move_index,
            expected_revision=body.expected_revision,
        )
        payload = await _present_battle_payload(request, result.battle)
        payload["turn"] = battle_turn_to_dict(result.turn)
        payload["selected_candidate"] = battle_candidate_outcome_to_dict(
            result.selected_candidate
        )
        return payload
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/{battle_id}/cancel")
async def cancel_battle(
    battle_id: str, body: BattleCancelRequest, request: Request
) -> dict[str, object]:
    """Cancel an open battle from either paired participant badge."""

    try:
        player_id = _battle_participant_player_id(request, battle_id, body.htn_id)
        battle = await battle_service(request).cancel(
            battle_id,
            player_id,
            reason=body.reason,
            expected_revision=body.expected_revision,
        )
        return await _present_battle_payload(request, battle)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/{battle_id}/disconnect")
async def disconnect_battle_player(
    battle_id: str, body: BattleConnectionRequest, request: Request
) -> dict[str, object]:
    """Temporarily lock a match while one of its badges reconnects."""

    try:
        player_id = _battle_participant_player_id(request, battle_id, body.htn_id)
        battle = await battle_service(request).disconnect(
            battle_id,
            player_id,
            expected_revision=body.expected_revision,
        )
        return await _present_battle_payload(request, battle)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/battles/{battle_id}/reconnect")
async def reconnect_battle_player(
    battle_id: str, body: BattleConnectionRequest, request: Request
) -> dict[str, object]:
    """Restore the pre-disconnect turn/ready phase for the returning badge."""

    try:
        player_id = _battle_participant_player_id(request, battle_id, body.htn_id)
        battle = await battle_service(request).reconnect(
            battle_id,
            player_id,
            expected_revision=body.expected_revision,
        )
        return await _present_battle_payload(request, battle)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/players/{player_id}/pokemon")
async def list_pokemon(player_id: str, request: Request) -> dict[str, object]:
    try:
        store(request).require_player(player_id)
        return {
            "pokemon": [
                pokemon_to_dict(item)
                for item in store(request).list_pokemon_for_player(player_id)
            ]
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/players/{player_id}/pokemon", status_code=status.HTTP_201_CREATED)
async def create_owned_pokemon(
    player_id: str, body: OwnedPokemonCreateRequest, request: Request
) -> dict[str, object]:
    try:
        captured_by_badge_id: str | None = None
        if body.captured_by_htn_id:
            capture_badge = store(request).require_badge_by_htn_id(body.captured_by_htn_id)
            if capture_badge.player_id != player_id:
                raise OwnershipError("A capture badge must belong to the Pokemon owner.")
            captured_by_badge_id = capture_badge.badge_id
        record = pokemon_from_dict(
            body.pokemon.model_dump(by_alias=True, mode="json"),
            owner_player_id=player_id,
            captured_by_badge_id=captured_by_badge_id,
        )
        created = store(request).create_pokemon(record)
        renders = await runtime(request).refresh_player(player_id)
        return {
            "pokemon": pokemon_to_dict(created),
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/pokemon/{pokemon_id}/sprite")
async def upload_owned_pokemon_sprite(
    pokemon_id: str, request: Request, image: UploadFile = File(...)
) -> dict[str, object]:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_SPRITE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Sprite must be a JPEG, PNG, or WebP image.",
        )
    try:
        content = await image.read(MAX_SPRITE_BYTES + 1)
    finally:
        await image.close()
    if len(content) > MAX_SPRITE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Sprite image must be 5 MiB or smaller.",
        )
    try:
        pokemon = store(request).require_pokemon(pokemon_id)
        key = sprites(request).save_png(pokemon_id, content)
        updated = store(request).update_pokemon_sprite(
            pokemon_id, key, expected_owner_player_id=pokemon.owner_player_id
        )
        renders = await runtime(request).refresh_player(pokemon.owner_player_id)
        return {
            "pokemon": pokemon_to_dict(updated),
            "sprite_key": key,
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/pokemon/{pokemon_id}/trade")
async def trade_pokemon(
    pokemon_id: str, body: PokemonTradeRequest, request: Request
) -> dict[str, object]:
    try:
        pokemon, transfer = store(request).transfer_pokemon(
            pokemon_id,
            body.to_player_id,
            expected_owner_player_id=body.expected_owner_player_id,
            reason=body.reason,
        )
        # Both players can have a live Dex/Habitat screen.  Refresh after the
        # atomic transfer so neither sees an intermediate ownership state.
        source_renders = await runtime(request).refresh_player(transfer.from_player_id)
        target_renders = await runtime(request).refresh_player(transfer.to_player_id)
        return {
            "pokemon": pokemon_to_dict(pokemon),
            "transfer": {
                "transfer_id": transfer.transfer_id,
                "from_player_id": transfer.from_player_id,
                "to_player_id": transfer.to_player_id,
                "reason": transfer.reason,
                "transferred_at": transfer.transferred_at.isoformat(),
            },
            "refreshed_badges": [
                *[_dispatch_payload(item) for item in source_renders],
                *[_dispatch_payload(item) for item in target_renders],
            ],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/players/{player_id}/world/{pokemon_id}")
async def put_world_state(
    player_id: str, pokemon_id: str, body: WorldStateRequest, request: Request
) -> dict[str, object]:
    try:
        state = store(request).upsert_world_state(
            player_id,
            PokemonWorldState(
                pokemon_id=pokemon_id,
                x=body.x,
                y=body.y,
                mood=body.mood,
                energy=body.energy,
                activity=body.activity,
            ),
        )
        renders = await runtime(request).refresh_player(
            player_id, only_active_app="habitat"
        )
        return {
            "state": world_state_to_dict(state),
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/players/{player_id}/events", status_code=status.HTTP_201_CREATED)
async def append_world_event(
    player_id: str, body: SimulationEventRequest, request: Request
) -> dict[str, object]:
    try:
        existing = store(request).list_simulation_events_for_player(player_id, limit=1)
        revision = existing[-1].revision + 1 if existing else 1
        event = store(request).append_simulation_event(
            SimulationEventRecord(
                player_id=player_id,
                revision=revision,
                actor_pokemon_id=body.actor_pokemon_id,
                target_pokemon_id=body.target_pokemon_id,
                kind=body.kind,
                summary=body.summary,
                dialogue=tuple(
                    SimulationDialogueLine(
                        speaker_pokemon_id=line.speaker_pokemon_id, text=line.text
                    )
                    for line in body.dialogue
                ),
            )
        )
        renders = await runtime(request).refresh_player(
            player_id, only_active_app="habitat"
        )
        return {
            "event": simulation_event_to_dict(event),
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/players/{player_id}/simulation/tick")
async def tick_player_habitat(player_id: str, request: Request) -> dict[str, object]:
    """Advance a Habitat only after Jev selects its next interaction."""

    try:
        result = await player_simulation(request).tick(player_id)
        renders = await runtime(request).refresh_player(
            player_id, only_active_app="habitat"
        )
        return {
            "simulation": result.to_dict(),
            "refreshed_badges": [_dispatch_payload(item) for item in renders],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/players/{player_id}/world")
async def player_world(player_id: str, request: Request) -> dict[str, object]:
    try:
        store(request).require_player(player_id)
        return {
            "states": [
                world_state_to_dict(item)
                for item in store(request).list_world_states_for_player(player_id)
            ],
            "events": [
                simulation_event_to_dict(item)
                for item in store(request).list_simulation_events_for_player(player_id)
            ],
        }
    except Exception as exc:
        raise _http_error(exc) from exc


__all__ = ["router"]
