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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator, Mapping, Sequence, TypeAlias
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


__all__ = [
    "Badge",
    "BadgeSession",
    "BadgeStore",
    "BadgeStoreError",
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
