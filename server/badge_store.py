"""SQLite persistence for the multi-badge Shutterdex server.

This module is deliberately independent of FastAPI, Pydantic, and the legacy
Lua-badge code.  It is the server-side source of truth for:

* players and their paired HTN badges;
* Pokemon ownership and capture provenance;
* an auditable, atomic ownership-transfer operation; and
* the small, JSON-serializable UI session owned by each badge.

``app_key_ciphertext`` is intentionally treated as an opaque encrypted blob.
The caller is responsible for encrypting it before it reaches this module and
for decrypting it only when an outbound HTN command must be authenticated.
No firmware credential is represented or persisted here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator, Literal, Mapping, Sequence, TypeAlias
from uuid import uuid4


JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]


class BadgeStoreError(RuntimeError):
    """Base exception for persistence-layer failures."""


class NotFoundError(BadgeStoreError):
    """Raised when a requested durable record does not exist."""


class ConflictError(BadgeStoreError):
    """Raised when a unique constraint or optimistic revision check fails."""


class OwnershipError(ConflictError):
    """Raised when a Pokemon transfer does not match its current owner."""


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def datetime_to_storage(value: datetime) -> str:
    """Serialize a datetime in one canonical UTC ISO-8601 representation."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds")


def datetime_from_storage(value: str) -> datetime:
    """Parse a timestamp emitted by :func:`datetime_to_storage`."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid stored timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def json_dumps(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically.

    The round trip is also used to sever references to mutable app-state
    dictionaries supplied by callers.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Value must contain only JSON-compatible data.") from exc


def json_loads(value: str) -> JSONValue:
    """Read persisted JSON and reject corrupt/non-JSON values clearly."""

    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Stored JSON is invalid.") from exc


def json_object(value: Mapping[str, Any] | None, *, field_name: str) -> dict[str, JSONValue]:
    """Return a defensive, JSON-only copy of a mapping.

    Badge UI state is intentionally kept to basic JSON values.  That makes it
    safe to persist, easy to inspect, and resilient across process restarts.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    copied = json_loads(json_dumps(dict(value)))
    if not isinstance(copied, dict):  # Defensive: json.dumps(dict(...)) always is.
        raise ValueError(f"{field_name} must be a JSON object.")
    return copied


def _text(value: str, field_name: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} cannot be blank.")
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters.")
    return value


def _optional_text(value: str | None, field_name: str, *, maximum: int = 2048) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum=maximum)


def _identifier(value: str, field_name: str) -> str:
    # IDs are opaque server identifiers, not SQL fragments.  Restricting them
    # still catches accidental whitespace and makes URLs/logs predictable.
    value = _text(value, field_name, maximum=128)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if any(character not in allowed for character in value):
        raise ValueError(f"{field_name} may contain only letters, digits, '_' and '-'.")
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _normalise_types(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        raise ValueError("types must be a string or a sequence of strings.")
    result = tuple(_text(value, "type", maximum=32).lower() for value in values)
    if not result:
        raise ValueError("Pokemon needs at least one type.")
    if len(result) > 3:
        raise ValueError("Pokemon may have at most three types.")
    if len(set(result)) != len(result):
        raise ValueError("Pokemon types cannot be duplicated.")
    return result


def _normalise_stats(values: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ValueError("stats must be a JSON object.")
    result: dict[str, int] = {}
    for name in ("hp", "attack", "defense", "speed"):
        raw = values.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 999:
            raise ValueError(f"stats.{name} must be an integer from 1 to 999.")
        result[name] = raw
    return result


def _normalise_moves(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError("moves must be a sequence of strings.")
    result = tuple(_text(value, "move", maximum=64).lower().replace(" ", "_") for value in values)
    if not result:
        raise ValueError("Pokemon needs at least one move.")
    if len(result) > 4:
        raise ValueError("Pokemon may have at most four moves.")
    return result


def _normalise_battle_natures(values: Sequence[str]) -> tuple[str, ...]:
    """Normalize optional tags while keeping pre-battle records readable."""

    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError("battle_natures must be a sequence of strings.")
    result = tuple(
        _text(value, "battle_nature", maximum=48).lower().replace(" ", "_")
        for value in values
    )
    if len(result) > 4:
        raise ValueError("Pokemon may have at most four battle-nature tags.")
    if len(set(result)) != len(result):
        raise ValueError("Pokemon battle-nature tags cannot be duplicated.")
    return result


@dataclass(frozen=True, slots=True)
class Player:
    player_id: str
    display_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Badge:
    """A paired HTN badge.

    ``app_key_ciphertext`` remains opaque in this type.  Do not expose it in
    API responses; use :func:`badge_to_dict` with ``include_credential=False``
    (the default) for client-facing payloads.
    """

    badge_id: str
    htn_id: str
    player_id: str | None
    app_key_ciphertext: str
    last_seen_at: datetime | None
    online: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PokemonRecord:
    """Durable Pokemon profile and ownership record.

    ``captured_by_badge_id`` is provenance only.  ``owner_player_id`` is the
    authoritative current owner and is the field changed by a trade.
    """

    pokemon_id: str
    owner_player_id: str
    name: str
    species: str
    types: tuple[str, ...]
    stats: Mapping[str, int]
    moves: tuple[str, ...]
    flavour: str
    sprite_prompt: str
    rarity: str
    caught_at: datetime
    sprite_path: str | None
    captured_by_badge_id: str | None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    battle_natures: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class PokemonWorldState:
    """The mutable Habitat state for one Pokemon.

    Ownership is intentionally not duplicated here: it is resolved from the
    Pokemon record when a state is read or written.  That keeps a trade from
    accidentally leaving a creature's movement state attached to its old
    player.
    """

    pokemon_id: str
    x: int = 50
    y: int = 50
    mood: str = "calm"
    energy: int = 75
    activity: str = "waiting"
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SimulationDialogueLine:
    speaker_pokemon_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SimulationEventRecord:
    """A player-scoped Habitat event with optional short dialogue."""

    player_id: str
    revision: int
    actor_pokemon_id: str
    kind: str
    summary: str
    target_pokemon_id: str | None = None
    dialogue: tuple[SimulationDialogueLine, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: _new_id("evt"))


@dataclass(frozen=True, slots=True)
class BadgeSession:
    """The serializable, per-badge UI state.

    ``revision`` is incremented by :meth:`BadgeStore.save_session` and can be
    supplied as ``expected_revision`` to reject racing button events.
    """

    badge_id: str
    active_app: str
    app_state: Mapping[str, JSONValue]
    canvas_active: bool
    last_render_hash: str | None
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OwnershipTransfer:
    transfer_id: str
    pokemon_id: str
    from_player_id: str
    to_player_id: str
    reason: str
    transferred_at: datetime


# Battle state deliberately lives beside, rather than inside, ``PokemonRecord``.
# A battle must survive a trade or later profile edit without silently changing
# the combatants that originally accepted the challenge.  These records are
# therefore immutable snapshots; only ``current_hp``, ``stat_stages``, and a
# roster's ``active_index`` change while a battle is in progress.
BattleStatus: TypeAlias = Literal[
    "challenge",
    "ready",
    "active",
    "resolving",
    "finished",
    "cancelled",
    "disconnected",
    "timed_out",
]
BattleTurnStatus: TypeAlias = Literal["resolving", "resolved", "aborted"]

_BATTLE_OPEN_STATUSES: frozenset[str] = frozenset(
    {"challenge", "ready", "active", "resolving", "disconnected"}
)
_BATTLE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"finished", "cancelled", "timed_out"}
)
_BATTLE_STAT_STAGE_NAMES: tuple[str, ...] = ("attack", "defense", "speed")

# ``turn_deadline_at`` has two deliberately distinct meanings, based on the
# durable battle phase.  While a battle is ``active`` it is the player's move
# clock.  When a legal move is claimed and the battle becomes ``resolving``,
# it becomes a bounded *resolution lease*.  A lease expiry releases the same
# turn for retry rather than declaring the player timed out.  This keeps a
# slow Writer/Director call from consuming a player's turn clock while still
# giving a process-restart-safe recovery path for abandoned resolutions.
DEFAULT_BATTLE_TURN_TIMEOUT_SECONDS = 60
DEFAULT_BATTLE_RESOLUTION_LEASE_SECONDS = 120


def _default_battle_stat_stages() -> dict[str, int]:
    return {name: 0 for name in _BATTLE_STAT_STAGE_NAMES}


@dataclass(frozen=True, slots=True)
class BattlePokemonSnapshot:
    """The immutable combat profile plus mutable combat values for one Pokemon.

    The original Pokemon record can be edited or traded after a challenge.
    This snapshot intentionally cannot: it is the profile both players saw
    when they readied up.  A resolution may only change ``current_hp`` and
    bounded ``stat_stages``.
    """

    pokemon_id: str
    name: str
    species: str
    types: tuple[str, ...]
    stats: Mapping[str, int]
    moves: tuple[str, ...]
    battle_natures: tuple[str, ...]
    flavour: str
    rarity: str
    max_hp: int
    current_hp: int
    sprite_path: str | None = None
    stat_stages: Mapping[str, int] = field(default_factory=_default_battle_stat_stages)

    @property
    def fainted(self) -> bool:
        return self.current_hp <= 0


@dataclass(frozen=True, slots=True)
class BattleRosterSnapshot:
    """A player's fixed one-to-six Pokemon battle roster."""

    pokemon: tuple[BattlePokemonSnapshot, ...]
    active_index: int = 0

    @property
    def active_pokemon(self) -> BattlePokemonSnapshot:
        return self.pokemon[self.active_index]


@dataclass(frozen=True, slots=True)
class BattleCandidateOutcome:
    """One Writer-proposed, server-shaped outcome awaiting Jev's choice.

    A model can only propose bounded HP and stage deltas.  The battle service
    applies the selected candidate deterministically to the two active roster
    members, then the store verifies the resulting snapshots before commit.
    """

    candidate_id: str
    summary: str
    rationale: str
    actor_hp_delta: int = 0
    target_hp_delta: int = 0
    actor_stat: str | None = None
    actor_stat_delta: int = 0
    target_stat: str | None = None
    target_stat_delta: int = 0


@dataclass(frozen=True, slots=True)
class BattleTurnRecord:
    """Durable audit record for one move selection and its resolution."""

    turn_id: str
    battle_id: str
    turn_number: int
    attempt: int
    acting_player_id: str
    actor_pokemon_id: str
    move_id: str
    candidate_outcomes: tuple[BattleCandidateOutcome, ...]
    status: BattleTurnStatus
    created_at: datetime
    selected_candidate_id: str | None = None
    visible_rationale: str | None = None
    state_after: Mapping[str, JSONValue] | None = None
    resolved_at: datetime | None = None
    abort_reason: str | None = None


@dataclass(frozen=True, slots=True)
class BattleRecord:
    """Authoritative shared state for a two-player Shutterdex battle."""

    battle_id: str
    challenger_player_id: str
    challenger_display_name: str
    challenger_roster: BattleRosterSnapshot
    challenger_ready: bool
    opponent_player_id: str
    opponent_display_name: str
    opponent_roster: BattleRosterSnapshot
    opponent_ready: bool
    status: BattleStatus
    current_player_id: str | None
    turn_number: int
    revision: int
    created_at: datetime
    updated_at: datetime
    challenger_badge_id: str | None = None
    opponent_badge_id: str | None = None
    ready_deadline_at: datetime | None = None
    turn_deadline_at: datetime | None = None
    disconnect_deadline_at: datetime | None = None
    disconnected_player_id: str | None = None
    status_before_disconnect: BattleStatus | None = None
    winner_player_id: str | None = None
    cancelled_by_player_id: str | None = None
    end_reason: str | None = None
    notice: str | None = None
    last_visible_rationale: str | None = None


def player_to_dict(player: Player) -> dict[str, JSONValue]:
    return {
        "player_id": player.player_id,
        "display_name": player.display_name,
        "created_at": datetime_to_storage(player.created_at),
        "updated_at": datetime_to_storage(player.updated_at),
    }


def badge_to_dict(badge: Badge, *, include_credential: bool = False) -> dict[str, JSONValue]:
    """Return an API-safe badge mapping unless explicitly asked otherwise."""

    result: dict[str, JSONValue] = {
        "badge_id": badge.badge_id,
        "htn_id": badge.htn_id,
        "player_id": badge.player_id,
        "last_seen_at": (
            datetime_to_storage(badge.last_seen_at) if badge.last_seen_at is not None else None
        ),
        "online": badge.online,
        "created_at": datetime_to_storage(badge.created_at),
        "updated_at": datetime_to_storage(badge.updated_at),
    }
    if include_credential:
        result["app_key_ciphertext"] = badge.app_key_ciphertext
    return result


def pokemon_to_dict(pokemon: PokemonRecord) -> dict[str, JSONValue]:
    """Serialize a record for JSON APIs or renderer context."""

    return {
        "pokemon_id": pokemon.pokemon_id,
        "owner_player_id": pokemon.owner_player_id,
        "name": pokemon.name,
        "species": pokemon.species,
        "types": list(pokemon.types),
        # The legacy generator calls this a singular ``type``.  Keeping it in
        # the compatibility payload lets existing image-generation code migrate
        # without throwing away a second type if one is later introduced.
        "type": pokemon.types[0],
        "stats": dict(pokemon.stats),
        "moves": list(pokemon.moves),
        "battle_natures": list(pokemon.battle_natures),
        "flavour": pokemon.flavour,
        "sprite_prompt": pokemon.sprite_prompt,
        "rarity": pokemon.rarity,
        "caught_at": datetime_to_storage(pokemon.caught_at),
        "sprite_path": pokemon.sprite_path,
        "captured_by_badge_id": pokemon.captured_by_badge_id,
        "metadata": json_object(pokemon.metadata, field_name="metadata"),
        "created_at": datetime_to_storage(pokemon.created_at),
        "updated_at": datetime_to_storage(pokemon.updated_at),
    }


def world_state_to_dict(state: PokemonWorldState) -> dict[str, JSONValue]:
    return {
        "pokemon_id": state.pokemon_id,
        "x": state.x,
        "y": state.y,
        "mood": state.mood,
        "energy": state.energy,
        "activity": state.activity,
        "updated_at": datetime_to_storage(state.updated_at),
    }


def simulation_event_to_dict(event: SimulationEventRecord) -> dict[str, JSONValue]:
    return {
        "event_id": event.event_id,
        "player_id": event.player_id,
        "revision": event.revision,
        "actor_pokemon_id": event.actor_pokemon_id,
        "target_pokemon_id": event.target_pokemon_id,
        "kind": event.kind,
        "summary": event.summary,
        "dialogue": [
            {"speaker_pokemon_id": line.speaker_pokemon_id, "text": line.text}
            for line in event.dialogue
        ],
        "created_at": datetime_to_storage(event.created_at),
    }


def pokemon_from_dict(
    payload: Mapping[str, Any],
    *,
    owner_player_id: str | None = None,
    captured_by_badge_id: str | None = None,
) -> PokemonRecord:
    """Build a :class:`PokemonRecord` from the existing generator's mapping.

    It accepts either ``type``/``element`` or the newer ``types`` list.  This
    is the only compatibility seam needed by the existing image workflow.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("Pokemon payload must be an object.")
    raw_types = payload.get("types", payload.get("type", payload.get("element")))
    if raw_types is None:
        raise ValueError("Pokemon payload is missing type(s).")
    raw_caught_at = payload.get("caught_at")
    caught_at = (
        datetime_from_storage(raw_caught_at)
        if isinstance(raw_caught_at, str)
        else utc_now()
    )
    raw_created_at = payload.get("created_at")
    created_at = (
        datetime_from_storage(raw_created_at)
        if isinstance(raw_created_at, str)
        else caught_at
    )
    raw_updated_at = payload.get("updated_at")
    updated_at = (
        datetime_from_storage(raw_updated_at)
        if isinstance(raw_updated_at, str)
        else created_at
    )
    return PokemonRecord(
        pokemon_id=_identifier(
            str(payload.get("pokemon_id", payload.get("id", _new_id("mon")))), "pokemon_id"
        ),
        owner_player_id=_identifier(
            owner_player_id or str(payload.get("owner_player_id", "")), "owner_player_id"
        ),
        name=_text(str(payload.get("name", "")), "name", maximum=48),
        species=_text(str(payload.get("species", "")), "species", maximum=96),
        types=_normalise_types(raw_types),
        stats=_normalise_stats(payload.get("stats", {})),
        moves=_normalise_moves(payload.get("moves", ())),
        battle_natures=_normalise_battle_natures(payload.get("battle_natures", ())),
        flavour=_text(str(payload.get("flavour", "")), "flavour", maximum=240),
        sprite_prompt=_text(str(payload.get("sprite_prompt", "")), "sprite_prompt", maximum=500),
        rarity=_text(str(payload.get("rarity", "common")), "rarity", maximum=32).lower(),
        caught_at=caught_at,
        sprite_path=_optional_text(
            payload.get("sprite_path", payload.get("sprite_key")), "sprite_path", maximum=512
        ),
        captured_by_badge_id=(
            _identifier(captured_by_badge_id, "captured_by_badge_id")
            if captured_by_badge_id is not None
            else _optional_identifier(payload.get("captured_by_badge_id"), "captured_by_badge_id")
        ),
        metadata=json_object(payload.get("metadata"), field_name="metadata"),
        created_at=created_at,
        updated_at=updated_at,
    )


def session_to_dict(session: BadgeSession) -> dict[str, JSONValue]:
    return {
        "badge_id": session.badge_id,
        "active_app": session.active_app,
        "app_state": json_object(session.app_state, field_name="app_state"),
        "canvas_active": session.canvas_active,
        "last_render_hash": session.last_render_hash,
        "revision": session.revision,
        "updated_at": datetime_to_storage(session.updated_at),
    }


def ownership_transfer_to_dict(transfer: OwnershipTransfer) -> dict[str, JSONValue]:
    return {
        "transfer_id": transfer.transfer_id,
        "pokemon_id": transfer.pokemon_id,
        "from_player_id": transfer.from_player_id,
        "to_player_id": transfer.to_player_id,
        "reason": transfer.reason,
        "transferred_at": datetime_to_storage(transfer.transferred_at),
    }


def battle_pokemon_snapshot_from_pokemon(pokemon: PokemonRecord) -> BattlePokemonSnapshot:
    """Freeze a battle-ready profile from a current owned Pokemon record.

    Old prototype captures may still be readable by the Shutterdex with fewer
    moves or battle tags.  They cannot enter a battle until capture generation
    repairs them, which avoids a combat rule silently inventing a loadout.
    """

    pokemon = _validated_pokemon(pokemon)
    if len(pokemon.moves) != 4:
        raise ValueError("A battle Pokemon needs exactly four moves.")
    if len(set(pokemon.moves)) != 4:
        raise ValueError("Battle Pokemon moves cannot be duplicated.")
    if not 2 <= len(pokemon.battle_natures) <= 4:
        raise ValueError("A battle Pokemon needs two to four battle-nature tags.")
    return _validated_battle_pokemon_snapshot(
        BattlePokemonSnapshot(
            pokemon_id=pokemon.pokemon_id,
            name=pokemon.name,
            species=pokemon.species,
            types=pokemon.types,
            stats=pokemon.stats,
            moves=pokemon.moves,
            battle_natures=pokemon.battle_natures,
            flavour=pokemon.flavour,
            rarity=pokemon.rarity,
            max_hp=int(pokemon.stats["hp"]),
            current_hp=int(pokemon.stats["hp"]),
            sprite_path=pokemon.sprite_path,
        )
    )


def battle_pokemon_snapshot_to_dict(snapshot: BattlePokemonSnapshot) -> dict[str, JSONValue]:
    snapshot = _validated_battle_pokemon_snapshot(snapshot)
    return {
        "pokemon_id": snapshot.pokemon_id,
        "name": snapshot.name,
        "species": snapshot.species,
        "types": list(snapshot.types),
        "type": snapshot.types[0],
        "stats": dict(snapshot.stats),
        "moves": list(snapshot.moves),
        "battle_natures": list(snapshot.battle_natures),
        "flavour": snapshot.flavour,
        "rarity": snapshot.rarity,
        "max_hp": snapshot.max_hp,
        "current_hp": snapshot.current_hp,
        "fainted": snapshot.fainted,
        "sprite_path": snapshot.sprite_path,
        "stat_stages": dict(snapshot.stat_stages),
    }


def battle_roster_snapshot_to_dict(roster: BattleRosterSnapshot) -> dict[str, JSONValue]:
    roster = _validated_battle_roster_snapshot(roster)
    return {
        "pokemon": [battle_pokemon_snapshot_to_dict(snapshot) for snapshot in roster.pokemon],
        "active_index": roster.active_index,
    }


def battle_candidate_outcome_to_dict(candidate: BattleCandidateOutcome) -> dict[str, JSONValue]:
    candidate = _validated_battle_candidate_outcome(candidate)
    return {
        "candidate_id": candidate.candidate_id,
        "summary": candidate.summary,
        "rationale": candidate.rationale,
        "actor_hp_delta": candidate.actor_hp_delta,
        "target_hp_delta": candidate.target_hp_delta,
        "actor_stat": candidate.actor_stat,
        "actor_stat_delta": candidate.actor_stat_delta,
        "target_stat": candidate.target_stat,
        "target_stat_delta": candidate.target_stat_delta,
    }


def battle_turn_to_dict(turn: BattleTurnRecord) -> dict[str, JSONValue]:
    turn = _validated_battle_turn(turn)
    return {
        "turn_id": turn.turn_id,
        "battle_id": turn.battle_id,
        "turn_number": turn.turn_number,
        "attempt": turn.attempt,
        "acting_player_id": turn.acting_player_id,
        "actor_pokemon_id": turn.actor_pokemon_id,
        "move_id": turn.move_id,
        "candidate_outcomes": [
            battle_candidate_outcome_to_dict(candidate) for candidate in turn.candidate_outcomes
        ],
        "status": turn.status,
        "selected_candidate_id": turn.selected_candidate_id,
        "visible_rationale": turn.visible_rationale,
        "state_after": (
            json_object(turn.state_after, field_name="state_after")
            if turn.state_after is not None
            else None
        ),
        "created_at": datetime_to_storage(turn.created_at),
        "resolved_at": (
            datetime_to_storage(turn.resolved_at) if turn.resolved_at is not None else None
        ),
        "abort_reason": turn.abort_reason,
    }


def battle_record_to_dict(battle: BattleRecord) -> dict[str, JSONValue]:
    """Serialize a shared battle state without any badge credential."""

    battle = _validated_battle_record(battle)
    challenger: dict[str, JSONValue] = {
        "player_id": battle.challenger_player_id,
        "display_name": battle.challenger_display_name,
        "badge_id": battle.challenger_badge_id,
        "ready": battle.challenger_ready,
        "roster": battle_roster_snapshot_to_dict(battle.challenger_roster),
    }
    opponent: dict[str, JSONValue] = {
        "player_id": battle.opponent_player_id,
        "display_name": battle.opponent_display_name,
        "badge_id": battle.opponent_badge_id,
        "ready": battle.opponent_ready,
        "roster": battle_roster_snapshot_to_dict(battle.opponent_roster),
    }
    return {
        "battle_id": battle.battle_id,
        "challenger": challenger,
        "opponent": opponent,
        # Flat aliases keep the renderer/API mapping pleasantly small.
        "challenger_player_id": battle.challenger_player_id,
        "opponent_player_id": battle.opponent_player_id,
        "challenger_ready": battle.challenger_ready,
        "opponent_ready": battle.opponent_ready,
        "status": battle.status,
        "current_player_id": battle.current_player_id,
        "turn_number": battle.turn_number,
        "revision": battle.revision,
        "ready_deadline_at": (
            datetime_to_storage(battle.ready_deadline_at)
            if battle.ready_deadline_at is not None
            else None
        ),
        "turn_deadline_at": (
            datetime_to_storage(battle.turn_deadline_at)
            if battle.turn_deadline_at is not None
            else None
        ),
        "disconnect_deadline_at": (
            datetime_to_storage(battle.disconnect_deadline_at)
            if battle.disconnect_deadline_at is not None
            else None
        ),
        "disconnected_player_id": battle.disconnected_player_id,
        "winner_player_id": battle.winner_player_id,
        "cancelled_by_player_id": battle.cancelled_by_player_id,
        "end_reason": battle.end_reason,
        "notice": battle.notice,
        "last_visible_rationale": battle.last_visible_rationale,
        "created_at": datetime_to_storage(battle.created_at),
        "updated_at": datetime_to_storage(battle.updated_at),
    }


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(str(value), field_name)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS badges (
    badge_id TEXT PRIMARY KEY,
    htn_id TEXT NOT NULL UNIQUE,
    player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
    app_key_ciphertext TEXT NOT NULL,
    last_seen_at TEXT,
    online INTEGER NOT NULL DEFAULT 0 CHECK (online IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS badges_by_player ON badges(player_id);

CREATE TABLE IF NOT EXISTS pokemon (
    pokemon_id TEXT PRIMARY KEY,
    owner_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    captured_by_badge_id TEXT REFERENCES badges(badge_id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    species TEXT NOT NULL,
    types_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    moves_json TEXT NOT NULL,
    battle_natures_json TEXT NOT NULL DEFAULT '[]',
    flavour TEXT NOT NULL,
    sprite_prompt TEXT NOT NULL,
    rarity TEXT NOT NULL,
    sprite_path TEXT,
    caught_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pokemon_by_owner ON pokemon(owner_player_id, caught_at DESC);

CREATE TABLE IF NOT EXISTS pokemon_world_state (
    pokemon_id TEXT PRIMARY KEY REFERENCES pokemon(pokemon_id) ON DELETE CASCADE,
    x INTEGER NOT NULL CHECK (x BETWEEN 0 AND 100),
    y INTEGER NOT NULL CHECK (y BETWEEN 0 AND 100),
    mood TEXT NOT NULL,
    energy INTEGER NOT NULL CHECK (energy BETWEEN 0 AND 100),
    activity TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pokemon_transfers (
    transfer_id TEXT PRIMARY KEY,
    pokemon_id TEXT NOT NULL REFERENCES pokemon(pokemon_id) ON DELETE RESTRICT,
    from_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    to_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    transferred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pokemon_transfers_by_pokemon
    ON pokemon_transfers(pokemon_id, transferred_at DESC);

CREATE TABLE IF NOT EXISTS simulation_events (
    event_id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    actor_pokemon_id TEXT NOT NULL REFERENCES pokemon(pokemon_id) ON DELETE RESTRICT,
    target_pokemon_id TEXT REFERENCES pokemon(pokemon_id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    dialogue_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS simulation_events_by_player
    ON simulation_events(player_id, revision DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS badge_sessions (
    badge_id TEXT PRIMARY KEY REFERENCES badges(badge_id) ON DELETE CASCADE,
    active_app TEXT NOT NULL,
    app_state_json TEXT NOT NULL,
    canvas_active INTEGER NOT NULL DEFAULT 0 CHECK (canvas_active IN (0, 1)),
    last_render_hash TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TEXT NOT NULL
);

-- Battle profiles are JSON snapshots, not foreign-keyed live Pokemon fields:
-- an accepted challenge must remain reproducible if a Pokemon is later traded
-- or edited.  The two player IDs and optional badge endpoints remain foreign
-- keys for authorization and live delivery.
CREATE TABLE IF NOT EXISTS battles (
    battle_id TEXT PRIMARY KEY,
    challenger_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    challenger_display_name TEXT NOT NULL,
    challenger_badge_id TEXT REFERENCES badges(badge_id) ON DELETE SET NULL,
    challenger_roster_json TEXT NOT NULL,
    challenger_ready INTEGER NOT NULL DEFAULT 0 CHECK (challenger_ready IN (0, 1)),
    opponent_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    opponent_display_name TEXT NOT NULL,
    opponent_badge_id TEXT REFERENCES badges(badge_id) ON DELETE SET NULL,
    opponent_roster_json TEXT NOT NULL,
    opponent_ready INTEGER NOT NULL DEFAULT 0 CHECK (opponent_ready IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN (
        'challenge', 'ready', 'active', 'resolving', 'finished', 'cancelled', 'disconnected', 'timed_out'
    )),
    current_player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
    turn_number INTEGER NOT NULL DEFAULT 0 CHECK (turn_number >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    ready_deadline_at TEXT,
    turn_deadline_at TEXT,
    disconnect_deadline_at TEXT,
    disconnected_player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
    status_before_disconnect TEXT,
    winner_player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
    cancelled_by_player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
    end_reason TEXT,
    notice TEXT,
    last_visible_rationale TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (challenger_player_id <> opponent_player_id)
);

CREATE INDEX IF NOT EXISTS battles_by_challenger
    ON battles(challenger_player_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS battles_by_opponent
    ON battles(opponent_player_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS battles_by_deadline
    ON battles(status, ready_deadline_at, turn_deadline_at, disconnect_deadline_at);

CREATE TABLE IF NOT EXISTS battle_turns (
    turn_id TEXT PRIMARY KEY,
    battle_id TEXT NOT NULL REFERENCES battles(battle_id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    acting_player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE RESTRICT,
    actor_pokemon_id TEXT NOT NULL,
    move_id TEXT NOT NULL,
    candidate_outcomes_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolving', 'resolved', 'aborted')),
    selected_candidate_id TEXT,
    visible_rationale TEXT,
    state_after_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    abort_reason TEXT,
    UNIQUE(battle_id, turn_number, attempt)
);

CREATE INDEX IF NOT EXISTS battle_turns_by_battle
    ON battle_turns(battle_id, turn_number DESC, attempt DESC);
"""


class BadgeStore:
    """Thread-safe repository over one SQLite database connection.

    The methods are synchronous by design.  Call them from FastAPI handlers
    through ``asyncio.to_thread`` if a deployment needs to avoid brief SQLite
    work on the event loop.  Every write is protected by ``BEGIN IMMEDIATE``;
    in particular, a trade cannot partially update a Pokemon owner.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        if self.database_path != Path(":memory:"):
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.connection = sqlite3.connect(
            str(self.database_path), check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        # WAL allows a renderer read to coexist with a short write transaction.
        if self.database_path != Path(":memory:"):
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self.connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(pokemon)").fetchall()
            }
            if "battle_natures_json" not in columns:
                self.connection.execute(
                    "ALTER TABLE pokemon ADD COLUMN battle_natures_json TEXT NOT NULL DEFAULT '[]'"
                )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "BadgeStore":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize an all-or-nothing write transaction."""

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _fetchone(self, query: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(query, params).fetchone()

    def _fetchall(self, query: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(query, params).fetchall())

    # -- Players ---------------------------------------------------------

    def create_player(self, display_name: str, *, player_id: str | None = None) -> Player:
        player_id = _identifier(player_id or _new_id("player"), "player_id")
        display_name = _text(display_name, "display_name", maximum=80)
        now = utc_now()
        timestamp = datetime_to_storage(now)
        try:
            with self._transaction():
                self.connection.execute(
                    """
                    INSERT INTO players(player_id, display_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (player_id, display_name, timestamp, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Player {player_id!r} already exists.") from exc
        return Player(player_id, display_name, now, now)

    def get_player(self, player_id: str) -> Player | None:
        row = self._fetchone(
            "SELECT player_id, display_name, created_at, updated_at FROM players WHERE player_id = ?",
            (_identifier(player_id, "player_id"),),
        )
        return _player_from_row(row) if row is not None else None

    def require_player(self, player_id: str) -> Player:
        player = self.get_player(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id!r} was not found.")
        return player

    def list_players(self) -> list[Player]:
        return [
            _player_from_row(row)
            for row in self._fetchall(
                "SELECT player_id, display_name, created_at, updated_at FROM players ORDER BY created_at"
            )
        ]

    # -- Badges ----------------------------------------------------------

    def create_badge(
        self,
        htn_id: str,
        app_key_ciphertext: str,
        *,
        player_id: str | None = None,
        badge_id: str | None = None,
    ) -> Badge:
        """Register a badge with an opaque app-key ciphertext.

        The caller must encrypt the key before calling this method.  A badge is
        allowed to be unclaimed during pairing, then assigned later.
        """

        badge_id = _identifier(badge_id or _new_id("badge"), "badge_id")
        htn_id = _text(htn_id, "htn_id", maximum=64)
        app_key_ciphertext = _text(
            app_key_ciphertext, "app_key_ciphertext", maximum=16384
        )
        owner = _optional_identifier(player_id, "player_id")
        now = utc_now()
        timestamp = datetime_to_storage(now)
        try:
            with self._transaction():
                if owner is not None:
                    self._require_player_in_transaction(owner)
                self.connection.execute(
                    """
                    INSERT INTO badges(
                        badge_id, htn_id, player_id, app_key_ciphertext, last_seen_at,
                        online, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (badge_id, htn_id, owner, app_key_ciphertext, timestamp, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Badge ID or HTN ID already exists: {htn_id!r}.") from exc
        return Badge(badge_id, htn_id, owner, app_key_ciphertext, None, False, now, now)

    def get_badge(self, badge_id: str) -> Badge | None:
        row = self._fetchone("SELECT * FROM badges WHERE badge_id = ?", (_identifier(badge_id, "badge_id"),))
        return _badge_from_row(row) if row is not None else None

    def require_badge(self, badge_id: str) -> Badge:
        badge = self.get_badge(badge_id)
        if badge is None:
            raise NotFoundError(f"Badge {badge_id!r} was not found.")
        return badge

    def get_badge_by_htn_id(self, htn_id: str) -> Badge | None:
        row = self._fetchone("SELECT * FROM badges WHERE htn_id = ?", (_text(htn_id, "htn_id", maximum=64),))
        return _badge_from_row(row) if row is not None else None

    def require_badge_by_htn_id(self, htn_id: str) -> Badge:
        badge = self.get_badge_by_htn_id(htn_id)
        if badge is None:
            raise NotFoundError(f"HTN badge {htn_id!r} was not found.")
        return badge

    def list_badges_for_player(self, player_id: str) -> list[Badge]:
        owner = _identifier(player_id, "player_id")
        return [
            _badge_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM badges WHERE player_id = ? ORDER BY created_at", (owner,)
            )
        ]

    def list_badges(self) -> list[Badge]:
        """Return every paired badge for connection-manager restoration.

        Callers must still treat ``app_key_ciphertext`` as opaque and must not
        serialize these records in a user-facing response.  This read exists
        so a restarted single-process app server can reopen its outbound app
        sockets without needing a player to press a dashboard button first.
        """

        return [
            _badge_from_row(row)
            for row in self._fetchall("SELECT * FROM badges ORDER BY created_at")
        ]

    def assign_badge(self, badge_id: str, player_id: str | None) -> Badge:
        """Claim, move, or unclaim a badge without altering its app key."""

        badge_id = _identifier(badge_id, "badge_id")
        owner = _optional_identifier(player_id, "player_id")
        now = utc_now()
        with self._transaction():
            if owner is not None:
                self._require_player_in_transaction(owner)
            row = self.connection.execute("SELECT * FROM badges WHERE badge_id = ?", (badge_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Badge {badge_id!r} was not found.")
            self.connection.execute(
                "UPDATE badges SET player_id = ?, updated_at = ? WHERE badge_id = ?",
                (owner, datetime_to_storage(now), badge_id),
            )
            updated = self.connection.execute("SELECT * FROM badges WHERE badge_id = ?", (badge_id,)).fetchone()
        assert updated is not None
        return _badge_from_row(updated)

    def replace_badge_credential(self, badge_id: str, app_key_ciphertext: str) -> Badge:
        """Rotate the opaque application credential for a paired badge."""

        badge_id = _identifier(badge_id, "badge_id")
        app_key_ciphertext = _text(
            app_key_ciphertext, "app_key_ciphertext", maximum=16384
        )
        now = utc_now()
        with self._transaction():
            cursor = self.connection.execute(
                "UPDATE badges SET app_key_ciphertext = ?, updated_at = ? WHERE badge_id = ?",
                (app_key_ciphertext, datetime_to_storage(now), badge_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Badge {badge_id!r} was not found.")
            row = self.connection.execute("SELECT * FROM badges WHERE badge_id = ?", (badge_id,)).fetchone()
        assert row is not None
        return _badge_from_row(row)

    def mark_badge_seen(
        self, badge_id: str, *, online: bool, seen_at: datetime | None = None
    ) -> Badge:
        """Persist reachability reported by the gateway's connection manager."""

        badge_id = _identifier(badge_id, "badge_id")
        seen_at = seen_at or utc_now()
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE badges
                SET last_seen_at = ?, online = ?, updated_at = ?
                WHERE badge_id = ?
                """,
                (
                    datetime_to_storage(seen_at),
                    int(bool(online)),
                    datetime_to_storage(seen_at),
                    badge_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Badge {badge_id!r} was not found.")
            row = self.connection.execute("SELECT * FROM badges WHERE badge_id = ?", (badge_id,)).fetchone()
        assert row is not None
        return _badge_from_row(row)

    # -- Pokemon and ownership ------------------------------------------

    def create_pokemon(self, pokemon: PokemonRecord) -> PokemonRecord:
        """Persist a newly captured Pokemon under its current owner."""

        pokemon = _validated_pokemon(pokemon)
        try:
            with self._transaction():
                self._require_player_in_transaction(pokemon.owner_player_id)
                if pokemon.captured_by_badge_id is not None:
                    self._require_badge_in_transaction(pokemon.captured_by_badge_id)
                self.connection.execute(
                    """
                    INSERT INTO pokemon(
                        pokemon_id, owner_player_id, captured_by_badge_id, name, species,
                        types_json, stats_json, moves_json, battle_natures_json, flavour, sprite_prompt, rarity,
                        sprite_path, caught_at, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _pokemon_params(pokemon),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Pokemon {pokemon.pokemon_id!r} already exists.") from exc
        return pokemon

    def get_pokemon(self, pokemon_id: str) -> PokemonRecord | None:
        row = self._fetchone("SELECT * FROM pokemon WHERE pokemon_id = ?", (_identifier(pokemon_id, "pokemon_id"),))
        return _pokemon_from_row(row) if row is not None else None

    def require_pokemon(self, pokemon_id: str) -> PokemonRecord:
        pokemon = self.get_pokemon(pokemon_id)
        if pokemon is None:
            raise NotFoundError(f"Pokemon {pokemon_id!r} was not found.")
        return pokemon

    def list_pokemon_for_player(self, player_id: str) -> list[PokemonRecord]:
        owner = _identifier(player_id, "player_id")
        return [
            _pokemon_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM pokemon WHERE owner_player_id = ? ORDER BY caught_at DESC, pokemon_id",
                (owner,),
            )
        ]

    def replace_pokemon(self, pokemon: PokemonRecord, *, expected_owner_player_id: str | None = None) -> PokemonRecord:
        """Replace profile fields without allowing this method to perform a trade.

        Ownership changes must use :meth:`transfer_pokemon`, ensuring they are
        audited.  This method is suitable for attaching a generated sprite or
        editing a profile after image processing finishes.
        """

        pokemon = _validated_pokemon(pokemon)
        expected = _optional_identifier(expected_owner_player_id, "expected_owner_player_id")
        with self._transaction():
            existing = self.connection.execute(
                "SELECT owner_player_id, created_at FROM pokemon WHERE pokemon_id = ?",
                (pokemon.pokemon_id,),
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Pokemon {pokemon.pokemon_id!r} was not found.")
            if existing["owner_player_id"] != pokemon.owner_player_id:
                raise OwnershipError("Use transfer_pokemon to change a Pokemon owner.")
            if expected is not None and existing["owner_player_id"] != expected:
                raise OwnershipError("Pokemon owner changed before the update could be applied.")
            # Preserve creation time even if a stale caller supplied another one.
            pokemon = PokemonRecord(
                **{**asdict(pokemon), "created_at": datetime_from_storage(existing["created_at"]), "updated_at": utc_now()}
            )
            self.connection.execute(
                """
                UPDATE pokemon SET
                    owner_player_id = ?, captured_by_badge_id = ?, name = ?, species = ?,
                    types_json = ?, stats_json = ?, moves_json = ?, battle_natures_json = ?, flavour = ?,
                    sprite_prompt = ?, rarity = ?, sprite_path = ?, caught_at = ?,
                    metadata_json = ?, created_at = ?, updated_at = ?
                WHERE pokemon_id = ?
                """,
                _pokemon_update_params(pokemon),
            )
        return pokemon

    def update_pokemon_sprite(
        self,
        pokemon_id: str,
        sprite_path: str | None,
        *,
        expected_owner_player_id: str | None = None,
    ) -> PokemonRecord:
        """Attach/remove a server-hosted sprite while preserving ownership."""

        pokemon_id = _identifier(pokemon_id, "pokemon_id")
        owner = _optional_identifier(expected_owner_player_id, "expected_owner_player_id")
        path = _optional_text(sprite_path, "sprite_path", maximum=512)
        now = utc_now()
        with self._transaction():
            row = self.connection.execute("SELECT * FROM pokemon WHERE pokemon_id = ?", (pokemon_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Pokemon {pokemon_id!r} was not found.")
            if owner is not None and row["owner_player_id"] != owner:
                raise OwnershipError("Pokemon does not belong to the expected player.")
            self.connection.execute(
                "UPDATE pokemon SET sprite_path = ?, updated_at = ? WHERE pokemon_id = ?",
                (path, datetime_to_storage(now), pokemon_id),
            )
            updated = self.connection.execute("SELECT * FROM pokemon WHERE pokemon_id = ?", (pokemon_id,)).fetchone()
        assert updated is not None
        return _pokemon_from_row(updated)

    def transfer_pokemon(
        self,
        pokemon_id: str,
        to_player_id: str,
        *,
        expected_owner_player_id: str | None = None,
        reason: str = "trade",
        transferred_at: datetime | None = None,
    ) -> tuple[PokemonRecord, OwnershipTransfer]:
        """Atomically move one Pokemon and append its ownership audit record.

        ``expected_owner_player_id`` is recommended for a multiplayer trade;
        it prevents a stale confirmation from moving a Pokemon that was traded
        in another request moments earlier.
        """

        pokemon_id = _identifier(pokemon_id, "pokemon_id")
        to_player_id = _identifier(to_player_id, "to_player_id")
        expected_owner = _optional_identifier(expected_owner_player_id, "expected_owner_player_id")
        reason = _text(reason, "reason", maximum=96)
        transferred_at = transferred_at or utc_now()
        now_text = datetime_to_storage(transferred_at)
        transfer = OwnershipTransfer(
            transfer_id=_new_id("transfer"),
            pokemon_id=pokemon_id,
            from_player_id="",  # Filled once the protected current owner is read.
            to_player_id=to_player_id,
            reason=reason,
            transferred_at=transferred_at,
        )
        with self._transaction():
            self._require_player_in_transaction(to_player_id)
            row = self.connection.execute("SELECT * FROM pokemon WHERE pokemon_id = ?", (pokemon_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Pokemon {pokemon_id!r} was not found.")
            from_player_id = row["owner_player_id"]
            if expected_owner is not None and from_player_id != expected_owner:
                raise OwnershipError("Pokemon owner changed before this transfer was confirmed.")
            if from_player_id == to_player_id:
                raise ConflictError("A Pokemon cannot be transferred to its current owner.")
            cursor = self.connection.execute(
                """
                UPDATE pokemon SET owner_player_id = ?, updated_at = ?
                WHERE pokemon_id = ? AND owner_player_id = ?
                """,
                (to_player_id, now_text, pokemon_id, from_player_id),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("Pokemon owner changed before this transfer was applied.")
            transfer = OwnershipTransfer(
                transfer_id=transfer.transfer_id,
                pokemon_id=pokemon_id,
                from_player_id=from_player_id,
                to_player_id=to_player_id,
                reason=reason,
                transferred_at=transferred_at,
            )
            self.connection.execute(
                """
                INSERT INTO pokemon_transfers(
                    transfer_id, pokemon_id, from_player_id, to_player_id, reason, transferred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer.transfer_id,
                    transfer.pokemon_id,
                    transfer.from_player_id,
                    transfer.to_player_id,
                    transfer.reason,
                    now_text,
                ),
            )
            updated = self.connection.execute("SELECT * FROM pokemon WHERE pokemon_id = ?", (pokemon_id,)).fetchone()
        assert updated is not None
        return _pokemon_from_row(updated), transfer

    def list_pokemon_transfers(self, pokemon_id: str) -> list[OwnershipTransfer]:
        pokemon_id = _identifier(pokemon_id, "pokemon_id")
        return [
            _transfer_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM pokemon_transfers WHERE pokemon_id = ? ORDER BY transferred_at DESC",
                (pokemon_id,),
            )
        ]

    def list_pokemon(self) -> list[PokemonRecord]:
        """Return every captured Pokemon for maintenance-only operations.

        Normal gameplay should always use :meth:`list_pokemon_for_player` so
        ownership remains explicit.  This narrow administrative read supports
        schema/data repairs that must visit legacy rows across all players.
        """

        return [
            _pokemon_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM pokemon ORDER BY owner_player_id, caught_at DESC, pokemon_id"
            )
        ]

    # -- Shared two-player battle state ---------------------------------

    def build_battle_roster(
        self, player_id: str, *, limit: int = 6
    ) -> BattleRosterSnapshot:
        """Snapshot up to the player's most recently caught battle-ready Pokemon.

        This is intentionally a convenience method rather than an implicit
        side effect of challenge creation.  A caller may later offer a
        deliberate subset, while the default challenge flow gets the six most
        recent captures in the exact order shown by the Shutterdex.
        """

        player_id = _identifier(player_id, "player_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 6:
            raise ValueError("limit must be an integer from 1 to 6.")
        self.require_player(player_id)
        pokemon = self.list_pokemon_for_player(player_id)[:limit]
        if not pokemon:
            raise ValueError("A player needs at least one Pokemon to battle.")
        return _validated_battle_roster_snapshot(
            BattleRosterSnapshot(
                pokemon=tuple(battle_pokemon_snapshot_from_pokemon(record) for record in pokemon)
            )
        )

    def create_battle_challenge(
        self,
        challenger_player_id: str,
        opponent_player_id: str,
        *,
        challenger_roster: BattleRosterSnapshot | None = None,
        opponent_roster: BattleRosterSnapshot | None = None,
        challenger_badge_id: str | None = None,
        opponent_badge_id: str | None = None,
        ready_deadline_at: datetime | None = None,
        notice: str | None = None,
        battle_id: str | None = None,
        created_at: datetime | None = None,
    ) -> BattleRecord:
        """Atomically create a pending two-player challenge.

        Both rosters are rechecked against current Pokemon ownership and
        profile data inside the transaction.  That means stale browser state,
        an NFC trade, or a profile edit cannot sneak a forged combatant into a
        battle after the challenge button was pressed.
        """

        challenger_player_id = _identifier(challenger_player_id, "challenger_player_id")
        opponent_player_id = _identifier(opponent_player_id, "opponent_player_id")
        if challenger_player_id == opponent_player_id:
            raise ValueError("A player cannot challenge themself.")
        challenger_badge_id = _optional_identifier(challenger_badge_id, "challenger_badge_id")
        opponent_badge_id = _optional_identifier(opponent_badge_id, "opponent_badge_id")
        battle_id = _identifier(battle_id or _new_id("battle"), "battle_id")
        now = _normalise_datetime(created_at or utc_now(), "created_at")
        ready_deadline = (
            _normalise_datetime(ready_deadline_at, "ready_deadline_at")
            if ready_deadline_at is not None
            else None
        )
        if ready_deadline is not None and ready_deadline <= now:
            raise ValueError("ready_deadline_at must be in the future.")
        safe_notice = _optional_text(notice, "notice", maximum=400) or "Challenge sent."

        # Make snapshots outside the write transaction for the ordinary
        # convenience path.  The protected ownership/profile comparison below
        # still catches any change that lands before the INSERT.
        challenger_roster = _validated_battle_roster_snapshot(
            challenger_roster or self.build_battle_roster(challenger_player_id)
        )
        opponent_roster = _validated_battle_roster_snapshot(
            opponent_roster or self.build_battle_roster(opponent_player_id)
        )
        _ensure_distinct_battle_rosters(challenger_roster, opponent_roster)

        try:
            with self._transaction():
                challenger = self.connection.execute(
                    "SELECT * FROM players WHERE player_id = ?", (challenger_player_id,)
                ).fetchone()
                opponent = self.connection.execute(
                    "SELECT * FROM players WHERE player_id = ?", (opponent_player_id,)
                ).fetchone()
                if challenger is None:
                    raise NotFoundError(f"Player {challenger_player_id!r} was not found.")
                if opponent is None:
                    raise NotFoundError(f"Player {opponent_player_id!r} was not found.")
                self._require_badge_owned_by_player_in_transaction(
                    challenger_badge_id, challenger_player_id, "challenger_badge_id"
                )
                self._require_badge_owned_by_player_in_transaction(
                    opponent_badge_id, opponent_player_id, "opponent_badge_id"
                )
                self._require_no_open_battle_in_transaction(challenger_player_id)
                self._require_no_open_battle_in_transaction(opponent_player_id)
                self._require_battle_roster_owned_in_transaction(
                    challenger_player_id, challenger_roster
                )
                self._require_battle_roster_owned_in_transaction(
                    opponent_player_id, opponent_roster
                )
                record = BattleRecord(
                    battle_id=battle_id,
                    challenger_player_id=challenger_player_id,
                    challenger_display_name=challenger["display_name"],
                    challenger_roster=challenger_roster,
                    challenger_ready=False,
                    opponent_player_id=opponent_player_id,
                    opponent_display_name=opponent["display_name"],
                    opponent_roster=opponent_roster,
                    opponent_ready=False,
                    status="challenge",
                    current_player_id=None,
                    turn_number=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                    challenger_badge_id=challenger_badge_id,
                    opponent_badge_id=opponent_badge_id,
                    ready_deadline_at=ready_deadline,
                    notice=safe_notice,
                )
                self.connection.execute(
                    """
                    INSERT INTO battles(
                        battle_id, challenger_player_id, challenger_display_name,
                        challenger_badge_id, challenger_roster_json, challenger_ready,
                        opponent_player_id, opponent_display_name, opponent_badge_id,
                        opponent_roster_json, opponent_ready, status, current_player_id,
                        turn_number, revision, ready_deadline_at, turn_deadline_at,
                        disconnect_deadline_at, disconnected_player_id,
                        status_before_disconnect, winner_player_id, cancelled_by_player_id,
                        end_reason, notice, last_visible_rationale, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _battle_insert_params(record),
                )
                row = self.connection.execute(
                    "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Battle {battle_id!r} already exists.") from exc
        assert row is not None
        return _battle_from_row(row)

    def get_battle(self, battle_id: str) -> BattleRecord | None:
        row = self._fetchone(
            "SELECT * FROM battles WHERE battle_id = ?", (_identifier(battle_id, "battle_id"),)
        )
        return _battle_from_row(row) if row is not None else None

    def require_battle(self, battle_id: str) -> BattleRecord:
        battle = self.get_battle(battle_id)
        if battle is None:
            raise NotFoundError(f"Battle {battle_id!r} was not found.")
        return battle

    def list_battles_for_player(
        self,
        player_id: str,
        *,
        include_terminal: bool = True,
        limit: int = 50,
    ) -> list[BattleRecord]:
        """List one player's newest shared battles, latest first."""

        player_id = _identifier(player_id, "player_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500.")
        terminal = tuple(sorted(_BATTLE_TERMINAL_STATUSES))
        if include_terminal:
            rows = self._fetchall(
                """
                SELECT * FROM battles
                WHERE challenger_player_id = ? OR opponent_player_id = ?
                ORDER BY updated_at DESC, battle_id DESC LIMIT ?
                """,
                (player_id, player_id, limit),
            )
        else:
            placeholders = ", ".join("?" for _ in terminal)
            rows = self._fetchall(
                f"""
                SELECT * FROM battles
                WHERE (challenger_player_id = ? OR opponent_player_id = ?)
                  AND status NOT IN ({placeholders})
                ORDER BY updated_at DESC, battle_id DESC LIMIT ?
                """,
                (player_id, player_id, *terminal, limit),
            )
        return [_battle_from_row(row) for row in rows]

    def get_open_battle_for_player(self, player_id: str) -> BattleRecord | None:
        """Return the one non-terminal battle a player may currently occupy."""

        player_id = _identifier(player_id, "player_id")
        terminal = tuple(sorted(_BATTLE_TERMINAL_STATUSES))
        placeholders = ", ".join("?" for _ in terminal)
        row = self._fetchone(
            f"""
            SELECT * FROM battles
            WHERE (challenger_player_id = ? OR opponent_player_id = ?)
              AND status NOT IN ({placeholders})
            ORDER BY updated_at DESC, battle_id DESC LIMIT 1
            """,
            (player_id, player_id, *terminal),
        )
        return _battle_from_row(row) if row is not None else None

    def mark_battle_ready(
        self,
        battle_id: str,
        player_id: str,
        *,
        ready: bool = True,
        initial_player_id: str | None = None,
        turn_deadline_at: datetime | None = None,
        notice: str | None = None,
        expected_revision: int | None = None,
        updated_at: datetime | None = None,
    ) -> BattleRecord:
        """Set one participant's ready flag and activate when both agree.

        The challenger receives the first turn by default.  A caller may pass
        either participant as ``initial_player_id`` for a deterministic
        speed/seed rule, but cannot nominate an unrelated player.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        initial_player_id = _optional_identifier(initial_player_id, "initial_player_id")
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(updated_at or utc_now(), "updated_at")
        deadline = (
            _normalise_datetime(turn_deadline_at, "turn_deadline_at")
            if turn_deadline_at is not None
            else None
        )
        if deadline is not None and deadline <= now:
            raise ValueError("turn_deadline_at must be in the future.")
        safe_notice = _optional_text(notice, "notice", maximum=400)
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status not in {"challenge", "ready"}:
                raise ConflictError("Only a pending challenge can be readied.")
            if battle.ready_deadline_at is not None and battle.ready_deadline_at <= now:
                raise ConflictError("This challenge has already reached its ready deadline.")
            challenger_ready = bool(ready) if player_id == battle.challenger_player_id else battle.challenger_ready
            opponent_ready = bool(ready) if player_id == battle.opponent_player_id else battle.opponent_ready
            both_ready = challenger_ready and opponent_ready
            if both_ready:
                first = initial_player_id or battle.challenger_player_id
                self._require_battle_participant(battle, first)
                status: BattleStatus = "active"
                current_player_id: str | None = first
                turn_number = 1
                ready_deadline = None
                next_deadline = deadline
                default_notice = "Both players are ready. Choose a move."
            else:
                status = "ready" if challenger_ready or opponent_ready else "challenge"
                current_player_id = None
                turn_number = 0
                ready_deadline = battle.ready_deadline_at
                next_deadline = None
                default_notice = "Waiting for the other player to ready up."
            self.connection.execute(
                """
                UPDATE battles SET
                    challenger_ready = ?, opponent_ready = ?, status = ?, current_player_id = ?,
                    turn_number = ?, ready_deadline_at = ?, turn_deadline_at = ?, notice = ?,
                    revision = ?, updated_at = ?
                WHERE battle_id = ?
                """,
                (
                    int(challenger_ready),
                    int(opponent_ready),
                    status,
                    current_player_id,
                    turn_number,
                    _storage_datetime_or_none(ready_deadline),
                    _storage_datetime_or_none(next_deadline),
                    safe_notice or default_notice,
                    battle.revision + 1,
                    datetime_to_storage(now),
                    battle_id,
                ),
            )
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def begin_battle_resolution(
        self,
        battle_id: str,
        player_id: str,
        *,
        move_id: str,
        candidate_outcomes: Sequence[BattleCandidateOutcome] = (),
        actor_pokemon_id: str | None = None,
        notice: str | None = None,
        expected_revision: int | None = None,
        started_at: datetime | None = None,
        resolution_deadline_at: datetime | None = None,
    ) -> tuple[BattleRecord, BattleTurnRecord]:
        """Claim the current turn before any slow external model work.

        The first durable operation for a submitted move must happen before
        invoking Writer or Jev.  It changes an ``active`` turn into
        ``resolving`` and swaps the player's move clock for a bounded
        resolution lease.  Therefore a move accepted before the move deadline
        cannot be lost merely because a model call takes time.

        ``candidate_outcomes`` may be empty while Writer is still running.
        Call :meth:`set_battle_resolution_candidates` before asking the
        Director to pick one.  Existing callers that already have candidates
        may still persist them atomically in this initial claim.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        move_id = _normalise_battle_move(move_id)
        actor_pokemon_id = _optional_identifier(actor_pokemon_id, "actor_pokemon_id")
        candidates = _normalise_battle_candidate_outcomes(
            candidate_outcomes, allow_empty=True
        )
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(started_at or utc_now(), "started_at")
        resolution_deadline = (
            _normalise_datetime(resolution_deadline_at, "resolution_deadline_at")
            if resolution_deadline_at is not None
            else now + timedelta(seconds=DEFAULT_BATTLE_RESOLUTION_LEASE_SECONDS)
        )
        if resolution_deadline <= now:
            raise ValueError("resolution_deadline_at must be in the future.")
        safe_notice = _optional_text(notice, "notice", maximum=400) or "The Director is choosing an outcome."
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status != "active":
                raise ConflictError("A move can only be selected during an active battle turn.")
            if battle.current_player_id != player_id:
                raise ConflictError("It is not this player's turn.")
            if battle.turn_deadline_at is not None and battle.turn_deadline_at <= now:
                raise ConflictError("This turn has already reached its deadline.")
            actor = _active_snapshot_for_player(battle, player_id)
            if actor.fainted:
                raise ConflictError("The active Pokemon has fainted and cannot choose a move.")
            if actor_pokemon_id is not None and actor_pokemon_id != actor.pokemon_id:
                raise ConflictError("The supplied actor is not the active Pokemon.")
            if move_id not in actor.moves:
                raise ValueError("The selected move does not belong to the active Pokemon.")
            previous_attempt = self.connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) AS last_attempt
                FROM battle_turns WHERE battle_id = ? AND turn_number = ?
                """,
                (battle_id, battle.turn_number),
            ).fetchone()
            assert previous_attempt is not None
            turn = BattleTurnRecord(
                turn_id=_new_id("turn"),
                battle_id=battle_id,
                turn_number=battle.turn_number,
                attempt=int(previous_attempt["last_attempt"]) + 1,
                acting_player_id=player_id,
                actor_pokemon_id=actor.pokemon_id,
                move_id=move_id,
                candidate_outcomes=candidates,
                status="resolving",
                created_at=now,
            )
            self.connection.execute(
                """
                INSERT INTO battle_turns(
                    turn_id, battle_id, turn_number, attempt, acting_player_id,
                    actor_pokemon_id, move_id, candidate_outcomes_json, status,
                    selected_candidate_id, visible_rationale, state_after_json,
                    created_at, resolved_at, abort_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)
                """,
                _battle_turn_insert_params(turn),
            )
            updated_battle = replace(
                battle,
                status="resolving",
                # The prior value was a player turn clock.  A resolving row
                # stores a separate lease instead, so expiry can recover a
                # crashed/abandoned model job without forfeiting this move.
                turn_deadline_at=resolution_deadline,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated), _validated_battle_turn(turn)

    def set_battle_resolution_candidates(
        self,
        battle_id: str,
        player_id: str,
        *,
        turn_id: str,
        candidate_outcomes: Sequence[BattleCandidateOutcome],
        expected_revision: int | None = None,
        updated_at: datetime | None = None,
    ) -> BattleTurnRecord:
        """Persist Writer choices for an already-claimed resolution.

        Saving candidates separately lets :meth:`begin_battle_resolution`
        safely pause the player clock before Writer starts.  The battle
        revision intentionally does not change here: the shared screen is
        already resolving, and the original claim revision remains the
        compare-and-swap token for commit or abort.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        turn_id = _identifier(turn_id, "turn_id")
        candidates = _normalise_battle_candidate_outcomes(candidate_outcomes)
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(updated_at or utc_now(), "updated_at")
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status != "resolving" or battle.current_player_id != player_id:
                raise ConflictError("This battle is not awaiting this player's resolution.")
            if (
                battle.turn_deadline_at is not None
                and battle.turn_deadline_at <= now
            ):
                raise ConflictError("This battle resolution lease has expired; retry the move.")
            turn_row = self.connection.execute(
                "SELECT * FROM battle_turns WHERE turn_id = ? AND battle_id = ?",
                (turn_id, battle_id),
            ).fetchone()
            if turn_row is None:
                raise NotFoundError(f"Battle turn {turn_id!r} was not found.")
            turn = _battle_turn_from_row(turn_row)
            if turn.status != "resolving" or turn.acting_player_id != player_id:
                raise ConflictError("That battle turn is not awaiting this player's resolution.")
            if turn.candidate_outcomes:
                raise ConflictError("Writer candidates were already saved for this battle turn.")
            self.connection.execute(
                "UPDATE battle_turns SET candidate_outcomes_json = ? WHERE turn_id = ?",
                (
                    json_dumps(
                        [battle_candidate_outcome_to_dict(candidate) for candidate in candidates]
                    ),
                    turn_id,
                ),
            )
            updated = self.connection.execute(
                "SELECT * FROM battle_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        assert updated is not None
        return _battle_turn_from_row(updated)

    def abort_battle_resolution(
        self,
        battle_id: str,
        player_id: str,
        *,
        turn_id: str,
        reason: str,
        notice: str | None = None,
        expected_revision: int | None = None,
        updated_at: datetime | None = None,
        retry_turn_deadline_at: datetime | None = None,
    ) -> BattleRecord:
        """Release a Writer/Jev lock without changing either roster.

        The attempted candidates remain in ``battle_turns`` as an audit entry,
        while the same player can safely retry their move on the same turn.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        turn_id = _identifier(turn_id, "turn_id")
        reason = _text(reason, "reason", maximum=400)
        safe_notice = _optional_text(notice, "notice", maximum=400) or reason
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(updated_at or utc_now(), "updated_at")
        retry_deadline = (
            _normalise_datetime(retry_turn_deadline_at, "retry_turn_deadline_at")
            if retry_turn_deadline_at is not None
            else now + timedelta(seconds=DEFAULT_BATTLE_TURN_TIMEOUT_SECONDS)
        )
        if retry_deadline <= now:
            raise ValueError("retry_turn_deadline_at must be in the future.")
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status != "resolving" or battle.current_player_id != player_id:
                raise ConflictError("There is no resolution for this player to abort.")
            turn_row = self.connection.execute(
                "SELECT * FROM battle_turns WHERE turn_id = ? AND battle_id = ?",
                (turn_id, battle_id),
            ).fetchone()
            if turn_row is None:
                raise NotFoundError(f"Battle turn {turn_id!r} was not found.")
            turn = _battle_turn_from_row(turn_row)
            if turn.status != "resolving" or turn.acting_player_id != player_id:
                raise ConflictError("That battle turn is not awaiting this player's resolution.")
            self.connection.execute(
                """
                UPDATE battle_turns SET status = 'aborted', abort_reason = ?, resolved_at = ?
                WHERE turn_id = ?
                """,
                (reason, datetime_to_storage(now), turn_id),
            )
            updated_battle = replace(
                battle,
                status="active",
                turn_deadline_at=retry_deadline,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def resolve_battle_turn(
        self,
        battle_id: str,
        player_id: str,
        *,
        turn_id: str,
        selected_candidate_id: str,
        challenger_roster: BattleRosterSnapshot,
        opponent_roster: BattleRosterSnapshot,
        next_player_id: str | None = None,
        winner_player_id: str | None = None,
        visible_rationale: str | None = None,
        notice: str | None = None,
        end_reason: str | None = None,
        next_turn_deadline_at: datetime | None = None,
        expected_revision: int | None = None,
        resolved_at: datetime | None = None,
    ) -> BattleRecord:
        """Atomically commit Jev's chosen outcome and the deterministic state.

        The supplied rosters must preserve every immutable profile field and
        can affect only the two active Pokemon.  A selected candidate must be
        one of the candidates saved by :meth:`begin_battle_resolution`.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        turn_id = _identifier(turn_id, "turn_id")
        selected_candidate_id = _identifier(selected_candidate_id, "selected_candidate_id")
        challenger_roster = _validated_battle_roster_snapshot(challenger_roster)
        opponent_roster = _validated_battle_roster_snapshot(opponent_roster)
        _ensure_distinct_battle_rosters(challenger_roster, opponent_roster)
        next_player_id = _optional_identifier(next_player_id, "next_player_id")
        winner_player_id = _optional_identifier(winner_player_id, "winner_player_id")
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(resolved_at or utc_now(), "resolved_at")
        deadline = (
            _normalise_datetime(next_turn_deadline_at, "next_turn_deadline_at")
            if next_turn_deadline_at is not None
            else None
        )
        if deadline is not None and deadline <= now:
            raise ValueError("next_turn_deadline_at must be in the future.")
        safe_end_reason = _optional_text(end_reason, "end_reason", maximum=160)
        safe_notice = _optional_text(notice, "notice", maximum=400)
        requested_rationale = _optional_text(
            visible_rationale, "visible_rationale", maximum=600
        )
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status != "resolving" or battle.current_player_id != player_id:
                raise ConflictError("This battle is not awaiting this player's resolution.")
            if (
                battle.turn_deadline_at is not None
                and battle.turn_deadline_at <= now
            ):
                raise ConflictError("This battle resolution lease has expired; retry the move.")
            turn_row = self.connection.execute(
                "SELECT * FROM battle_turns WHERE turn_id = ? AND battle_id = ?",
                (turn_id, battle_id),
            ).fetchone()
            if turn_row is None:
                raise NotFoundError(f"Battle turn {turn_id!r} was not found.")
            turn = _battle_turn_from_row(turn_row)
            if turn.status != "resolving" or turn.acting_player_id != player_id:
                raise ConflictError("That battle turn is not awaiting this player's resolution.")
            selected = next(
                (
                    candidate
                    for candidate in turn.candidate_outcomes
                    if candidate.candidate_id == selected_candidate_id
                ),
                None,
            )
            if selected is None:
                raise ValueError("The selected outcome was not proposed for this turn.")
            _validate_battle_resolution_rosters(
                battle.challenger_roster,
                battle.opponent_roster,
                challenger_roster,
                opponent_roster,
            )
            _validate_candidate_state_change(
                selected,
                battle,
                challenger_roster,
                opponent_roster,
            )
            challenger_fainted = _all_battle_pokemon_fainted(challenger_roster)
            opponent_fainted = _all_battle_pokemon_fainted(opponent_roster)
            if winner_player_id is not None:
                self._require_battle_participant(battle, winner_player_id)
                loser = _other_battle_player(battle, winner_player_id)
                loser_fainted = opponent_fainted if loser == battle.opponent_player_id else challenger_fainted
                if not loser_fainted:
                    raise ValueError("A move-resolution winner requires the opposing roster to faint.")
                status: BattleStatus = "finished"
                current_player_id: str | None = None
                turn_number = battle.turn_number
                turn_deadline = None
                default_end_reason = "knockout"
                default_notice = "The battle is over."
            else:
                if challenger_fainted or opponent_fainted:
                    raise ValueError("A winner is required when a roster has fainted.")
                expected_next = _other_battle_player(battle, player_id)
                if next_player_id is None:
                    next_player_id = expected_next
                if next_player_id != expected_next:
                    raise ValueError("A resolved move must pass the turn to the other player.")
                status = "active"
                current_player_id = next_player_id
                turn_number = battle.turn_number + 1
                turn_deadline = deadline
                default_end_reason = None
                default_notice = "Choose a move."
            rationale = requested_rationale or selected.rationale
            state_after: dict[str, JSONValue] = {
                "challenger_roster": battle_roster_snapshot_to_dict(challenger_roster),
                "opponent_roster": battle_roster_snapshot_to_dict(opponent_roster),
                "current_player_id": current_player_id,
                "winner_player_id": winner_player_id,
            }
            self.connection.execute(
                """
                UPDATE battle_turns SET
                    status = 'resolved', selected_candidate_id = ?, visible_rationale = ?,
                    state_after_json = ?, resolved_at = ?, abort_reason = NULL
                WHERE turn_id = ?
                """,
                (
                    selected_candidate_id,
                    rationale,
                    json_dumps(state_after),
                    datetime_to_storage(now),
                    turn_id,
                ),
            )
            updated_battle = replace(
                battle,
                challenger_roster=challenger_roster,
                opponent_roster=opponent_roster,
                status=status,
                current_player_id=current_player_id,
                turn_number=turn_number,
                turn_deadline_at=turn_deadline,
                winner_player_id=winner_player_id,
                end_reason=safe_end_reason or default_end_reason,
                notice=safe_notice or default_notice,
                last_visible_rationale=rationale,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def list_battle_turns(
        self, battle_id: str, *, limit: int = 100
    ) -> list[BattleTurnRecord]:
        """Return turn attempts in chronological order for replay/debug UI."""

        battle_id = _identifier(battle_id, "battle_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500.")
        # Require the parent record so a typo and an empty history are distinct.
        self.require_battle(battle_id)
        rows = self._fetchall(
            """
            SELECT * FROM battle_turns WHERE battle_id = ?
            ORDER BY turn_number DESC, attempt DESC LIMIT ?
            """,
            (battle_id, limit),
        )
        return list(reversed([_battle_turn_from_row(row) for row in rows]))

    def finish_battle(
        self,
        battle_id: str,
        winner_player_id: str,
        *,
        end_reason: str = "forfeit",
        notice: str | None = None,
        expected_revision: int | None = None,
        finished_at: datetime | None = None,
    ) -> BattleRecord:
        """Finish an open battle for a server-authoritative non-move result.

        Normal knockouts should go through :meth:`resolve_battle_turn`, which
        verifies the fainted roster.  This method is deliberately available for
        surrender, an adjudicated network result, or another explicit game
        rule that has no move-resolution payload.
        """

        battle_id = _identifier(battle_id, "battle_id")
        winner_player_id = _identifier(winner_player_id, "winner_player_id")
        end_reason = _text(end_reason, "end_reason", maximum=160)
        safe_notice = _optional_text(notice, "notice", maximum=400) or "The battle is over."
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(finished_at or utc_now(), "finished_at")
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, winner_player_id)
            if battle.status not in _BATTLE_OPEN_STATUSES:
                raise ConflictError("Only an open battle can be finished.")
            self._abort_pending_battle_turn_in_transaction(
                battle.battle_id, reason=end_reason, now=now
            )
            updated_battle = replace(
                battle,
                status="finished",
                current_player_id=None,
                ready_deadline_at=None,
                turn_deadline_at=None,
                disconnect_deadline_at=None,
                disconnected_player_id=None,
                status_before_disconnect=None,
                winner_player_id=winner_player_id,
                end_reason=end_reason,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def cancel_battle(
        self,
        battle_id: str,
        *,
        player_id: str | None = None,
        reason: str = "cancelled",
        notice: str | None = None,
        expected_revision: int | None = None,
        cancelled_at: datetime | None = None,
    ) -> BattleRecord:
        """Cancel an open challenge/battle and preserve why it ended.

        ``player_id`` is optional only for an internal server cancellation.  A
        normal badge action supplies it and is rejected unless the badge's
        player is one of the two participants.
        """

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _optional_identifier(player_id, "player_id")
        reason = _text(reason, "reason", maximum=160)
        safe_notice = _optional_text(notice, "notice", maximum=400) or "Battle cancelled."
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(cancelled_at or utc_now(), "cancelled_at")
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            if player_id is not None:
                self._require_battle_participant(battle, player_id)
            if battle.status not in _BATTLE_OPEN_STATUSES:
                raise ConflictError("Only an open battle can be cancelled.")
            self._abort_pending_battle_turn_in_transaction(
                battle.battle_id, reason=reason, now=now
            )
            updated_battle = replace(
                battle,
                status="cancelled",
                current_player_id=None,
                ready_deadline_at=None,
                turn_deadline_at=None,
                disconnect_deadline_at=None,
                disconnected_player_id=None,
                status_before_disconnect=None,
                cancelled_by_player_id=player_id,
                end_reason=reason,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def mark_battle_disconnected(
        self,
        battle_id: str,
        player_id: str,
        *,
        disconnect_deadline_at: datetime | None = None,
        notice: str | None = None,
        expected_revision: int | None = None,
        disconnected_at: datetime | None = None,
    ) -> BattleRecord:
        """Temporarily lock an open battle while one participant reconnects."""

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(disconnected_at or utc_now(), "disconnected_at")
        deadline = (
            _normalise_datetime(disconnect_deadline_at, "disconnect_deadline_at")
            if disconnect_deadline_at is not None
            else now + timedelta(seconds=30)
        )
        if deadline <= now:
            raise ValueError("disconnect_deadline_at must be in the future.")
        safe_notice = _optional_text(notice, "notice", maximum=400) or "Waiting for a player to reconnect."
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            self._require_battle_participant(battle, player_id)
            if battle.status in _BATTLE_TERMINAL_STATUSES:
                raise ConflictError("A finished battle cannot enter reconnect mode.")
            if battle.status == "disconnected":
                if battle.disconnected_player_id != player_id:
                    raise ConflictError("The other participant is already reconnecting.")
                original_status = battle.status_before_disconnect
                turn_deadline = battle.turn_deadline_at
            elif battle.status == "resolving":
                # A Writer/Director request lives in a process task, not in
                # SQLite.  It cannot safely resume after this participant
                # drops off the transport, so preserve the attempt for audit,
                # release its lock, and reconnect to the same active turn.
                # Leaving ``resolving`` here would strand both badges on a
                # screen that no worker is guaranteed to finish.
                self._abort_pending_battle_turn_in_transaction(
                    battle.battle_id,
                    reason="player_disconnected_during_resolution",
                    now=now,
                )
                original_status = "active"
                turn_deadline = None
            else:
                original_status = battle.status
                # The reconnect grace period pauses an active move timer. The
                # service supplies a fresh bounded deadline only after the
                # same participant returns.
                turn_deadline = None if battle.status == "active" else battle.turn_deadline_at
            assert original_status is not None
            updated_battle = replace(
                battle,
                status="disconnected",
                turn_deadline_at=turn_deadline,
                disconnect_deadline_at=deadline,
                disconnected_player_id=player_id,
                status_before_disconnect=original_status,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def restore_battle_connection(
        self,
        battle_id: str,
        player_id: str,
        *,
        notice: str | None = None,
        turn_deadline_at: datetime | None = None,
        expected_revision: int | None = None,
        restored_at: datetime | None = None,
    ) -> BattleRecord:
        """Restore the exact pre-disconnect phase after the same player returns."""

        battle_id = _identifier(battle_id, "battle_id")
        player_id = _identifier(player_id, "player_id")
        expected_revision = _normalise_expected_revision(expected_revision)
        now = _normalise_datetime(restored_at or utc_now(), "restored_at")
        next_turn_deadline = (
            _normalise_datetime(turn_deadline_at, "turn_deadline_at")
            if turn_deadline_at is not None
            else None
        )
        safe_notice = _optional_text(notice, "notice", maximum=400) or "Player reconnected."
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Battle {battle_id!r} was not found.")
            battle = _battle_from_row(row)
            self._require_battle_revision(battle, expected_revision)
            if battle.status != "disconnected" or battle.disconnected_player_id != player_id:
                raise ConflictError("This player does not have a reconnectable battle.")
            if battle.disconnect_deadline_at is not None and battle.disconnect_deadline_at <= now:
                raise ConflictError("The reconnect grace period has expired.")
            restored_status = battle.status_before_disconnect
            if restored_status is None or restored_status == "disconnected":
                raise ConflictError("The battle does not have a valid pre-disconnect state.")
            if restored_status == "resolving":
                # Heal a row created by an older server version which paused
                # a process-local model job as ``resolving``. No model task is
                # durable, so this must become a retryable active turn.
                self._abort_pending_battle_turn_in_transaction(
                    battle.battle_id,
                    reason="reconnected_after_interrupted_resolution",
                    now=now,
                )
                restored_status = "active"
            if next_turn_deadline is not None:
                if restored_status != "active":
                    raise ValueError("Only an active battle can receive a turn deadline.")
                if next_turn_deadline <= now:
                    raise ValueError("turn_deadline_at must be in the future.")
            updated_battle = replace(
                battle,
                status=restored_status,
                turn_deadline_at=(next_turn_deadline if restored_status == "active" else None),
                disconnect_deadline_at=None,
                disconnected_player_id=None,
                status_before_disconnect=None,
                notice=safe_notice,
                revision=battle.revision + 1,
                updated_at=now,
            )
            self._update_battle_in_transaction(updated_battle)
            updated = self.connection.execute(
                "SELECT * FROM battles WHERE battle_id = ?", (battle_id,)
            ).fetchone()
        assert updated is not None
        return _battle_from_row(updated)

    def expire_battles(
        self,
        *,
        now: datetime | None = None,
        resolving_retry_timeout_seconds: int = DEFAULT_BATTLE_TURN_TIMEOUT_SECONDS,
        resolving_fallback_timeout_seconds: int = DEFAULT_BATTLE_RESOLUTION_LEASE_SECONDS,
    ) -> list[BattleRecord]:
        """Advance overdue challenge, turn, reconnect, and resolution leases.

        This is idempotent: once a battle has a terminal status it is not
        selected again.  An active move clock expires to a normal timeout.  A
        resolving lease is deliberately different: it aborts the unfinished
        model attempt and restores the same active turn with a fresh retry
        clock.  Calling this periodically is therefore enough to recover a
        process that died during Writer/Director work without making a legal,
        already-submitted move lose on account of server latency.
        """

        now = _normalise_datetime(now or utc_now(), "now")
        if (
            isinstance(resolving_retry_timeout_seconds, bool)
            or not isinstance(resolving_retry_timeout_seconds, int)
            or resolving_retry_timeout_seconds <= 0
        ):
            raise ValueError("resolving_retry_timeout_seconds must be a positive integer.")
        if (
            isinstance(resolving_fallback_timeout_seconds, bool)
            or not isinstance(resolving_fallback_timeout_seconds, int)
            or resolving_fallback_timeout_seconds <= 0
        ):
            raise ValueError("resolving_fallback_timeout_seconds must be a positive integer.")
        timestamp = datetime_to_storage(now)
        legacy_resolving_cutoff = datetime_to_storage(
            now - timedelta(seconds=resolving_fallback_timeout_seconds)
        )
        expired: list[BattleRecord] = []
        with self._transaction():
            rows = self.connection.execute(
                """
                SELECT * FROM battles
                WHERE (status IN ('challenge', 'ready') AND ready_deadline_at IS NOT NULL AND ready_deadline_at <= ?)
                   OR (status = 'active' AND turn_deadline_at IS NOT NULL AND turn_deadline_at <= ?)
                   OR (status = 'resolving' AND (
                        (turn_deadline_at IS NOT NULL AND turn_deadline_at <= ?)
                        OR (turn_deadline_at IS NULL AND updated_at <= ?)
                   ))
                   OR (status = 'disconnected' AND disconnect_deadline_at IS NOT NULL AND disconnect_deadline_at <= ?)
                ORDER BY updated_at, battle_id
                """,
                (
                    timestamp,
                    timestamp,
                    timestamp,
                    legacy_resolving_cutoff,
                    timestamp,
                ),
            ).fetchall()
            for row in rows:
                battle = _battle_from_row(row)
                if battle.status in {"challenge", "ready"}:
                    winner = (
                        battle.challenger_player_id
                        if battle.challenger_ready and not battle.opponent_ready
                        else (
                            battle.opponent_player_id
                            if battle.opponent_ready and not battle.challenger_ready
                            else None
                        )
                    )
                    reason = "ready_timeout"
                    notice = "The challenge timed out."
                elif battle.status == "disconnected":
                    winner = _other_battle_player(battle, battle.disconnected_player_id)
                    reason = "disconnect_timeout"
                    notice = "A player did not reconnect in time."
                elif battle.status == "resolving":
                    # The resolution lease is an operational recovery guard,
                    # not a player move clock.  Never make the player forfeit
                    # just because Writer/Director or a prior server process
                    # failed to finish the already accepted move.
                    self._abort_pending_battle_turn_in_transaction(
                        battle.battle_id, reason="resolution_timeout", now=now
                    )
                    updated_battle = replace(
                        battle,
                        status="active",
                        turn_deadline_at=(
                            now + timedelta(seconds=resolving_retry_timeout_seconds)
                        ),
                        notice="The Director took too long. Choose a move.",
                        revision=battle.revision + 1,
                        updated_at=now,
                    )
                    self._update_battle_in_transaction(updated_battle)
                    expired.append(updated_battle)
                    continue
                else:
                    winner = _other_battle_player(battle, battle.current_player_id)
                    reason = "turn_timeout"
                    notice = "A player ran out of time."
                self._abort_pending_battle_turn_in_transaction(
                    battle.battle_id, reason=reason, now=now
                )
                updated_battle = replace(
                    battle,
                    status="timed_out",
                    current_player_id=None,
                    ready_deadline_at=None,
                    turn_deadline_at=None,
                    disconnect_deadline_at=None,
                    disconnected_player_id=None,
                    status_before_disconnect=None,
                    winner_player_id=winner,
                    end_reason=reason,
                    notice=notice,
                    revision=battle.revision + 1,
                    updated_at=now,
                )
                self._update_battle_in_transaction(updated_battle)
                expired.append(updated_battle)
        return expired

    # -- Player-owned Habitat state -------------------------------------

    def get_world_state(
        self, player_id: str, pokemon_id: str
    ) -> PokemonWorldState | None:
        """Return a state only when that Pokemon currently belongs to player."""

        owner = _identifier(player_id, "player_id")
        pokemon_id = _identifier(pokemon_id, "pokemon_id")
        row = self._fetchone(
            """
            SELECT state.*
            FROM pokemon_world_state AS state
            JOIN pokemon AS creature ON creature.pokemon_id = state.pokemon_id
            WHERE state.pokemon_id = ? AND creature.owner_player_id = ?
            """,
            (pokemon_id, owner),
        )
        return _world_state_from_row(row) if row is not None else None

    def upsert_world_state(
        self, player_id: str, state: PokemonWorldState
    ) -> PokemonWorldState:
        """Create or update one current owner's Habitat state.

        The owner check is part of the same transaction as the upsert.  A
        simulator for Player A therefore cannot overwrite a Pokemon that was
        traded to Player B between ticks.
        """

        owner = _identifier(player_id, "player_id")
        state = _validated_world_state(state)
        with self._transaction():
            self._require_player_in_transaction(owner)
            row = self.connection.execute(
                "SELECT owner_player_id FROM pokemon WHERE pokemon_id = ?",
                (state.pokemon_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Pokemon {state.pokemon_id!r} was not found.")
            if row["owner_player_id"] != owner:
                raise OwnershipError("Pokemon does not belong to the requested player.")
            self.connection.execute(
                """
                INSERT INTO pokemon_world_state(
                    pokemon_id, x, y, mood, energy, activity, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pokemon_id) DO UPDATE SET
                    x = excluded.x,
                    y = excluded.y,
                    mood = excluded.mood,
                    energy = excluded.energy,
                    activity = excluded.activity,
                    updated_at = excluded.updated_at
                """,
                _world_state_params(state),
            )
        return state

    def list_world_states_for_player(self, player_id: str) -> list[PokemonWorldState]:
        """List persisted Habitat states for the player's current collection."""

        owner = _identifier(player_id, "player_id")
        return [
            _world_state_from_row(row)
            for row in self._fetchall(
                """
                SELECT state.*
                FROM pokemon_world_state AS state
                JOIN pokemon AS creature ON creature.pokemon_id = state.pokemon_id
                WHERE creature.owner_player_id = ?
                ORDER BY creature.caught_at, state.pokemon_id
                """,
                (owner,),
            )
        ]

    def append_simulation_event(self, event: SimulationEventRecord) -> SimulationEventRecord:
        """Append an event after verifying all referenced Pokemon are owned.

        Events remain attached to the player whose world generated them even if
        one of their Pokemon is traded later.  This preserves a readable
        Habitat history while current state always follows ownership.
        """

        event = _validated_simulation_event(event)
        try:
            with self._transaction():
                self._require_player_in_transaction(event.player_id)
                self._require_owned_pokemon_in_transaction(
                    event.actor_pokemon_id, event.player_id
                )
                if event.target_pokemon_id is not None:
                    self._require_owned_pokemon_in_transaction(
                        event.target_pokemon_id, event.player_id
                    )
                for line in event.dialogue:
                    self._require_owned_pokemon_in_transaction(
                        line.speaker_pokemon_id, event.player_id
                    )
                self.connection.execute(
                    """
                    INSERT INTO simulation_events(
                        event_id, player_id, revision, actor_pokemon_id,
                        target_pokemon_id, kind, summary, dialogue_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _simulation_event_params(event),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Simulation event {event.event_id!r} already exists.") from exc
        return event

    def commit_simulation_tick(
        self,
        player_id: str,
        states: Sequence[PokemonWorldState],
        event: SimulationEventRecord,
    ) -> SimulationEventRecord:
        """Atomically persist one player Habitat beat.

        A tick's positions and activity record must either both become visible
        or neither must.  In particular, this prevents an NFC/HTTP trade from
        landing between individual state upserts and the event append.  The
        ownership checks occur in this same SQLite transaction, so a simulator
        cannot commit a result for a Pokemon that has just moved to another
        player's collection.
        """

        owner = _identifier(player_id, "player_id")
        if isinstance(states, (str, bytes)) or not isinstance(states, Sequence):
            raise ValueError("states must be a sequence of PokemonWorldState values.")
        checked_states = tuple(_validated_world_state(state) for state in states)
        if len({state.pokemon_id for state in checked_states}) != len(checked_states):
            raise ValueError("A simulation tick cannot contain duplicate Pokemon states.")
        event = _validated_simulation_event(event)
        if event.player_id != owner:
            raise OwnershipError("Simulation event player does not match the requested player.")
        try:
            with self._transaction():
                self._require_player_in_transaction(owner)
                for state in checked_states:
                    self._require_owned_pokemon_in_transaction(state.pokemon_id, owner)
                self._require_owned_pokemon_in_transaction(event.actor_pokemon_id, owner)
                if event.target_pokemon_id is not None:
                    self._require_owned_pokemon_in_transaction(
                        event.target_pokemon_id, owner
                    )
                for line in event.dialogue:
                    self._require_owned_pokemon_in_transaction(
                        line.speaker_pokemon_id, owner
                    )
                for state in checked_states:
                    self.connection.execute(
                        """
                        INSERT INTO pokemon_world_state(
                            pokemon_id, x, y, mood, energy, activity, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(pokemon_id) DO UPDATE SET
                            x = excluded.x,
                            y = excluded.y,
                            mood = excluded.mood,
                            energy = excluded.energy,
                            activity = excluded.activity,
                            updated_at = excluded.updated_at
                        """,
                        _world_state_params(state),
                    )
                self.connection.execute(
                    """
                    INSERT INTO simulation_events(
                        event_id, player_id, revision, actor_pokemon_id,
                        target_pokemon_id, kind, summary, dialogue_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _simulation_event_params(event),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Simulation event {event.event_id!r} already exists.") from exc
        return event

    def list_simulation_events_for_player(
        self, player_id: str, *, limit: int = 50
    ) -> list[SimulationEventRecord]:
        """Return the newest ``limit`` events in chronological order.

        Fetching newest-first keeps the query fast; reversing the small result
        gives the renderer an intuitive oldest-to-newest timeline.
        """

        owner = _identifier(player_id, "player_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500.")
        rows = self._fetchall(
            """
            SELECT * FROM simulation_events
            WHERE player_id = ?
            ORDER BY revision DESC, created_at DESC
            LIMIT ?
            """,
            (owner, limit),
        )
        return list(reversed([_simulation_event_from_row(row) for row in rows]))

    # -- Per-badge UI sessions ------------------------------------------

    def get_session(self, badge_id: str) -> BadgeSession | None:
        row = self._fetchone(
            "SELECT * FROM badge_sessions WHERE badge_id = ?", (_identifier(badge_id, "badge_id"),)
        )
        return _session_from_row(row) if row is not None else None

    def get_or_create_session(
        self,
        badge_id: str,
        *,
        active_app: str = "home",
        app_state: Mapping[str, Any] | None = None,
        canvas_active: bool = False,
    ) -> BadgeSession:
        """Return a badge's durable app state, creating its Home state once."""

        badge_id = _identifier(badge_id, "badge_id")
        active_app = _text(active_app, "active_app", maximum=64)
        state = json_object(app_state, field_name="app_state")
        now = utc_now()
        with self._transaction():
            self._require_badge_in_transaction(badge_id)
            row = self.connection.execute("SELECT * FROM badge_sessions WHERE badge_id = ?", (badge_id,)).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO badge_sessions(
                        badge_id, active_app, app_state_json, canvas_active,
                        last_render_hash, revision, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?)
                    """,
                    (
                        badge_id,
                        active_app,
                        json_dumps(state),
                        int(bool(canvas_active)),
                        datetime_to_storage(now),
                    ),
                )
                row = self.connection.execute("SELECT * FROM badge_sessions WHERE badge_id = ?", (badge_id,)).fetchone()
        assert row is not None
        return _session_from_row(row)

    def save_session(
        self,
        badge_id: str,
        *,
        active_app: str,
        app_state: Mapping[str, Any],
        canvas_active: bool,
        last_render_hash: str | None = None,
        expected_revision: int | None = None,
    ) -> BadgeSession:
        """Commit new UI state and increment its revision.

        New sessions must be initialized with :meth:`get_or_create_session`.
        An ``expected_revision`` check gives the gateway a simple way to reject
        competing button events for the same badge.
        """

        badge_id = _identifier(badge_id, "badge_id")
        active_app = _text(active_app, "active_app", maximum=64)
        state = json_object(app_state, field_name="app_state")
        render_hash = _optional_text(last_render_hash, "last_render_hash", maximum=256)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer.")
        now = utc_now()
        with self._transaction():
            row = self.connection.execute("SELECT revision FROM badge_sessions WHERE badge_id = ?", (badge_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Badge session for {badge_id!r} was not found.")
            revision = int(row["revision"])
            if expected_revision is not None and revision != expected_revision:
                raise ConflictError(
                    f"Badge session revision is {revision}, not expected {expected_revision}."
                )
            self.connection.execute(
                """
                UPDATE badge_sessions
                SET active_app = ?, app_state_json = ?, canvas_active = ?,
                    last_render_hash = ?, revision = ?, updated_at = ?
                WHERE badge_id = ?
                """,
                (
                    active_app,
                    json_dumps(state),
                    int(bool(canvas_active)),
                    render_hash,
                    revision + 1,
                    datetime_to_storage(now),
                    badge_id,
                ),
            )
            updated = self.connection.execute("SELECT * FROM badge_sessions WHERE badge_id = ?", (badge_id,)).fetchone()
        assert updated is not None
        return _session_from_row(updated)

    # -- Battle transaction helpers -------------------------------------

    def _require_battle_participant(self, battle: BattleRecord, player_id: str) -> None:
        if player_id not in {battle.challenger_player_id, battle.opponent_player_id}:
            raise OwnershipError("Player is not a participant in this battle.")

    def _require_battle_revision(
        self, battle: BattleRecord, expected_revision: int | None
    ) -> None:
        if expected_revision is not None and battle.revision != expected_revision:
            raise ConflictError(
                f"Battle revision is {battle.revision}, not expected {expected_revision}."
            )

    def _require_badge_owned_by_player_in_transaction(
        self, badge_id: str | None, player_id: str, field_name: str
    ) -> None:
        if badge_id is None:
            return
        row = self.connection.execute(
            "SELECT player_id FROM badges WHERE badge_id = ?", (badge_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Badge {badge_id!r} was not found.")
        if row["player_id"] != player_id:
            raise OwnershipError(f"{field_name} does not belong to its battle player.")

    def _require_no_open_battle_in_transaction(self, player_id: str) -> None:
        placeholders = ", ".join("?" for _ in _BATTLE_OPEN_STATUSES)
        row = self.connection.execute(
            f"""
            SELECT battle_id FROM battles
            WHERE (challenger_player_id = ? OR opponent_player_id = ?)
              AND status IN ({placeholders})
            LIMIT 1
            """,
            (player_id, player_id, *sorted(_BATTLE_OPEN_STATUSES)),
        ).fetchone()
        if row is not None:
            raise ConflictError(f"Player already has an open battle: {row['battle_id']!r}.")

    def _require_battle_roster_owned_in_transaction(
        self, player_id: str, roster: BattleRosterSnapshot
    ) -> None:
        """Validate source ownership and reject stale/forged challenge snapshots."""

        for snapshot in roster.pokemon:
            row = self.connection.execute(
                "SELECT * FROM pokemon WHERE pokemon_id = ?", (snapshot.pokemon_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Pokemon {snapshot.pokemon_id!r} was not found.")
            record = _pokemon_from_row(row)
            if record.owner_player_id != player_id:
                raise OwnershipError(
                    f"Pokemon {snapshot.pokemon_id!r} does not belong to player {player_id!r}."
                )
            current = battle_pokemon_snapshot_from_pokemon(record)
            if not _same_battle_pokemon_profile(snapshot, current):
                raise ConflictError(
                    f"Pokemon {snapshot.pokemon_id!r} changed before the challenge was created."
                )
            if snapshot.current_hp != snapshot.max_hp or any(snapshot.stat_stages.values()):
                raise ValueError("A new battle roster must start at full HP with neutral stat stages.")

    def _abort_pending_battle_turn_in_transaction(
        self, battle_id: str, *, reason: str, now: datetime
    ) -> None:
        """Mark a pending Writer/Jev attempt terminal before ending a battle."""

        self.connection.execute(
            """
            UPDATE battle_turns
            SET status = 'aborted', abort_reason = ?, resolved_at = ?
            WHERE battle_id = ? AND status = 'resolving'
            """,
            (reason, datetime_to_storage(now), battle_id),
        )

    def _update_battle_in_transaction(self, battle: BattleRecord) -> None:
        battle = _validated_battle_record(battle)
        cursor = self.connection.execute(
            """
            UPDATE battles SET
                challenger_player_id = ?, challenger_display_name = ?,
                challenger_badge_id = ?, challenger_roster_json = ?, challenger_ready = ?,
                opponent_player_id = ?, opponent_display_name = ?,
                opponent_badge_id = ?, opponent_roster_json = ?, opponent_ready = ?,
                status = ?, current_player_id = ?, turn_number = ?, revision = ?,
                ready_deadline_at = ?, turn_deadline_at = ?, disconnect_deadline_at = ?,
                disconnected_player_id = ?, status_before_disconnect = ?, winner_player_id = ?,
                cancelled_by_player_id = ?, end_reason = ?, notice = ?,
                last_visible_rationale = ?, created_at = ?, updated_at = ?
            WHERE battle_id = ?
            """,
            _battle_update_params(battle),
        )
        if cursor.rowcount != 1:
            raise NotFoundError(f"Battle {battle.battle_id!r} was not found.")

    def _require_player_in_transaction(self, player_id: str) -> None:
        row = self.connection.execute("SELECT 1 FROM players WHERE player_id = ?", (player_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Player {player_id!r} was not found.")

    def _require_badge_in_transaction(self, badge_id: str) -> None:
        row = self.connection.execute("SELECT 1 FROM badges WHERE badge_id = ?", (badge_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Badge {badge_id!r} was not found.")

    def _require_owned_pokemon_in_transaction(self, pokemon_id: str, player_id: str) -> None:
        row = self.connection.execute(
            "SELECT owner_player_id FROM pokemon WHERE pokemon_id = ?", (pokemon_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Pokemon {pokemon_id!r} was not found.")
        if row["owner_player_id"] != player_id:
            raise OwnershipError(f"Pokemon {pokemon_id!r} does not belong to player {player_id!r}.")


def _player_from_row(row: sqlite3.Row) -> Player:
    return Player(
        player_id=row["player_id"],
        display_name=row["display_name"],
        created_at=datetime_from_storage(row["created_at"]),
        updated_at=datetime_from_storage(row["updated_at"]),
    )


def _badge_from_row(row: sqlite3.Row) -> Badge:
    return Badge(
        badge_id=row["badge_id"],
        htn_id=row["htn_id"],
        player_id=row["player_id"],
        app_key_ciphertext=row["app_key_ciphertext"],
        last_seen_at=(
            datetime_from_storage(row["last_seen_at"])
            if row["last_seen_at"] is not None
            else None
        ),
        online=bool(row["online"]),
        created_at=datetime_from_storage(row["created_at"]),
        updated_at=datetime_from_storage(row["updated_at"]),
    )


def _pokemon_from_row(row: sqlite3.Row) -> PokemonRecord:
    raw_types = json_loads(row["types_json"])
    raw_stats = json_loads(row["stats_json"])
    raw_moves = json_loads(row["moves_json"])
    raw_battle_natures = json_loads(row["battle_natures_json"])
    raw_metadata = json_loads(row["metadata_json"])
    if (
        not isinstance(raw_types, list)
        or not isinstance(raw_stats, dict)
        or not isinstance(raw_moves, list)
        or not isinstance(raw_battle_natures, list)
    ):
        raise ValueError("Stored Pokemon JSON has the wrong shape.")
    if not isinstance(raw_metadata, dict):
        raise ValueError("Stored Pokemon metadata is not an object.")
    return _validated_pokemon(
        PokemonRecord(
            pokemon_id=row["pokemon_id"],
            owner_player_id=row["owner_player_id"],
            name=row["name"],
            species=row["species"],
            types=tuple(raw_types),
            stats=raw_stats,
            moves=tuple(raw_moves),
            battle_natures=tuple(raw_battle_natures),
            flavour=row["flavour"],
            sprite_prompt=row["sprite_prompt"],
            rarity=row["rarity"],
            caught_at=datetime_from_storage(row["caught_at"]),
            sprite_path=row["sprite_path"],
            captured_by_badge_id=row["captured_by_badge_id"],
            metadata=raw_metadata,
            created_at=datetime_from_storage(row["created_at"]),
            updated_at=datetime_from_storage(row["updated_at"]),
        )
    )


def _world_state_from_row(row: sqlite3.Row) -> PokemonWorldState:
    return _validated_world_state(
        PokemonWorldState(
            pokemon_id=row["pokemon_id"],
            x=int(row["x"]),
            y=int(row["y"]),
            mood=row["mood"],
            energy=int(row["energy"]),
            activity=row["activity"],
            updated_at=datetime_from_storage(row["updated_at"]),
        )
    )


def _simulation_event_from_row(row: sqlite3.Row) -> SimulationEventRecord:
    raw_dialogue = json_loads(row["dialogue_json"])
    if not isinstance(raw_dialogue, list):
        raise ValueError("Stored simulation dialogue is not a list.")
    dialogue: list[SimulationDialogueLine] = []
    for item in raw_dialogue:
        if not isinstance(item, dict):
            raise ValueError("Stored simulation dialogue line is not an object.")
        speaker = item.get("speaker_pokemon_id")
        text = item.get("text")
        if not isinstance(speaker, str) or not isinstance(text, str):
            raise ValueError("Stored simulation dialogue line is malformed.")
        dialogue.append(SimulationDialogueLine(speaker, text))
    return _validated_simulation_event(
        SimulationEventRecord(
            event_id=row["event_id"],
            player_id=row["player_id"],
            revision=int(row["revision"]),
            actor_pokemon_id=row["actor_pokemon_id"],
            target_pokemon_id=row["target_pokemon_id"],
            kind=row["kind"],
            summary=row["summary"],
            dialogue=tuple(dialogue),
            created_at=datetime_from_storage(row["created_at"]),
        )
    )


def _session_from_row(row: sqlite3.Row) -> BadgeSession:
    state = json_loads(row["app_state_json"])
    if not isinstance(state, dict):
        raise ValueError("Stored badge app state is not an object.")
    return BadgeSession(
        badge_id=row["badge_id"],
        active_app=row["active_app"],
        app_state=state,
        canvas_active=bool(row["canvas_active"]),
        last_render_hash=row["last_render_hash"],
        revision=int(row["revision"]),
        updated_at=datetime_from_storage(row["updated_at"]),
    )


def _transfer_from_row(row: sqlite3.Row) -> OwnershipTransfer:
    return OwnershipTransfer(
        transfer_id=row["transfer_id"],
        pokemon_id=row["pokemon_id"],
        from_player_id=row["from_player_id"],
        to_player_id=row["to_player_id"],
        reason=row["reason"],
        transferred_at=datetime_from_storage(row["transferred_at"]),
    )


def _battle_pokemon_snapshot_from_dict(value: JSONValue) -> BattlePokemonSnapshot:
    if not isinstance(value, dict):
        raise ValueError("Stored battle Pokemon snapshot is not an object.")
    required_lists = ("types", "moves", "battle_natures")
    if any(not isinstance(value.get(field_name), list) for field_name in required_lists):
        raise ValueError("Stored battle Pokemon snapshot has malformed list fields.")
    if not isinstance(value.get("stats"), dict) or not isinstance(value.get("stat_stages"), dict):
        raise ValueError("Stored battle Pokemon snapshot has malformed stat fields.")
    return _validated_battle_pokemon_snapshot(
        BattlePokemonSnapshot(
            pokemon_id=value.get("pokemon_id"),  # type: ignore[arg-type]
            name=value.get("name"),  # type: ignore[arg-type]
            species=value.get("species"),  # type: ignore[arg-type]
            types=tuple(value["types"]),  # type: ignore[arg-type]
            stats=value["stats"],  # type: ignore[arg-type]
            moves=tuple(value["moves"]),  # type: ignore[arg-type]
            battle_natures=tuple(value["battle_natures"]),  # type: ignore[arg-type]
            flavour=value.get("flavour"),  # type: ignore[arg-type]
            rarity=value.get("rarity"),  # type: ignore[arg-type]
            max_hp=value.get("max_hp"),  # type: ignore[arg-type]
            current_hp=value.get("current_hp"),  # type: ignore[arg-type]
            sprite_path=value.get("sprite_path"),  # type: ignore[arg-type]
            stat_stages=value["stat_stages"],  # type: ignore[arg-type]
        )
    )


def _battle_roster_snapshot_from_dict(value: JSONValue) -> BattleRosterSnapshot:
    if not isinstance(value, dict):
        raise ValueError("Stored battle roster is not an object.")
    raw_pokemon = value.get("pokemon")
    if not isinstance(raw_pokemon, list):
        raise ValueError("Stored battle roster Pokemon list is malformed.")
    return _validated_battle_roster_snapshot(
        BattleRosterSnapshot(
            pokemon=tuple(_battle_pokemon_snapshot_from_dict(item) for item in raw_pokemon),
            active_index=value.get("active_index", 0),  # type: ignore[arg-type]
        )
    )


def _battle_candidate_outcome_from_dict(value: JSONValue) -> BattleCandidateOutcome:
    if not isinstance(value, dict):
        raise ValueError("Stored battle candidate is not an object.")
    return _validated_battle_candidate_outcome(
        BattleCandidateOutcome(
            candidate_id=value.get("candidate_id"),  # type: ignore[arg-type]
            summary=value.get("summary"),  # type: ignore[arg-type]
            rationale=value.get("rationale"),  # type: ignore[arg-type]
            actor_hp_delta=value.get("actor_hp_delta", 0),  # type: ignore[arg-type]
            target_hp_delta=value.get("target_hp_delta", 0),  # type: ignore[arg-type]
            actor_stat=value.get("actor_stat"),  # type: ignore[arg-type]
            actor_stat_delta=value.get("actor_stat_delta", 0),  # type: ignore[arg-type]
            target_stat=value.get("target_stat"),  # type: ignore[arg-type]
            target_stat_delta=value.get("target_stat_delta", 0),  # type: ignore[arg-type]
        )
    )


def _battle_turn_from_row(row: sqlite3.Row) -> BattleTurnRecord:
    candidates = json_loads(row["candidate_outcomes_json"])
    if not isinstance(candidates, list):
        raise ValueError("Stored battle turn candidates are not a list.")
    state_after: Mapping[str, JSONValue] | None = None
    if row["state_after_json"] is not None:
        raw_state_after = json_loads(row["state_after_json"])
        if not isinstance(raw_state_after, dict):
            raise ValueError("Stored battle turn state is not an object.")
        state_after = raw_state_after
    return _validated_battle_turn(
        BattleTurnRecord(
            turn_id=row["turn_id"],
            battle_id=row["battle_id"],
            turn_number=int(row["turn_number"]),
            attempt=int(row["attempt"]),
            acting_player_id=row["acting_player_id"],
            actor_pokemon_id=row["actor_pokemon_id"],
            move_id=row["move_id"],
            candidate_outcomes=tuple(
                _battle_candidate_outcome_from_dict(candidate) for candidate in candidates
            ),
            status=row["status"],  # type: ignore[arg-type]
            selected_candidate_id=row["selected_candidate_id"],
            visible_rationale=row["visible_rationale"],
            state_after=state_after,
            created_at=datetime_from_storage(row["created_at"]),
            resolved_at=(
                datetime_from_storage(row["resolved_at"])
                if row["resolved_at"] is not None
                else None
            ),
            abort_reason=row["abort_reason"],
        )
    )


def _battle_from_row(row: sqlite3.Row) -> BattleRecord:
    challenger_roster = _battle_roster_snapshot_from_dict(
        json_loads(row["challenger_roster_json"])
    )
    opponent_roster = _battle_roster_snapshot_from_dict(json_loads(row["opponent_roster_json"]))
    return _validated_battle_record(
        BattleRecord(
            battle_id=row["battle_id"],
            challenger_player_id=row["challenger_player_id"],
            challenger_display_name=row["challenger_display_name"],
            challenger_roster=challenger_roster,
            challenger_ready=bool(row["challenger_ready"]),
            opponent_player_id=row["opponent_player_id"],
            opponent_display_name=row["opponent_display_name"],
            opponent_roster=opponent_roster,
            opponent_ready=bool(row["opponent_ready"]),
            status=row["status"],  # type: ignore[arg-type]
            current_player_id=row["current_player_id"],
            turn_number=int(row["turn_number"]),
            revision=int(row["revision"]),
            created_at=datetime_from_storage(row["created_at"]),
            updated_at=datetime_from_storage(row["updated_at"]),
            challenger_badge_id=row["challenger_badge_id"],
            opponent_badge_id=row["opponent_badge_id"],
            ready_deadline_at=(
                datetime_from_storage(row["ready_deadline_at"])
                if row["ready_deadline_at"] is not None
                else None
            ),
            turn_deadline_at=(
                datetime_from_storage(row["turn_deadline_at"])
                if row["turn_deadline_at"] is not None
                else None
            ),
            disconnect_deadline_at=(
                datetime_from_storage(row["disconnect_deadline_at"])
                if row["disconnect_deadline_at"] is not None
                else None
            ),
            disconnected_player_id=row["disconnected_player_id"],
            status_before_disconnect=row["status_before_disconnect"],  # type: ignore[arg-type]
            winner_player_id=row["winner_player_id"],
            cancelled_by_player_id=row["cancelled_by_player_id"],
            end_reason=row["end_reason"],
            notice=row["notice"],
            last_visible_rationale=row["last_visible_rationale"],
        )
    )


def _validated_pokemon(pokemon: PokemonRecord) -> PokemonRecord:
    if not isinstance(pokemon, PokemonRecord):
        raise ValueError("pokemon must be a PokemonRecord.")
    return PokemonRecord(
        pokemon_id=_identifier(pokemon.pokemon_id, "pokemon_id"),
        owner_player_id=_identifier(pokemon.owner_player_id, "owner_player_id"),
        name=_text(pokemon.name, "name", maximum=48),
        species=_text(pokemon.species, "species", maximum=96),
        types=_normalise_types(pokemon.types),
        stats=_normalise_stats(pokemon.stats),
        moves=_normalise_moves(pokemon.moves),
        battle_natures=_normalise_battle_natures(pokemon.battle_natures),
        flavour=_text(pokemon.flavour, "flavour", maximum=240),
        sprite_prompt=_text(pokemon.sprite_prompt, "sprite_prompt", maximum=500),
        rarity=_text(pokemon.rarity, "rarity", maximum=32).lower(),
        caught_at=_normalise_datetime(pokemon.caught_at, "caught_at"),
        sprite_path=_optional_text(pokemon.sprite_path, "sprite_path", maximum=512),
        captured_by_badge_id=_optional_identifier(
            pokemon.captured_by_badge_id, "captured_by_badge_id"
        ),
        metadata=json_object(pokemon.metadata, field_name="metadata"),
        created_at=_normalise_datetime(pokemon.created_at, "created_at"),
        updated_at=_normalise_datetime(pokemon.updated_at, "updated_at"),
    )


def _normalise_battle_status(value: Any, field_name: str = "battle status") -> BattleStatus:
    if not isinstance(value, str) or value not in {
        "challenge",
        "ready",
        "active",
        "resolving",
        "finished",
        "cancelled",
        "disconnected",
        "timed_out",
    }:
        raise ValueError(f"{field_name} is invalid.")
    return value  # type: ignore[return-value]


def _normalise_battle_turn_status(value: Any) -> BattleTurnStatus:
    if not isinstance(value, str) or value not in {"resolving", "resolved", "aborted"}:
        raise ValueError("battle turn status is invalid.")
    return value  # type: ignore[return-value]


def _normalise_battle_move(value: Any) -> str:
    return _text(value, "move_id", maximum=64).lower().replace(" ", "_")


def _normalise_expected_revision(value: int | None) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, "expected_revision", 0, 2_147_483_647)


def _normalise_battle_stat_stages(values: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ValueError("stat_stages must be a JSON object.")
    if set(values) != set(_BATTLE_STAT_STAGE_NAMES):
        raise ValueError("stat_stages must contain attack, defense, and speed.")
    return {
        name: _bounded_int(values[name], f"stat_stages.{name}", -6, 6)
        for name in _BATTLE_STAT_STAGE_NAMES
    }


def _normalise_battle_stat_name(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    name = _text(value, field_name, maximum=16).lower().replace(" ", "_")
    if name not in _BATTLE_STAT_STAGE_NAMES:
        raise ValueError(f"{field_name} must name attack, defense, or speed.")
    return name


def _validated_battle_pokemon_snapshot(
    snapshot: BattlePokemonSnapshot,
) -> BattlePokemonSnapshot:
    if not isinstance(snapshot, BattlePokemonSnapshot):
        raise ValueError("snapshot must be a BattlePokemonSnapshot.")
    moves = _normalise_moves(snapshot.moves)
    battle_natures = _normalise_battle_natures(snapshot.battle_natures)
    if len(moves) != 4:
        raise ValueError("A battle Pokemon needs exactly four moves.")
    if len(set(moves)) != len(moves):
        raise ValueError("Battle Pokemon moves cannot be duplicated.")
    if not 2 <= len(battle_natures) <= 4:
        raise ValueError("A battle Pokemon needs two to four battle-nature tags.")
    stats = _normalise_stats(snapshot.stats)
    max_hp = _bounded_int(snapshot.max_hp, "max_hp", 1, 999)
    if max_hp != stats["hp"]:
        raise ValueError("max_hp must match the snapshot's HP stat.")
    current_hp = _bounded_int(snapshot.current_hp, "current_hp", 0, max_hp)
    return BattlePokemonSnapshot(
        pokemon_id=_identifier(snapshot.pokemon_id, "pokemon_id"),
        name=_text(snapshot.name, "name", maximum=48),
        species=_text(snapshot.species, "species", maximum=96),
        types=_normalise_types(snapshot.types),
        stats=stats,
        moves=moves,
        battle_natures=battle_natures,
        flavour=_text(snapshot.flavour, "flavour", maximum=240),
        rarity=_text(snapshot.rarity, "rarity", maximum=32).lower(),
        max_hp=max_hp,
        current_hp=current_hp,
        sprite_path=_optional_text(snapshot.sprite_path, "sprite_path", maximum=512),
        stat_stages=_normalise_battle_stat_stages(snapshot.stat_stages),
    )


def _validated_battle_roster_snapshot(
    roster: BattleRosterSnapshot,
) -> BattleRosterSnapshot:
    if not isinstance(roster, BattleRosterSnapshot):
        raise ValueError("roster must be a BattleRosterSnapshot.")
    if isinstance(roster.pokemon, (str, bytes)) or not isinstance(roster.pokemon, Sequence):
        raise ValueError("Battle roster Pokemon must be a sequence.")
    pokemon = tuple(_validated_battle_pokemon_snapshot(snapshot) for snapshot in roster.pokemon)
    if not 1 <= len(pokemon) <= 6:
        raise ValueError("A battle roster needs one to six Pokemon.")
    if len({snapshot.pokemon_id for snapshot in pokemon}) != len(pokemon):
        raise ValueError("A battle roster cannot contain the same Pokemon twice.")
    active_index = _bounded_int(roster.active_index, "active_index", 0, len(pokemon) - 1)
    if pokemon[active_index].fainted and not all(snapshot.fainted for snapshot in pokemon):
        raise ValueError("The active battle Pokemon cannot be fainted while another can fight.")
    return BattleRosterSnapshot(pokemon=pokemon, active_index=active_index)


def _validated_battle_candidate_outcome(
    candidate: BattleCandidateOutcome,
) -> BattleCandidateOutcome:
    if not isinstance(candidate, BattleCandidateOutcome):
        raise ValueError("candidate must be a BattleCandidateOutcome.")
    actor_stat = _normalise_battle_stat_name(candidate.actor_stat, "actor_stat")
    target_stat = _normalise_battle_stat_name(candidate.target_stat, "target_stat")
    actor_stat_delta = _bounded_int(candidate.actor_stat_delta, "actor_stat_delta", -6, 6)
    target_stat_delta = _bounded_int(candidate.target_stat_delta, "target_stat_delta", -6, 6)
    if actor_stat is None and actor_stat_delta != 0:
        raise ValueError("actor_stat_delta needs an actor_stat.")
    if target_stat is None and target_stat_delta != 0:
        raise ValueError("target_stat_delta needs a target_stat.")
    actor_hp_delta = _bounded_int(candidate.actor_hp_delta, "actor_hp_delta", -999, 999)
    target_hp_delta = _bounded_int(candidate.target_hp_delta, "target_hp_delta", -999, 999)
    if not any((actor_hp_delta, target_hp_delta, actor_stat_delta, target_stat_delta)):
        raise ValueError("A battle candidate must change HP or a stat stage.")
    return BattleCandidateOutcome(
        candidate_id=_identifier(candidate.candidate_id, "candidate_id"),
        summary=_text(candidate.summary, "candidate summary", maximum=280),
        rationale=_text(candidate.rationale, "candidate rationale", maximum=600),
        actor_hp_delta=actor_hp_delta,
        target_hp_delta=target_hp_delta,
        actor_stat=actor_stat,
        actor_stat_delta=actor_stat_delta,
        target_stat=target_stat,
        target_stat_delta=target_stat_delta,
    )


def _normalise_battle_candidate_outcomes(
    values: Sequence[BattleCandidateOutcome],
    *,
    allow_empty: bool = False,
) -> tuple[BattleCandidateOutcome, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("candidate_outcomes must be a sequence of BattleCandidateOutcome values.")
    minimum = 0 if allow_empty else 1
    if not minimum <= len(values) <= 5:
        raise ValueError("A battle turn needs one to five candidate outcomes.")
    candidates = tuple(_validated_battle_candidate_outcome(candidate) for candidate in values)
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("Battle candidate IDs cannot be duplicated.")
    return candidates


def _validated_battle_turn(turn: BattleTurnRecord) -> BattleTurnRecord:
    if not isinstance(turn, BattleTurnRecord):
        raise ValueError("turn must be a BattleTurnRecord.")
    status = _normalise_battle_turn_status(turn.status)
    candidates = _normalise_battle_candidate_outcomes(
        turn.candidate_outcomes, allow_empty=True
    )
    selected_candidate_id = _optional_identifier(
        turn.selected_candidate_id, "selected_candidate_id"
    )
    visible_rationale = _optional_text(
        turn.visible_rationale, "visible_rationale", maximum=600
    )
    state_after = (
        json_object(turn.state_after, field_name="state_after")
        if turn.state_after is not None
        else None
    )
    resolved_at = (
        _normalise_datetime(turn.resolved_at, "resolved_at")
        if turn.resolved_at is not None
        else None
    )
    abort_reason = _optional_text(turn.abort_reason, "abort_reason", maximum=400)
    if status == "resolving":
        if any(value is not None for value in (selected_candidate_id, visible_rationale, state_after, resolved_at, abort_reason)):
            raise ValueError("A resolving battle turn cannot have a result yet.")
    elif status == "resolved":
        if not candidates:
            raise ValueError("A resolved battle turn needs saved candidate outcomes.")
        if selected_candidate_id is None or selected_candidate_id not in {
            candidate.candidate_id for candidate in candidates
        }:
            raise ValueError("A resolved battle turn must select one saved candidate.")
        if visible_rationale is None or state_after is None or resolved_at is None:
            raise ValueError("A resolved battle turn needs rationale, state, and timestamp.")
        if abort_reason is not None:
            raise ValueError("A resolved battle turn cannot have an abort reason.")
    else:
        if abort_reason is None or resolved_at is None:
            raise ValueError("An aborted battle turn needs a reason and timestamp.")
        if selected_candidate_id is not None or state_after is not None:
            raise ValueError("An aborted battle turn cannot have a selected result.")
    return BattleTurnRecord(
        turn_id=_identifier(turn.turn_id, "turn_id"),
        battle_id=_identifier(turn.battle_id, "battle_id"),
        turn_number=_bounded_int(turn.turn_number, "turn_number", 1, 2_147_483_647),
        attempt=_bounded_int(turn.attempt, "attempt", 1, 2_147_483_647),
        acting_player_id=_identifier(turn.acting_player_id, "acting_player_id"),
        actor_pokemon_id=_identifier(turn.actor_pokemon_id, "actor_pokemon_id"),
        move_id=_normalise_battle_move(turn.move_id),
        candidate_outcomes=candidates,
        status=status,
        created_at=_normalise_datetime(turn.created_at, "created_at"),
        selected_candidate_id=selected_candidate_id,
        visible_rationale=visible_rationale,
        state_after=state_after,
        resolved_at=resolved_at,
        abort_reason=abort_reason,
    )


def _validated_battle_record(battle: BattleRecord) -> BattleRecord:
    if not isinstance(battle, BattleRecord):
        raise ValueError("battle must be a BattleRecord.")
    challenger_player_id = _identifier(battle.challenger_player_id, "challenger_player_id")
    opponent_player_id = _identifier(battle.opponent_player_id, "opponent_player_id")
    if challenger_player_id == opponent_player_id:
        raise ValueError("A battle needs two distinct players.")
    challenger_roster = _validated_battle_roster_snapshot(battle.challenger_roster)
    opponent_roster = _validated_battle_roster_snapshot(battle.opponent_roster)
    _ensure_distinct_battle_rosters(challenger_roster, opponent_roster)
    if not isinstance(battle.challenger_ready, bool) or not isinstance(battle.opponent_ready, bool):
        raise ValueError("Battle ready flags must be booleans.")
    status = _normalise_battle_status(battle.status)
    current_player_id = _optional_identifier(battle.current_player_id, "current_player_id")
    winner_player_id = _optional_identifier(battle.winner_player_id, "winner_player_id")
    cancelled_by_player_id = _optional_identifier(
        battle.cancelled_by_player_id, "cancelled_by_player_id"
    )
    disconnected_player_id = _optional_identifier(
        battle.disconnected_player_id, "disconnected_player_id"
    )
    status_before_disconnect = (
        _normalise_battle_status(battle.status_before_disconnect, "status_before_disconnect")
        if battle.status_before_disconnect is not None
        else None
    )
    participants = {challenger_player_id, opponent_player_id}
    for value, field_name in (
        (current_player_id, "current_player_id"),
        (winner_player_id, "winner_player_id"),
        (cancelled_by_player_id, "cancelled_by_player_id"),
        (disconnected_player_id, "disconnected_player_id"),
    ):
        if value is not None and value not in participants:
            raise ValueError(f"{field_name} must be a battle participant.")
    ready_deadline = (
        _normalise_datetime(battle.ready_deadline_at, "ready_deadline_at")
        if battle.ready_deadline_at is not None
        else None
    )
    turn_deadline = (
        _normalise_datetime(battle.turn_deadline_at, "turn_deadline_at")
        if battle.turn_deadline_at is not None
        else None
    )
    disconnect_deadline = (
        _normalise_datetime(battle.disconnect_deadline_at, "disconnect_deadline_at")
        if battle.disconnect_deadline_at is not None
        else None
    )
    turn_number = _bounded_int(battle.turn_number, "turn_number", 0, 2_147_483_647)
    revision = _bounded_int(battle.revision, "revision", 0, 2_147_483_647)
    if status == "challenge":
        if battle.challenger_ready or battle.opponent_ready or current_player_id is not None or turn_number != 0:
            raise ValueError("A challenge must have no ready players or active turn.")
    elif status == "ready":
        if not (battle.challenger_ready or battle.opponent_ready) or (
            battle.challenger_ready and battle.opponent_ready
        ) or current_player_id is not None or turn_number != 0:
            raise ValueError("A ready battle must have exactly one ready player and no active turn.")
    elif status in {"active", "resolving"}:
        if not (battle.challenger_ready and battle.opponent_ready):
            raise ValueError("An active battle requires both players to be ready.")
        if current_player_id is None or turn_number < 1:
            raise ValueError("An active battle requires a current player and turn number.")
        if ready_deadline is not None:
            raise ValueError("An active battle cannot retain a ready deadline.")
    elif status == "disconnected":
        if disconnected_player_id is None or disconnect_deadline is None:
            raise ValueError("A disconnected battle needs a player and reconnect deadline.")
        if status_before_disconnect not in {"challenge", "ready", "active", "resolving"}:
            raise ValueError("A disconnected battle needs a valid prior status.")
    else:
        if current_player_id is not None:
            raise ValueError("A terminal battle cannot have a current player.")
        if ready_deadline is not None or turn_deadline is not None or disconnect_deadline is not None:
            raise ValueError("A terminal battle cannot retain a deadline.")
        if status == "finished" and winner_player_id is None:
            raise ValueError("A finished battle needs a winner.")
        if status == "cancelled" and cancelled_by_player_id is None:
            # A server cancellation sets no participant, which remains useful
            # for a paired badge being deprovisioned; it must say why instead.
            if battle.end_reason is None:
                raise ValueError("A server-cancelled battle needs an end reason.")
    return BattleRecord(
        battle_id=_identifier(battle.battle_id, "battle_id"),
        challenger_player_id=challenger_player_id,
        challenger_display_name=_text(
            battle.challenger_display_name, "challenger_display_name", maximum=80
        ),
        challenger_roster=challenger_roster,
        challenger_ready=battle.challenger_ready,
        opponent_player_id=opponent_player_id,
        opponent_display_name=_text(
            battle.opponent_display_name, "opponent_display_name", maximum=80
        ),
        opponent_roster=opponent_roster,
        opponent_ready=battle.opponent_ready,
        status=status,
        current_player_id=current_player_id,
        turn_number=turn_number,
        revision=revision,
        created_at=_normalise_datetime(battle.created_at, "created_at"),
        updated_at=_normalise_datetime(battle.updated_at, "updated_at"),
        challenger_badge_id=_optional_identifier(
            battle.challenger_badge_id, "challenger_badge_id"
        ),
        opponent_badge_id=_optional_identifier(battle.opponent_badge_id, "opponent_badge_id"),
        ready_deadline_at=ready_deadline,
        turn_deadline_at=turn_deadline,
        disconnect_deadline_at=disconnect_deadline,
        disconnected_player_id=disconnected_player_id,
        status_before_disconnect=status_before_disconnect,
        winner_player_id=winner_player_id,
        cancelled_by_player_id=cancelled_by_player_id,
        end_reason=_optional_text(battle.end_reason, "end_reason", maximum=160),
        notice=_optional_text(battle.notice, "notice", maximum=400),
        last_visible_rationale=_optional_text(
            battle.last_visible_rationale, "last_visible_rationale", maximum=600
        ),
    )


def _ensure_distinct_battle_rosters(
    challenger: BattleRosterSnapshot, opponent: BattleRosterSnapshot
) -> None:
    overlap = {snapshot.pokemon_id for snapshot in challenger.pokemon} & {
        snapshot.pokemon_id for snapshot in opponent.pokemon
    }
    if overlap:
        raise ValueError("The same Pokemon cannot appear on both battle rosters.")


def _same_battle_pokemon_profile(
    first: BattlePokemonSnapshot, second: BattlePokemonSnapshot
) -> bool:
    first = _validated_battle_pokemon_snapshot(first)
    second = _validated_battle_pokemon_snapshot(second)
    return (
        first.pokemon_id,
        first.name,
        first.species,
        first.types,
        dict(first.stats),
        first.moves,
        first.battle_natures,
        first.flavour,
        first.rarity,
        first.max_hp,
        first.sprite_path,
    ) == (
        second.pokemon_id,
        second.name,
        second.species,
        second.types,
        dict(second.stats),
        second.moves,
        second.battle_natures,
        second.flavour,
        second.rarity,
        second.max_hp,
        second.sprite_path,
    )


def _validate_battle_resolution_rosters(
    old_challenger: BattleRosterSnapshot,
    old_opponent: BattleRosterSnapshot,
    new_challenger: BattleRosterSnapshot,
    new_opponent: BattleRosterSnapshot,
) -> None:
    for old, new, side in (
        (old_challenger, new_challenger, "challenger"),
        (old_opponent, new_opponent, "opponent"),
    ):
        if len(old.pokemon) != len(new.pokemon):
            raise ValueError(f"A resolution cannot change the {side} roster size.")
        for index, (before, after) in enumerate(zip(old.pokemon, new.pokemon)):
            if not _same_battle_pokemon_profile(before, after):
                raise ValueError("A resolution cannot alter a captured Pokemon profile.")
            if before.fainted and not after.fainted:
                raise ValueError("A resolution cannot revive a fainted Pokemon.")
            if index != old.active_index and (
                before.current_hp != after.current_hp
                or dict(before.stat_stages) != dict(after.stat_stages)
            ):
                raise ValueError("Only the active Pokemon on each side may change this turn.")
        if new.active_index != old.active_index and not new.pokemon[old.active_index].fainted:
            raise ValueError("The active index can change only after the old active Pokemon faints.")


def _validate_candidate_state_change(
    candidate: BattleCandidateOutcome,
    battle: BattleRecord,
    challenger_roster: BattleRosterSnapshot,
    opponent_roster: BattleRosterSnapshot,
) -> None:
    """Ensure state is exactly the selected candidate's bounded/clamped result."""

    candidate = _validated_battle_candidate_outcome(candidate)
    actor_is_challenger = battle.current_player_id == battle.challenger_player_id
    old_actor = (
        battle.challenger_roster.active_pokemon
        if actor_is_challenger
        else battle.opponent_roster.active_pokemon
    )
    old_target = (
        battle.opponent_roster.active_pokemon
        if actor_is_challenger
        else battle.challenger_roster.active_pokemon
    )
    new_actor = (
        challenger_roster.pokemon[battle.challenger_roster.active_index]
        if actor_is_challenger
        else opponent_roster.pokemon[battle.opponent_roster.active_index]
    )
    new_target = (
        opponent_roster.pokemon[battle.opponent_roster.active_index]
        if actor_is_challenger
        else challenger_roster.pokemon[battle.challenger_roster.active_index]
    )
    if new_actor.current_hp != _clamp_battle_value(
        old_actor.current_hp + candidate.actor_hp_delta, 0, old_actor.max_hp
    ):
        raise ValueError("The actor HP does not match the selected candidate outcome.")
    if new_target.current_hp != _clamp_battle_value(
        old_target.current_hp + candidate.target_hp_delta, 0, old_target.max_hp
    ):
        raise ValueError("The target HP does not match the selected candidate outcome.")
    for stat_name in _BATTLE_STAT_STAGE_NAMES:
        actor_delta = candidate.actor_stat_delta if candidate.actor_stat == stat_name else 0
        target_delta = candidate.target_stat_delta if candidate.target_stat == stat_name else 0
        if new_actor.stat_stages[stat_name] != _clamp_battle_value(
            old_actor.stat_stages[stat_name] + actor_delta, -6, 6
        ):
            raise ValueError("The actor stat stages do not match the selected candidate outcome.")
        if new_target.stat_stages[stat_name] != _clamp_battle_value(
            old_target.stat_stages[stat_name] + target_delta, -6, 6
        ):
            raise ValueError("The target stat stages do not match the selected candidate outcome.")


def _clamp_battle_value(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _all_battle_pokemon_fainted(roster: BattleRosterSnapshot) -> bool:
    return all(snapshot.fainted for snapshot in roster.pokemon)


def _active_snapshot_for_player(
    battle: BattleRecord, player_id: str
) -> BattlePokemonSnapshot:
    if player_id == battle.challenger_player_id:
        return battle.challenger_roster.active_pokemon
    if player_id == battle.opponent_player_id:
        return battle.opponent_roster.active_pokemon
    raise OwnershipError("Player is not a participant in this battle.")


def _other_battle_player(battle: BattleRecord, player_id: str | None) -> str:
    if player_id == battle.challenger_player_id:
        return battle.opponent_player_id
    if player_id == battle.opponent_player_id:
        return battle.challenger_player_id
    raise ValueError("A battle player is required to identify the opponent.")


def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer from {minimum} to {maximum}.")
    return value


def _validated_world_state(state: PokemonWorldState) -> PokemonWorldState:
    if not isinstance(state, PokemonWorldState):
        raise ValueError("state must be a PokemonWorldState.")
    return PokemonWorldState(
        pokemon_id=_identifier(state.pokemon_id, "pokemon_id"),
        x=_bounded_int(state.x, "x", 0, 100),
        y=_bounded_int(state.y, "y", 0, 100),
        mood=_text(state.mood, "mood", maximum=32),
        energy=_bounded_int(state.energy, "energy", 0, 100),
        activity=_text(state.activity, "activity", maximum=96),
        updated_at=_normalise_datetime(state.updated_at, "updated_at"),
    )


def _normalise_dialogue(
    values: Sequence[SimulationDialogueLine],
) -> tuple[SimulationDialogueLine, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError("dialogue must be a sequence of SimulationDialogueLine values.")
    if len(values) > 4:
        raise ValueError("A simulation event may have at most four dialogue lines.")
    result: list[SimulationDialogueLine] = []
    for line in values:
        if not isinstance(line, SimulationDialogueLine):
            raise ValueError("dialogue entries must be SimulationDialogueLine values.")
        result.append(
            SimulationDialogueLine(
                speaker_pokemon_id=_identifier(line.speaker_pokemon_id, "speaker_pokemon_id"),
                text=_text(line.text, "dialogue text", maximum=240),
            )
        )
    return tuple(result)


def _validated_simulation_event(event: SimulationEventRecord) -> SimulationEventRecord:
    if not isinstance(event, SimulationEventRecord):
        raise ValueError("event must be a SimulationEventRecord.")
    return SimulationEventRecord(
        event_id=_identifier(event.event_id, "event_id"),
        player_id=_identifier(event.player_id, "player_id"),
        revision=_bounded_int(event.revision, "revision", 0, 2_147_483_647),
        actor_pokemon_id=_identifier(event.actor_pokemon_id, "actor_pokemon_id"),
        target_pokemon_id=_optional_identifier(event.target_pokemon_id, "target_pokemon_id"),
        kind=_text(event.kind, "kind", maximum=32),
        summary=_text(event.summary, "summary", maximum=240),
        dialogue=_normalise_dialogue(event.dialogue),
        created_at=_normalise_datetime(event.created_at, "created_at"),
    )


def _normalise_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    return datetime_from_storage(datetime_to_storage(value))


def _pokemon_params(pokemon: PokemonRecord) -> tuple[Any, ...]:
    return (
        pokemon.pokemon_id,
        pokemon.owner_player_id,
        pokemon.captured_by_badge_id,
        pokemon.name,
        pokemon.species,
        json_dumps(list(pokemon.types)),
        json_dumps(dict(pokemon.stats)),
        json_dumps(list(pokemon.moves)),
        json_dumps(list(pokemon.battle_natures)),
        pokemon.flavour,
        pokemon.sprite_prompt,
        pokemon.rarity,
        pokemon.sprite_path,
        datetime_to_storage(pokemon.caught_at),
        json_dumps(json_object(pokemon.metadata, field_name="metadata")),
        datetime_to_storage(pokemon.created_at),
        datetime_to_storage(pokemon.updated_at),
    )


def _pokemon_update_params(pokemon: PokemonRecord) -> tuple[Any, ...]:
    # UPDATE has all values except the immutable primary key first.
    return (
        pokemon.owner_player_id,
        pokemon.captured_by_badge_id,
        pokemon.name,
        pokemon.species,
        json_dumps(list(pokemon.types)),
        json_dumps(dict(pokemon.stats)),
        json_dumps(list(pokemon.moves)),
        json_dumps(list(pokemon.battle_natures)),
        pokemon.flavour,
        pokemon.sprite_prompt,
        pokemon.rarity,
        pokemon.sprite_path,
        datetime_to_storage(pokemon.caught_at),
        json_dumps(json_object(pokemon.metadata, field_name="metadata")),
        datetime_to_storage(pokemon.created_at),
        datetime_to_storage(pokemon.updated_at),
        pokemon.pokemon_id,
    )


def _world_state_params(state: PokemonWorldState) -> tuple[Any, ...]:
    return (
        state.pokemon_id,
        state.x,
        state.y,
        state.mood,
        state.energy,
        state.activity,
        datetime_to_storage(state.updated_at),
    )


def _simulation_event_params(event: SimulationEventRecord) -> tuple[Any, ...]:
    dialogue = [
        {"speaker_pokemon_id": line.speaker_pokemon_id, "text": line.text}
        for line in event.dialogue
    ]
    return (
        event.event_id,
        event.player_id,
        event.revision,
        event.actor_pokemon_id,
        event.target_pokemon_id,
        event.kind,
        event.summary,
        json_dumps(dialogue),
        datetime_to_storage(event.created_at),
    )


def _storage_datetime_or_none(value: datetime | None) -> str | None:
    return datetime_to_storage(value) if value is not None else None


def _battle_insert_params(battle: BattleRecord) -> tuple[Any, ...]:
    """Parameters for the full battle INSERT, in schema column order."""

    battle = _validated_battle_record(battle)
    return (
        battle.battle_id,
        battle.challenger_player_id,
        battle.challenger_display_name,
        battle.challenger_badge_id,
        json_dumps(battle_roster_snapshot_to_dict(battle.challenger_roster)),
        int(battle.challenger_ready),
        battle.opponent_player_id,
        battle.opponent_display_name,
        battle.opponent_badge_id,
        json_dumps(battle_roster_snapshot_to_dict(battle.opponent_roster)),
        int(battle.opponent_ready),
        battle.status,
        battle.current_player_id,
        battle.turn_number,
        battle.revision,
        _storage_datetime_or_none(battle.ready_deadline_at),
        _storage_datetime_or_none(battle.turn_deadline_at),
        _storage_datetime_or_none(battle.disconnect_deadline_at),
        battle.disconnected_player_id,
        battle.status_before_disconnect,
        battle.winner_player_id,
        battle.cancelled_by_player_id,
        battle.end_reason,
        battle.notice,
        battle.last_visible_rationale,
        datetime_to_storage(battle.created_at),
        datetime_to_storage(battle.updated_at),
    )


def _battle_update_params(battle: BattleRecord) -> tuple[Any, ...]:
    """Parameters for the full battle UPDATE, ending with its primary key."""

    return (*_battle_insert_params(battle)[1:], battle.battle_id)


def _battle_turn_insert_params(turn: BattleTurnRecord) -> tuple[Any, ...]:
    turn = _validated_battle_turn(turn)
    candidates = [
        battle_candidate_outcome_to_dict(candidate) for candidate in turn.candidate_outcomes
    ]
    return (
        turn.turn_id,
        turn.battle_id,
        turn.turn_number,
        turn.attempt,
        turn.acting_player_id,
        turn.actor_pokemon_id,
        turn.move_id,
        json_dumps(candidates),
        turn.status,
        datetime_to_storage(turn.created_at),
    )


__all__ = [
    "Badge",
    "BadgeSession",
    "BadgeStore",
    "BadgeStoreError",
    "BattleCandidateOutcome",
    "BattlePokemonSnapshot",
    "BattleRecord",
    "BattleRosterSnapshot",
    "BattleStatus",
    "BattleTurnRecord",
    "BattleTurnStatus",
    "ConflictError",
    "JSONValue",
    "NotFoundError",
    "OwnershipError",
    "OwnershipTransfer",
    "Player",
    "PokemonRecord",
    "PokemonWorldState",
    "SimulationDialogueLine",
    "SimulationEventRecord",
    "badge_to_dict",
    "battle_candidate_outcome_to_dict",
    "battle_pokemon_snapshot_from_pokemon",
    "battle_pokemon_snapshot_to_dict",
    "battle_record_to_dict",
    "battle_roster_snapshot_to_dict",
    "battle_turn_to_dict",
    "datetime_from_storage",
    "datetime_to_storage",
    "json_dumps",
    "json_loads",
    "ownership_transfer_to_dict",
    "player_to_dict",
    "pokemon_from_dict",
    "pokemon_to_dict",
    "session_to_dict",
    "simulation_event_to_dict",
    "utc_now",
    "world_state_to_dict",
]
