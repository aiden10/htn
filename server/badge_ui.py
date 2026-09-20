"""Server-rendered Shutterdex UI primitives for the HTN OS badge.

The badge firmware is deliberately not represented here.  This module turns a
per-badge, JSON-serializable session plus fresh server data into declarative
draw operations.  A transport adapter can translate those operations into the
HTN OS REST/WebSocket protocol without changing any app scene.

There are intentionally no sockets, callbacks, asyncio tasks, or mutable
per-player fields in an app object.  ``BadgeUi`` and the app classes are safe
to share between connections; all user-specific state belongs in
``BadgeSessionState`` and should be persisted by the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
import json
from typing import Any, Iterable, Literal, Mapping, Sequence, TypeAlias


# ---------------------------------------------------------------------------
# JSON/session helpers
# ---------------------------------------------------------------------------

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class UiStateError(ValueError):
    """Raised when a persisted badge UI session has an invalid shape."""


def _json_copy(value: Any, *, label: str) -> JsonValue:
    """Return an independent JSON-only copy, or explain why it cannot persist.

    App state is stored in SQLite as JSON by the runtime.  Checking it at this
    boundary prevents accidental persistence of model objects, callbacks,
    datetimes, or connection handles.
    """

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise UiStateError(f"{label} must contain only JSON-compatible values.") from exc


def _json_object(value: Any, *, label: str) -> JsonObject:
    copied = _json_copy(value, label=label)
    if not isinstance(copied, dict) or not all(isinstance(key, str) for key in copied):
        raise UiStateError(f"{label} must be an object with string keys.")
    return copied


def _int(value: object, default: int = 0) -> int:
    """Safely read an integer from a JSON state object."""

    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _focus_after_button(index: int, count: int, button: "Button", *, columns: int = 1) -> int:
    """Move focus without wrapping past a menu/grid edge."""

    if count <= 0:
        return 0
    index = _clamp(index, 0, count - 1)
    if button is Button.LEFT:
        return max(0, index - 1)
    if button is Button.RIGHT:
        return min(count - 1, index + 1)
    if button is Button.UP:
        return max(0, index - max(1, columns))
    if button is Button.DOWN:
        return min(count - 1, index + max(1, columns))
    return index


# ---------------------------------------------------------------------------
# Declarative drawing operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Clear:
    """Fill the entire display with one colour."""

    color: str = "#0A1020"

    def to_dict(self) -> dict[str, JsonValue]:
        return {"op": "clear", "color": self.color}


@dataclass(frozen=True, slots=True)
class Rect:
    """A filled/stroked rectangle in logical badge pixels."""

    x: int
    y: int
    width: int
    height: int
    fill: str
    stroke: str | None = None
    stroke_width: int = 0
    radius: int = 0

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError("Rectangle width and height cannot be negative.")
        if self.stroke_width < 0 or self.radius < 0:
            raise ValueError("Rectangle stroke width and radius cannot be negative.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "op": "rect",
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "fill": self.fill,
            "stroke": self.stroke,
            "stroke_width": self.stroke_width,
            "radius": self.radius,
        }


@dataclass(frozen=True, slots=True)
class Text:
    """Text positioned at its top-left corner.

    ``scroll`` is declarative.  The gateway may implement it with an HTN OS
    scrolling-text command, or with a tiny scheduled label update; scene code
    never owns a timer.
    """

    x: int
    y: int
    text: str
    color: str = "#F6F7FB"
    size: int = 16
    max_width: int | None = None
    max_lines: int | None = 1
    align: Literal["left", "center", "right"] = "left"
    scroll: bool = False

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("Text size must be positive.")
        if self.max_width is not None and self.max_width < 0:
            raise ValueError("Text max_width cannot be negative.")
        if self.max_lines is not None and self.max_lines <= 0:
            raise ValueError("Text max_lines must be positive when supplied.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "op": "text",
            "x": self.x,
            "y": self.y,
            "text": self.text,
            "color": self.color,
            "size": self.size,
            "max_width": self.max_width,
            "max_lines": self.max_lines,
            "align": self.align,
            "scroll": self.scroll,
        }


@dataclass(frozen=True, slots=True)
class Image:
    """Draw a server-hosted or data-URL image at a known size."""

    x: int
    y: int
    width: int
    height: int
    source: str
    fit: Literal["contain", "cover", "stretch"] = "contain"

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Image width and height must be positive.")
        if not self.source:
            raise ValueError("Image source cannot be blank.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "op": "image",
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "fit": self.fit,
        }


@dataclass(frozen=True, slots=True)
class Leds:
    """Set one or more badge LEDs.  The gateway owns hardware translation."""

    colors: tuple[str, ...]
    brightness: int | None = None

    def __post_init__(self) -> None:
        if not self.colors:
            raise ValueError("At least one LED colour is required.")
        if self.brightness is not None and not 0 <= self.brightness <= 255:
            raise ValueError("LED brightness must be between 0 and 255.")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "op": "leds",
            "colors": list(self.colors),
            "brightness": self.brightness,
        }


DrawOperation: TypeAlias = Clear | Rect | Text | Image | Leds


@dataclass(frozen=True, slots=True)
class Screen:
    """A complete, transport-independent render result."""

    operations: tuple[DrawOperation, ...]
    scene: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "scene": self.scene,
            "operations": [operation.to_dict() for operation in self.operations],
        }


# ---------------------------------------------------------------------------
# Data supplied by the server for one render.  This data is not persisted as
# UI state; it is always read fresh from the authoritative game database.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PokemonCard:
    """The small, presentation-safe Pokemon subset needed by the UI."""

    pokemon_id: str
    name: str
    species: str
    element: str
    flavour: str = ""
    sprite_url: str | None = None
    rarity: str = "common"
    stats: Mapping[str, int] = field(default_factory=dict)
    moves: tuple[str, ...] = ()
    caught_at: str | None = None


@dataclass(frozen=True, slots=True)
class HabitatCreature:
    """A Pokemon's current normalized location in the server simulation."""

    pokemon_id: str
    name: str
    x: int
    y: int
    activity: str = "waiting"
    mood: str = "calm"
    sprite_url: str | None = None
    element: str = "neutral"


@dataclass(frozen=True, slots=True)
class WorldEvent:
    event_id: str
    summary: str
    actor_name: str = ""
    dialogue: str = ""


@dataclass(frozen=True, slots=True)
class BattleOpponent:
    """One other badge currently waiting in the Battle lobby.

    The HTN ID is deliberately the visible identifier here.  It is what a
    person can compare with the small ID shown on another physical badge, and
    it avoids exposing any app credential or internal player identifier.
    """

    htn_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.htn_id, str) or not self.htn_id.strip():
            raise ValueError("A Battle opponent needs a non-empty HTN ID.")


BattlePhase: TypeAlias = Literal[
    "idle",
    "challenge",
    "ready",
    "waiting",
    "active",
    "resolving",
    "winner",
    "finished",
    "cancelled",
    "disconnected",
    "timed_out",
]


@dataclass(frozen=True, slots=True)
class BattleCombatantView:
    """The presentation-safe active-Pokemon snapshot for one battle side.

    Battle persistence owns the authoritative roster snapshot and current HP.
    This deliberately contains only the one active Pokemon the badge needs to
    draw.  The service can replace the object after a faint/switch without
    asking the UI to calculate combat, inspect a database, or mutate a roster.
    """

    pokemon_id: str
    name: str
    element: str
    hp: int
    max_hp: int
    moves: tuple[str, ...] = ()
    sprite_url: str | None = None
    roster_index: int = 0
    roster_size: int = 1
    fainted: bool = False

    def __post_init__(self) -> None:
        if not self.pokemon_id.strip() or not self.name.strip():
            raise ValueError("A battle combatant needs an id and name.")
        if self.max_hp <= 0:
            raise ValueError("A battle combatant max_hp must be positive.")
        if self.hp < 0:
            raise ValueError("A battle combatant hp cannot be negative.")
        if self.roster_index < 0 or self.roster_size <= 0 or self.roster_index >= self.roster_size:
            raise ValueError("Battle roster position must be within its roster size.")
        if len(self.moves) > 4:
            raise ValueError("A battle combatant can expose at most four moves.")

    @property
    def hp_ratio(self) -> float:
        """A safely bounded ratio for drawing a health bar."""

        return max(0.0, min(1.0, self.hp / self.max_hp))


@dataclass(frozen=True, slots=True)
class BattleView:
    """Read-only, per-badge projection of an authoritative shared battle.

    The store/service layer maps its challenger/opponent record into a view
    relative to the badge being rendered: ``viewer_*`` always means the
    currently connected player, and ``opponent_*`` means the other side.
    ``phase`` accepts the battle service's public lifecycle names; the UI also
    tolerates an unknown nonblank phase by rendering a harmless locked view.

    The UI never writes this object.  Button handling only persists a local
    move-focus index; the runtime asks :meth:`BattleApp.requested_action` for
    an intent and lets the battle service validate and apply it atomically.
    """

    battle_id: str
    phase: BattlePhase | str
    viewer_player_id: str
    viewer_player_name: str
    opponent_player_id: str
    opponent_player_name: str
    viewer_role: Literal["challenger", "opponent"] = "challenger"
    viewer_ready: bool = False
    opponent_ready: bool = False
    active_player_id: str | None = None
    viewer: BattleCombatantView | None = None
    opponent: BattleCombatantView | None = None
    turn_number: int = 0
    rationale: str = ""
    last_action: str = ""
    notice: str = ""
    winner_player_id: str | None = None
    end_reason: str = ""
    input_locked: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("battle_id", self.battle_id),
            ("phase", self.phase),
            ("viewer_player_id", self.viewer_player_id),
            ("viewer_player_name", self.viewer_player_name),
            ("opponent_player_id", self.opponent_player_id),
            ("opponent_player_name", self.opponent_player_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Battle {label} must be a non-empty string.")
        if self.viewer_role not in {"challenger", "opponent"}:
            raise ValueError("Battle viewer_role must be challenger or opponent.")
        if self.turn_number < 0:
            raise ValueError("Battle turn_number cannot be negative.")

    @property
    def normalized_phase(self) -> str:
        """Map service aliases to the small set of scenes this UI understands."""

        aliases = {
            "turn": "active",
            "in_progress": "active",
            "generating": "resolving",
            "director": "resolving",
            "complete": "finished",
            "timeout": "timed_out",
        }
        return aliases.get(str(self.phase).strip().lower(), str(self.phase).strip().lower())

    @property
    def viewer_turn(self) -> bool:
        return self.normalized_phase == "active" and self.active_player_id == self.viewer_player_id

    @property
    def is_terminal(self) -> bool:
        # ``disconnected`` is deliberately *not* terminal.  The store retains
        # the shared battle during its reconnect grace period, so both badges
        # must stay on a locked paused scene rather than being offered a
        # misleading "return home" acknowledgement.
        return self.normalized_phase in {"winner", "finished", "cancelled", "timed_out"}


@dataclass(frozen=True, slots=True)
class BadgeUiContext:
    """Read-only server data for a single badge render.

    ``pokemon`` must already be filtered to the current player's collection.
    The database/service layer, not UI code, is responsible for ownership
    authorization.
    """

    badge_id: str
    player_id: str
    pokemon: tuple[PokemonCard, ...] = ()
    creatures: tuple[HabitatCreature, ...] = ()
    events: tuple[WorldEvent, ...] = ()
    world_revision: int = 0
    width: int = 320
    height: int = 240
    # Appended rather than inserted before legacy fields so older callers that
    # constructed BadgeUiContext positionally keep their existing meaning.
    battle: BattleView | None = None
    battle_opponents: tuple[BattleOpponent, ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Badge dimensions must be positive.")


# ---------------------------------------------------------------------------
# Input and session contracts
# ---------------------------------------------------------------------------


class Button(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    A = "a"
    B = "b"
    START = "start"
    HOME = "home"

    @classmethod
    def parse(cls, raw: "Button | str") -> "Button":
        if isinstance(raw, cls):
            return raw
        normalized = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "dpad_up": cls.UP,
            "arrowup": cls.UP,
            "dpad_down": cls.DOWN,
            "arrowdown": cls.DOWN,
            "dpad_left": cls.LEFT,
            "arrowleft": cls.LEFT,
            "dpad_right": cls.RIGHT,
            "arrowright": cls.RIGHT,
            "select": cls.A,
            "confirm": cls.A,
            "accept": cls.A,
            "back": cls.B,
            "cancel": cls.B,
            "menu": cls.HOME,
            "start_button": cls.START,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            choices = ", ".join(button.value for button in cls)
            raise ValueError(f"Unknown button {raw!r}; expected one of {choices}.") from exc


@dataclass(frozen=True, slots=True)
class ButtonEvent:
    """One button press received from the badge service."""

    button: Button
    repeat: bool = False

    @classmethod
    def from_raw(cls, raw: Button | str, *, repeat: bool = False) -> "ButtonEvent":
        return cls(button=Button.parse(raw), repeat=repeat)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": "button", "button": self.button.value, "repeat": self.repeat}


@dataclass(frozen=True, slots=True)
class BadgeSessionState:
    """All mutable Shutterdex UI state for exactly one badge.

    Serialize ``to_dict()`` into the ``badge_sessions.app_state_json`` column.
    ``app_states`` retains each scene's focus independently, so opening Dex,
    returning Home, and reopening Dex restores the prior selection.
    """

    active_app: str = "home"
    app_states: Mapping[str, JsonObject] = field(default_factory=dict)
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.active_app or not isinstance(self.active_app, str):
            raise UiStateError("active_app must be a non-empty string.")
        if self.revision < 0:
            raise UiStateError("Session revision cannot be negative.")
        # Validate now rather than when a database insert fails later.
        for app_id, state in self.app_states.items():
            if not isinstance(app_id, str) or not app_id:
                raise UiStateError("Every app state needs a non-empty string app id.")
            _json_object(state, label=f"app state for {app_id!r}")

    def state_for(self, app_id: str) -> JsonObject | None:
        state = self.app_states.get(app_id)
        return None if state is None else _json_object(state, label=f"app state for {app_id!r}")

    def to_dict(self) -> dict[str, JsonValue]:
        copied_states = {
            app_id: _json_object(state, label=f"app state for {app_id!r}")
            for app_id, state in self.app_states.items()
        }
        return {
            "active_app": self.active_app,
            "app_states": copied_states,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BadgeSessionState":
        if not isinstance(payload, Mapping):
            raise UiStateError("Badge session must be an object.")
        active_app = payload.get("active_app", "home")
        raw_states = payload.get("app_states", {})
        revision = payload.get("revision", 0)
        if not isinstance(active_app, str) or not active_app:
            raise UiStateError("Badge session active_app must be a non-empty string.")
        if not isinstance(raw_states, Mapping):
            raise UiStateError("Badge session app_states must be an object.")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise UiStateError("Badge session revision must be a non-negative integer.")
        states: dict[str, JsonObject] = {}
        for app_id, state in raw_states.items():
            if not isinstance(app_id, str) or not app_id:
                raise UiStateError("Badge session app state ids must be non-empty strings.")
            states[app_id] = _json_object(state, label=f"app state for {app_id!r}")
        return cls(active_app=active_app, app_states=states, revision=revision)


@dataclass(frozen=True, slots=True)
class AppUpdate:
    """A pure reducer result returned by a scene.

    ``navigate_to`` is interpreted by ``BadgeUi``.  Apps never directly call
    one another and never need a badge connection to change views.
    """

    state: JsonObject
    navigate_to: str | None = None

    def __post_init__(self) -> None:
        _json_object(self.state, label="next app state")
        if self.navigate_to is not None and not self.navigate_to:
            raise UiStateError("navigate_to must be a non-empty app id or None.")


class BadgeApp(ABC):
    """Stateless scene definition used by every badge session.

    Implementations may keep immutable visual configuration, but must not keep
    a selected item, badge id, socket, task, or event listener on ``self``.
    The persisted JSON state supplied to these methods is the only per-user
    state.
    """

    app_id: str

    @abstractmethod
    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        """Create the first JSON-only state for one badge entering this app."""

    @abstractmethod
    def reduce(
        self,
        state: JsonObject,
        event: ButtonEvent,
        context: BadgeUiContext,
    ) -> AppUpdate:
        """Purely transform state in response to one event."""

    @abstractmethod
    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        """Return drawing operations for the current state and fresh data."""

    def owns_home_navigation(self, state: JsonObject, context: BadgeUiContext) -> bool:
        """Whether this scene should receive HOME instead of the global router.

        Most apps use HOME as an immediate return to the menu.  A live battle
        must not silently disappear just because one participant presses HOME:
        its reducer receives that button and the battle service can keep the
        shared lifecycle authoritative.
        """

        del state, context
        return False


@dataclass(frozen=True, slots=True)
class Theme:
    background: str = "#241F1B"
    surface: str = "#39302A"
    surface_alt: str = "#4A3E35"
    # Highlight labels need to remain readable over the dark canvas. Mint is
    # the interactive accent; parchment stays as the warm secondary colour.
    focus: str = "#FFF4E5"
    focus_text: str = "#FFF8F0"
    text: str = "#FFF8F0"
    muted: str = "#DECBB5"
    accent: str = "#B3E3A7"
    # Orange is reserved for model/Director activity and rationale.
    director: str = "#F78563"
    danger: str = "#F2817A"


DEFAULT_THEME = Theme()


def _element_colour(element: str) -> str:
    palette = {
        "earth": "#C58B5C",
        "electric": "#F7D34B",
        "grass": "#6BCB77",
        "water": "#62B5F0",
        "fire": "#F78563",
        "air": "#A9D5F4",
        "metal": "#AAB6C3",
        "neutral": "#A5BACD",
    }
    return palette.get(element.strip().lower(), "#A5BACD")


# ---------------------------------------------------------------------------
# Built-in scenes
# ---------------------------------------------------------------------------


class HomeApp(BadgeApp):
    """The server-rendered Shutterdex home menu."""

    app_id = "home"

    def __init__(
        self,
        *,
        theme: Theme = DEFAULT_THEME,
        destinations: Sequence[str] = ("dex", "habitat", "battle"),
    ) -> None:
        if not destinations:
            raise ValueError("HomeApp needs at least one destination.")
        self._theme = theme
        self._destinations = tuple(destinations)

    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        return {"focus": 0}

    def reduce(self, state: JsonObject, event: ButtonEvent, context: BadgeUiContext) -> AppUpdate:
        focus = _clamp(_int(state.get("focus")), 0, len(self._destinations) - 1)
        if event.button in (Button.UP, Button.DOWN):
            focus = _focus_after_button(focus, len(self._destinations), event.button)
            return AppUpdate({"focus": focus})
        if event.button in (Button.A, Button.RIGHT):
            return AppUpdate({"focus": focus}, navigate_to=self._destinations[focus])
        return AppUpdate({"focus": focus})

    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        theme = self._theme
        focus = _clamp(_int(state.get("focus")), 0, len(self._destinations) - 1)
        labels = {
            "dex": ("DEX", "Browse your captured creatures"),
            "habitat": ("HABITAT", "Watch their living world"),
            "battle": ("BATTLE", "Challenge another trainer"),
        }
        operations: list[DrawOperation] = [
            Clear(theme.background),
            # Intentionally no LED command on Home: this gives us a clean
            # battery-power diagnostic while Wi-Fi and Canvas wake together.
            Text(16, 16, "SHUTTERDEX", theme.text, size=26),
            Text(16, 45, "Choose an app", theme.muted, size=14),
        ]
        # Keep every destination selectable on a 320 x 240 badge.  The old
        # two-card menu has room for a third app, but only with compact cards.
        # This remains responsive for a custom HomeApp with a different count.
        card_y = 68
        gap = 7
        card_height = max(
            36,
            (context.height - card_y - 8 - gap * (len(self._destinations) - 1))
            // len(self._destinations),
        )
        for index, destination in enumerate(self._destinations):
            selected = index == focus
            fill = theme.surface_alt if selected else theme.surface
            title_color = theme.text
            sub_color = theme.accent if selected else theme.muted
            title, subtitle = labels.get(destination, (destination.upper(), ""))
            operations.extend(
                (
                    Rect(
                        14,
                        card_y,
                        context.width - 28,
                        card_height,
                        fill,
                        stroke=theme.accent if selected else None,
                        stroke_width=2 if selected else 0,
                        radius=9,
                    ),
                    Text(29, card_y + 7, ("> " if selected else "  ") + title, title_color, size=17),
                    Text(48, card_y + 27, subtitle, sub_color, size=11, max_width=context.width - 70),
                )
            )
            card_y += card_height + gap
        return Screen(tuple(operations), scene=self.app_id)


class DexApp(BadgeApp):
    """Shutterdex grid/detail scene backed by the current player's Pokemon rows."""

    app_id = "dex"

    def __init__(self, *, theme: Theme = DEFAULT_THEME, page_size: int = 4, columns: int = 2) -> None:
        if page_size <= 0 or columns <= 0 or page_size % columns:
            raise ValueError("page_size must be positive and divisible by columns.")
        self._theme = theme
        self._page_size = page_size
        self._columns = columns

    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        return {
            "selected": 0,
            "detail_tab": 0,
            "release_confirm": False,
            "release_notice": "",
        }

    def reduce(self, state: JsonObject, event: ButtonEvent, context: BadgeUiContext) -> AppUpdate:
        count = len(context.pokemon)
        selected = _clamp(_int(state.get("selected")), 0, max(0, count - 1))
        detail_tab = _clamp(_int(state.get("detail_tab")), 0, 1)
        release_confirm = bool(state.get("release_confirm", False))
        release_notice = state.get("release_notice") if isinstance(state.get("release_notice"), str) else ""
        if release_confirm:
            if event.button is Button.B:
                release_confirm = False
            # A is deliberately handled by ShutterdexRuntime so it can apply
            # the owned, durable release operation. Other inputs stay locked.
            return AppUpdate(
                {
                    "selected": selected,
                    "detail_tab": detail_tab,
                    "release_confirm": release_confirm,
                    "release_notice": release_notice,
                }
            )
        if event.button in (Button.UP, Button.DOWN, Button.LEFT, Button.RIGHT):
            selected = _focus_after_button(selected, count, event.button, columns=self._columns)
        elif event.button is Button.A:
            detail_tab = 1 - detail_tab
        elif event.button is Button.START and count:
            release_confirm = True
            release_notice = ""
        elif event.button is Button.B:
            return AppUpdate(
                {"selected": selected, "detail_tab": detail_tab, "release_confirm": False, "release_notice": ""},
                navigate_to="home",
            )
        return AppUpdate(
            {
                "selected": selected,
                "detail_tab": detail_tab,
                "release_confirm": release_confirm,
                "release_notice": release_notice,
            }
        )

    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        theme = self._theme
        pokemon = context.pokemon
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(10, 10, "SHUTTERDEX", theme.text, size=23),
            Text(10, 33, f"{len(pokemon)} CAPTURE{'S' if len(pokemon) != 1 else ''}", theme.muted, size=11),
            # Keep this control outside both large panels. The former
            # bottom-right placement sat inside the detail panel and could be
            # covered by its later draw operations on the badge.
            Text(context.width - 142, 33, "START RELEASE", theme.accent, size=10, max_width=132, align="right"),
            Rect(8, 48, 150, context.height - 56, theme.surface, radius=8),
            Rect(164, 48, context.width - 172, context.height - 56, theme.surface, radius=8),
        ]
        if not pokemon:
            operations.extend(
                (
                    Text(20, 73, "No captures yet", theme.text, size=16, max_width=128),
                    Text(20, 99, "Use the camera to discover a Pokemon.", theme.muted, size=12, max_width=128, max_lines=3),
                )
            )
            return Screen(tuple(operations), scene=self.app_id)

        selected = _clamp(_int(state.get("selected")), 0, len(pokemon) - 1)
        detail_tab = _clamp(_int(state.get("detail_tab")), 0, 1)
        release_confirm = bool(state.get("release_confirm", False))
        release_notice = state.get("release_notice") if isinstance(state.get("release_notice"), str) else ""
        page = selected // self._page_size
        first = page * self._page_size
        visible = pokemon[first : first + self._page_size]
        cell_width, cell_height = 68, 76
        for local_index, mon in enumerate(visible):
            column = local_index % self._columns
            row = local_index // self._columns
            x = 14 + column * 72
            y = 56 + row * 82
            absolute_index = first + local_index
            highlighted = absolute_index == selected
            fill = theme.surface if highlighted else theme.surface_alt
            text_color = theme.text
            operations.append(
                Rect(
                    x,
                    y,
                    cell_width,
                    cell_height,
                    fill,
                    stroke=theme.accent if highlighted else None,
                    stroke_width=2 if highlighted else 0,
                    radius=7,
                )
            )
            if mon.sprite_url:
                operations.append(Image(x + 17, y + 6, 34, 34, mon.sprite_url))
            else:
                operations.append(Text(x + 26, y + 14, mon.name[:1].upper(), text_color, size=25))
            operations.append(Text(x + 5, y + 43, mon.name, text_color, size=12, max_width=58, scroll=True))
            operations.append(
                Text(x + 5, y + 60, mon.element.upper(), theme.accent if highlighted else _element_colour(mon.element), size=10, max_width=58, scroll=True)
            )

        mon = pokemon[selected]
        detail_x, detail_y, detail_width = 171, 56, context.width - 186
        if mon.sprite_url:
            operations.append(Image(detail_x, detail_y, 56, 56, mon.sprite_url))
        else:
            operations.append(Rect(detail_x, detail_y, 56, 56, _element_colour(mon.element), radius=12))
            operations.append(Text(detail_x + 19, detail_y + 15, mon.name[:1].upper(), theme.focus_text, size=26))
        operations.extend(
            (
                Text(detail_x + 62, detail_y + 2, mon.name, theme.text, size=16, max_width=detail_width - 62, scroll=True),
                Text(detail_x + 62, detail_y + 25, mon.element.upper(), _element_colour(mon.element), size=11, max_width=detail_width - 62, scroll=True),
                Text(detail_x, detail_y + 64, "PROFILE" if detail_tab == 0 else "STATS + MOVES", theme.focus, size=11),
            )
        )
        if detail_tab == 0:
            operations.extend(
                (
                    Text(detail_x, detail_y + 83, mon.species, theme.text, size=13, max_width=detail_width, scroll=True),
                    Text(detail_x, detail_y + 111, mon.rarity.upper(), theme.muted, size=10),
                    Text(detail_x, detail_y + 130, mon.flavour, theme.muted, size=11, max_width=detail_width, max_lines=3, scroll=True),
                )
            )
        else:
            stat_y = detail_y + 84
            stats = tuple(mon.stats.items())[:4]
            if not stats:
                operations.append(Text(detail_x, stat_y, "No stats recorded", theme.muted, size=12, max_width=detail_width))
            for index, (label, value) in enumerate(stats):
                operations.append(
                    Text(
                        detail_x + (index % 2) * 67,
                        stat_y + (index // 2) * 16,
                        f"{label.upper()[:3]} {value}",
                        theme.text,
                        size=10,
                        max_width=63,
                    )
                )
            move_y = detail_y + 118
            operations.append(Text(detail_x, move_y, "MOVES", theme.muted, size=10))
            moves = mon.moves[:4]
            if not moves:
                operations.append(Text(detail_x, move_y + 16, "No moves recorded", theme.muted, size=10, max_width=detail_width))
            for index, move in enumerate(moves):
                operations.append(
                    Text(
                        detail_x + (index % 2) * 67,
                        move_y + 15 + (index // 2) * 17,
                        move.replace("_", " ").upper(),
                        theme.text,
                        size=9,
                        max_width=63,
                        scroll=True,
                    )
                )
        operations.append(Text(14, context.height - 19, f"PAGE {page + 1}/{max(1, (len(pokemon) + self._page_size - 1) // self._page_size)}", theme.muted, size=10))
        if release_confirm:
            operations.extend(
                (
                    Rect(20, 69, context.width - 40, 106, theme.surface_alt, stroke=theme.accent, stroke_width=2, radius=12),
                    Text(36, 85, "RELEASE THIS CREATURE?", theme.text, size=15, max_width=context.width - 72),
                    Text(36, 111, mon.name, theme.accent, size=18, max_width=context.width - 72, scroll=True),
                    Text(36, 143, "A RELEASE   B CANCEL", theme.muted, size=12, max_width=context.width - 72),
                )
            )
        elif release_notice:
            operations.append(
                Text(10, 34, release_notice, theme.danger, size=10, max_width=context.width - 20, align="right", scroll=True)
            )
        return Screen(tuple(operations), scene=self.app_id)


class HabitatApp(BadgeApp):
    """Living-world scene.  It renders server simulation positions; it does not simulate."""

    app_id = "habitat"
    BACKGROUND_IDS = (
        "sunrise",
        "early_morning",
        "late_morning",
        "noon",
        "afternoon",
        "golden_hour",
        "dusk",
        "night",
    )

    def __init__(self, *, theme: Theme = DEFAULT_THEME) -> None:
        self._theme = theme

    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        return {
            "selected": 0,
            "event_index": max(0, len(context.events) - 1),
            "panel": "event",
            "busy": False,
            "notice": "",
            "background_index": 0,
        }

    def reduce(self, state: JsonObject, event: ButtonEvent, context: BadgeUiContext) -> AppUpdate:
        creatures = self._creatures(context)
        selected = _clamp(_int(state.get("selected")), 0, max(0, len(creatures) - 1))
        event_index = _clamp(_int(state.get("event_index")), 0, max(0, len(context.events) - 1))
        panel = (
            state.get("panel")
            if state.get("panel") in {"creature", "event", "status"}
            else "event"
        )
        busy = bool(state.get("busy", False))
        notice = state.get("notice") if isinstance(state.get("notice"), str) else ""
        background_index = _clamp(
            _int(state.get("background_index")), 0, len(self.BACKGROUND_IDS) - 1
        )
        if event.button is Button.LEFT:
            selected = _focus_after_button(selected, len(creatures), Button.LEFT)
            panel, notice = "creature", ""
        elif event.button is Button.RIGHT:
            selected = _focus_after_button(selected, len(creatures), Button.RIGHT)
            panel, notice = "creature", ""
        elif event.button is Button.UP and context.events:
            event_index = _focus_after_button(event_index, len(context.events), Button.UP)
            panel, notice = "event", ""
        elif event.button is Button.DOWN and context.events:
            event_index = _focus_after_button(event_index, len(context.events), Button.DOWN)
            panel, notice = "event", ""
        # The runtime owns A's asynchronous side effect. It sets ``busy``
        # before requesting the writer and Jev so the badge responds instantly.
        elif event.button is Button.B:
            return AppUpdate(
                {
                    "selected": selected,
                    "event_index": event_index,
                    "panel": panel,
                    "busy": busy,
                    "notice": notice,
                },
                navigate_to="home",
            )
        return AppUpdate(
            {
                "selected": selected,
                "event_index": event_index,
                "panel": panel,
                "busy": busy,
                "notice": notice,
                "background_index": background_index,
            }
        )

    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        theme = self._theme
        creatures = self._creatures(context)
        selected = _clamp(_int(state.get("selected")), 0, max(0, len(creatures) - 1))
        event_index = _clamp(_int(state.get("event_index")), 0, max(0, len(context.events) - 1))
        panel = (
            state.get("panel")
            if state.get("panel") in {"creature", "event", "status"}
            else "event"
        )
        busy = bool(state.get("busy", False))
        notice = state.get("notice") if isinstance(state.get("notice"), str) else ""
        background_index = _clamp(
            _int(state.get("background_index")), 0, len(self.BACKGROUND_IDS) - 1
        )
        world_top, world_bottom = 50, context.height - 66
        world_height = world_bottom - world_top
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(10, 10, "HABITAT", theme.text, size=23),
            Text(
                context.width - 105,
                15,
                "DIRECTING..." if busy else f"WORLD {context.world_revision}",
                theme.director if busy else theme.muted,
                size=10,
                max_width=96,
                align="right",
            ),
            Text(
                10,
                36,
                "DIRECTING - CONTROLS LOCKED" if busy else "L/R CREATURE   U/D MOMENTS   A ADVANCE",
                theme.director if busy else theme.muted,
                size=9,
                max_width=context.width - 20,
            ),
            Image(
                8,
                world_top,
                context.width - 16,
                world_height,
                f"habitat://{self.BACKGROUND_IDS[background_index]}",
            ),
        ]
        if not creatures:
            operations.append(Text(20, world_top + 24, "No Pokemon are in this habitat yet.", theme.text, size=14, max_width=context.width - 40, max_lines=2))
        for index, creature in enumerate(creatures):
            # Positions are normalized by the simulation.  Clamp at the UI edge
            # so a malformed world row can never draw outside the badge canvas.
            x = 14 + round(_clamp(creature.x, 0, 100) * (context.width - 70) / 100)
            y = world_top + 8 + round(_clamp(creature.y, 0, 100) * (world_height - 52) / 100)
            x = _clamp(x, 14, context.width - 54)
            y = _clamp(y, world_top + 4, world_bottom - 44)
            focused = index == selected
            if focused:
                operations.append(Rect(x - 4, y - 4, 48, 48, "#0D1F2B", stroke=theme.accent, stroke_width=2, radius=10))
            if creature.sprite_url:
                operations.append(Image(x, y, 40, 40, creature.sprite_url))
            else:
                operations.extend(
                    (
                        Rect(x, y, 40, 40, _element_colour(creature.element), radius=12),
                        Text(x + 13, y + 10, creature.name[:1].upper(), theme.focus_text, size=20),
                    )
                )
            operations.append(Text(x - 3, y + 42, creature.name, theme.text, size=10, max_width=50, align="center", scroll=True))

        panel_y = context.height - 58
        operations.append(Rect(8, panel_y, context.width - 16, 50, theme.surface, radius=8))
        if notice:
            operations.extend(
                (
                    Text(17, panel_y + 7, "HABITAT NOTICE", theme.danger, size=11, max_width=context.width - 34),
                    Text(17, panel_y + 25, notice, theme.text, size=12, max_width=context.width - 34, scroll=True),
                )
            )
        elif busy and panel == "status":
            operations.extend(
                (
                    Text(17, panel_y + 7, "HABITAT DIRECTOR", theme.director, size=11, max_width=context.width - 34),
                    Text(17, panel_y + 25, "Working automatically. Controls unlock when done.", theme.text, size=12, max_width=context.width - 34, scroll=True),
                )
            )
        elif panel == "creature" and creatures:
            creature = creatures[selected]
            operations.extend(
                (
                    Text(17, panel_y + 7, f"{creature.name}  {creature.mood.upper()}", theme.focus, size=11, max_width=context.width - 34, scroll=True),
                    Text(17, panel_y + 25, creature.activity, theme.text, size=12, max_width=context.width - 34, scroll=True),
                )
            )
        elif context.events:
            event = context.events[event_index]
            heading = (
                f"LATEST MOMENT  {event.actor_name or 'WORLD'}"
                if event_index == len(context.events) - 1
                else f"MOMENT {event_index + 1}/{len(context.events)}  {event.actor_name or 'WORLD'}"
            )
            body = event.dialogue or event.summary
            operations.extend(
                (
                    Text(17, panel_y + 7, heading, theme.focus, size=11, max_width=context.width - 34, scroll=True),
                    Text(17, panel_y + 25, body, theme.text, size=12, max_width=context.width - 34, scroll=True),
                )
            )
        elif creatures:
            creature = creatures[selected]
            operations.extend(
                (
                    Text(17, panel_y + 7, creature.name, theme.focus, size=11, max_width=context.width - 34),
                    Text(17, panel_y + 25, creature.activity, theme.text, size=12, max_width=context.width - 34, scroll=True),
                )
            )
        return Screen(tuple(operations), scene=self.app_id)

    @staticmethod
    def _creatures(context: BadgeUiContext) -> tuple[HabitatCreature, ...]:
        """Supply a harmless static fallback when only Shutterdex data is loaded."""

        if context.creatures:
            return context.creatures
        fallback: list[HabitatCreature] = []
        for index, mon in enumerate(context.pokemon):
            fallback.append(
                HabitatCreature(
                    pokemon_id=mon.pokemon_id,
                    name=mon.name,
                    x=18 + (index * 37) % 72,
                    y=24 + (index * 29) % 62,
                    activity="arriving in the habitat",
                    sprite_url=mon.sprite_url,
                    element=mon.element,
                )
            )
        return tuple(fallback)


BattleUiAction: TypeAlias = Literal[
    "select_opponent",
    "accept_challenge",
    "ready",
    "select_move",
    "cancel_battle",
    "leave_terminal",
]


class BattleApp(BadgeApp):
    """Shared-battle presentation and local move focus.

    ``BattleView`` is an immutable projection of one row shared by two badges.
    It is intentionally the source of truth for the lifecycle, roster, turn,
    and outcome.  This app persists only ``move_index`` in a badge session so
    two players can independently browse their four moves without racing over
    shared state.  A runtime can call :meth:`requested_action` after routing a
    button to turn a valid press into one battle-service command.
    """

    app_id = "battle"

    def __init__(self, *, theme: Theme = DEFAULT_THEME) -> None:
        self._theme = theme

    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        del context
        return {
            "move_index": 0,
            "opponent_index": 0,
            "challenge_target": "",
            "terminal_battle_id": "",
            "busy": False,
        }

    def owns_home_navigation(self, state: JsonObject, context: BadgeUiContext) -> bool:
        """Do not let HOME silently abandon a nonterminal shared battle."""

        del state
        return bool(
            context.battle is not None
            and context.battle.normalized_phase != "idle"
            and not context.battle.is_terminal
        )

    def reduce(self, state: JsonObject, event: ButtonEvent, context: BadgeUiContext) -> AppUpdate:
        battle = context.battle
        move_index = self.selected_move_index(state, context)
        opponent_index = self.selected_opponent_index(state, context)
        challenge_target = self.selected_challenge_target(state)
        terminal_battle_id = self.terminal_battle_id(state)
        busy = bool(state.get("busy"))
        if busy:
            # A runtime-owned action is still updating shared state.  Preserve
            # the local focus but do not let any button mutate it or navigate.
            return AppUpdate(
                {
                    "move_index": move_index,
                    "opponent_index": opponent_index,
                    "challenge_target": challenge_target,
                    "terminal_battle_id": terminal_battle_id,
                    "busy": True,
                }
            )
        if battle is None and event.button in (Button.UP, Button.DOWN):
            opponent_index = _focus_after_button(
                opponent_index, len(context.battle_opponents), event.button
            )
        if battle is not None and self._can_choose_move(battle):
            move_index = _focus_after_button(
                move_index,
                len(battle.viewer.moves) if battle.viewer else 0,
                event.button,
                columns=2,
            )

        # Terminal screens are acknowledgement-only.  Every nonterminal
        # command is interpreted by the runtime through requested_action(),
        # never through a mutation of UI/session state.
        if battle is None and event.button is Button.B:
            return AppUpdate(
                {
                    "move_index": move_index,
                    "opponent_index": opponent_index,
                    "challenge_target": "",
                    "terminal_battle_id": "",
                    "busy": False,
                },
                navigate_to="home",
            )
        if battle is not None and battle.is_terminal and event.button in (Button.A, Button.B):
            return AppUpdate(
                {
                    "move_index": move_index,
                    "opponent_index": opponent_index,
                    "challenge_target": challenge_target,
                    # A terminal result is intentionally one-shot. Once it
                    # has been acknowledged, its audit row must not keep
                    # replacing a later Battle lobby.
                    "terminal_battle_id": "",
                    "busy": False,
                },
                navigate_to="home",
            )
        return AppUpdate(
            {
                "move_index": move_index,
                "opponent_index": opponent_index,
                "challenge_target": challenge_target,
                "terminal_battle_id": terminal_battle_id,
                "busy": False,
            }
        )

    def requested_action(
        self,
        state: JsonObject,
        event: ButtonEvent,
        context: BadgeUiContext,
    ) -> BattleUiAction | None:
        """Return an intent a runtime may submit to the battle service.

        This is deliberately a pure query: it neither changes the local
        selection nor trusts that the resulting action will be accepted.  The
        service must still authorize participants, check the current revision,
        enforce turn ownership, and transition shared state atomically.
        """

        if bool(state.get("busy")):
            return None
        battle = context.battle
        if battle is None or battle.normalized_phase == "idle":
            return (
                "select_opponent"
                if event.button is Button.A and self.selected_opponent_htn_id(state, context)
                else None
            )
        if battle.is_terminal:
            return "leave_terminal" if event.button in (Button.A, Button.B) else None
        if event.button is Button.B and not battle.input_locked:
            return "cancel_battle"

        phase = battle.normalized_phase
        if phase == "challenge":
            if event.button is Button.A and battle.viewer_role == "opponent":
                return "accept_challenge"
            return None
        if phase in {"ready", "waiting"}:
            if event.button is Button.A and not battle.viewer_ready and not battle.input_locked:
                return "ready"
            return None
        if phase == "active" and event.button is Button.A and self._can_choose_move(battle):
            return "select_move"
        return None

    @staticmethod
    def selected_move_index(state: JsonObject, context: BadgeUiContext) -> int:
        """Return the valid local focus position for the current active moves."""

        count = len(context.battle.viewer.moves) if context.battle and context.battle.viewer else 0
        return _clamp(_int(state.get("move_index")), 0, max(0, count - 1))

    @staticmethod
    def selected_opponent_index(state: JsonObject, context: BadgeUiContext) -> int:
        """Return the local focus position in the waiting-badge list."""

        return _clamp(
            _int(state.get("opponent_index")),
            0,
            max(0, len(context.battle_opponents) - 1),
        )

    @staticmethod
    def selected_challenge_target(state: JsonObject) -> str:
        """Read one prior, persisted invitation target without trusting it."""

        target = state.get("challenge_target", "")
        return target.strip() if isinstance(target, str) else ""

    @staticmethod
    def terminal_battle_id(state: JsonObject) -> str:
        """Return the explicitly presented terminal battle, if any."""

        battle_id = state.get("terminal_battle_id", "")
        return battle_id.strip() if isinstance(battle_id, str) else ""

    def selected_opponent_htn_id(
        self, state: JsonObject, context: BadgeUiContext
    ) -> str | None:
        """Return the HTN ID currently focused in the reciprocal lobby."""

        if not context.battle_opponents:
            return None
        return context.battle_opponents[
            self.selected_opponent_index(state, context)
        ].htn_id

    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        battle = context.battle
        if battle is not None and bool(state.get("busy")):
            # The SQLite row may still say ``active`` while a Writer request is
            # being made.  Present the same explicit, input-locked Director
            # phase immediately rather than waiting for a network response.
            battle = replace(
                battle,
                phase="resolving",
                input_locked=True,
                notice="The Director is preparing this turn.",
            )
        if battle is None or battle.normalized_phase == "idle":
            return self._render_discovery(state, context)
        if battle.is_terminal:
            return self._render_terminal(battle, context)
        if battle.normalized_phase in {"challenge", "ready", "waiting"}:
            return self._render_setup(battle, context)
        if battle.normalized_phase in {"active", "resolving"}:
            return self._render_arena(battle, self.selected_move_index(state, context), context)
        return self._render_paused(battle, context)

    def _render_discovery(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        """Draw the reciprocal lobby using only visible HTN badge IDs."""

        theme = self._theme
        opponents = context.battle_opponents
        focus = self.selected_opponent_index(state, context)
        target = self.selected_challenge_target(state)
        busy = bool(state.get("busy"))
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(12, 13, "BATTLE", theme.text, size=24),
            Text(12, 42, "WAITING FOR CHALLENGE", theme.muted, size=11),
        ]
        if not opponents:
            operations.extend(
                (
                    Rect(12, 66, context.width - 24, 103, theme.surface, radius=10),
                    Text(25, 84, "NO BADGES WAITING", theme.focus, size=16, max_width=context.width - 50),
                    Text(
                        25,
                        113,
                        "Open Battle on another paired badge. Its HTN ID will appear here.",
                        theme.text,
                        size=13,
                        max_width=context.width - 50,
                        max_lines=3,
                        scroll=True,
                    ),
                    Text(15, context.height - 26, "B HOME", theme.muted, size=11, max_width=context.width - 30),
                )
            )
            return Screen(tuple(operations), scene=self.app_id)

        operations.append(
            Text(
                18,
                62,
                "Choose the badge you can see. Both badges must choose each other.",
                theme.muted,
                size=10,
                max_width=context.width - 36,
                max_lines=2,
                scroll=True,
            )
        )
        for index, opponent in enumerate(opponents[:4]):
            selected = index == focus
            y = 89 + index * 27
            fill = theme.surface_alt if selected else theme.surface
            colour = theme.text
            marker = "> " if selected else "  "
            operations.extend(
                (
                    Rect(
                        16,
                        y,
                        context.width - 32,
                        23,
                        fill,
                        stroke=theme.accent if selected else None,
                        stroke_width=2 if selected else 0,
                        radius=6,
                    ),
                    Text(28, y + 5, marker + opponent.htn_id.upper(), colour, size=12, max_width=context.width - 56),
                )
            )
        if target:
            status = (
                f"LINKING TO {target.upper()}..." if busy else f"YOUR PICK: {target.upper()}"
            )
            operations.append(
                Text(16, 202, status, theme.accent, size=10, max_width=context.width - 32, scroll=True)
            )
        footer = "LINKING..." if busy else "UP/DOWN CHOOSE   A SELECT   B HOME"
        operations.append(
            Text(15, context.height - 26, footer, theme.muted, size=10, max_width=context.width - 30, scroll=True)
        )
        return Screen(tuple(operations), scene=self.app_id)

    def _render_setup(self, battle: BattleView, context: BadgeUiContext) -> Screen:
        theme = self._theme
        phase = battle.normalized_phase
        viewer_ready = "READY" if battle.viewer_ready else "NOT READY"
        opponent_ready = "READY" if battle.opponent_ready else "NOT READY"
        if battle.input_locked:
            heading = "SYNCING BATTLE"
            body = "The shared battle state is updating. Controls unlock automatically."
            footer = "CONTROLS LOCKED"
        elif phase == "challenge":
            if battle.viewer_role == "opponent":
                heading = "CHALLENGE DETECTED"
                body = f"{battle.opponent_player_name} wants to battle you."
                footer = "A ACCEPT   B DECLINE"
            else:
                heading = "CHALLENGE SENT"
                body = f"Waiting for {battle.opponent_player_name} to accept."
                footer = "WAITING - B CANCEL"
        elif battle.viewer_ready and not battle.opponent_ready:
            heading = "YOU ARE READY"
            body = f"Waiting for {battle.opponent_player_name} to ready up."
            footer = "WAITING - B CANCEL"
        elif not battle.viewer_ready:
            heading = "READY CHECK"
            body = "Confirm your roster when you are ready to begin."
            footer = "A READY   B CANCEL"
        else:
            heading = "READY CHECK"
            body = "Both trainers are ready. Starting the battle..."
            footer = "STARTING - CONTROLS LOCKED"

        if battle.notice:
            body = battle.notice
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(12, 13, "BATTLE LINK", theme.text, size=24),
            Text(12, 43, heading, theme.focus, size=15, max_width=context.width - 24, scroll=True),
            Rect(12, 68, context.width - 24, 50, theme.surface, radius=9),
            Text(26, 80, f"{battle.viewer_player_name}: {viewer_ready}", theme.text, size=13, max_width=context.width - 52, scroll=True),
            Text(26, 102, f"{battle.opponent_player_name}: {opponent_ready}", theme.text, size=13, max_width=context.width - 52, scroll=True),
            Rect(12, 130, context.width - 24, 60, theme.surface_alt, radius=9),
            Text(25, 142, body, theme.text, size=13, max_width=context.width - 50, max_lines=3, scroll=True),
            Text(15, context.height - 26, footer, theme.muted, size=11, max_width=context.width - 30, scroll=True),
        ]
        return Screen(tuple(operations), scene=self.app_id)

    def _render_arena(self, battle: BattleView, move_index: int, context: BadgeUiContext) -> Screen:
        theme = self._theme
        resolving = battle.normalized_phase == "resolving"
        viewer_turn = self._can_choose_move(battle)
        if resolving:
            status = "DIRECTOR RESOLVING - CONTROLS LOCKED"
            status_color = theme.director
        elif viewer_turn:
            status = "YOUR TURN - CHOOSE A MOVE"
            status_color = theme.accent
        else:
            status = f"{battle.opponent_player_name.upper()} TURN - CONTROLS LOCKED"
            status_color = theme.muted
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(10, 8, "BATTLE", theme.text, size=21),
            Text(context.width - 96, 13, f"TURN {max(1, battle.turn_number)}", theme.muted, size=11, max_width=86, align="right"),
            Text(10, 32, status, status_color, size=10, max_width=context.width - 20, scroll=True),
        ]
        self._append_combatant_card(
            operations,
            x=8,
            y=49,
            width=148,
            combatant=battle.viewer,
            label="YOU",
            theme=theme,
        )
        self._append_combatant_card(
            operations,
            x=164,
            y=49,
            width=context.width - 172,
            combatant=battle.opponent,
            label=battle.opponent_player_name.upper(),
            theme=theme,
        )

        narrative = battle.rationale or battle.last_action or battle.notice
        narrative_heading = "DIRECTOR RATIONALE" if battle.rationale else "BATTLE UPDATE"
        narrative_colour = theme.director if battle.rationale else theme.focus
        operations.extend(
            (
                Rect(8, 124, context.width - 16, 39, theme.surface, radius=8),
                Text(17, 129, narrative_heading, narrative_colour, size=9, max_width=context.width - 34),
                Text(
                    17,
                    143,
                    narrative or "Choose a move to see how the clash unfolds.",
                    theme.text,
                    size=11,
                    max_width=context.width - 34,
                    scroll=True,
                ),
                Text(10, 168, "MOVES", theme.muted, size=10),
            )
        )
        self._append_moves(operations, battle, move_index, enabled=viewer_turn, context=context)
        return Screen(tuple(operations), scene=self.app_id)

    def _render_paused(self, battle: BattleView, context: BadgeUiContext) -> Screen:
        theme = self._theme
        reconnecting = battle.normalized_phase == "disconnected"
        body = (
            battle.notice or "Waiting for the other player to reconnect."
            if reconnecting
            else battle.notice or "Waiting for the battle service to update this match."
        )
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(12, 13, "BATTLE", theme.text, size=24),
            Text(
                12,
                43,
                "RECONNECTING PLAYER" if reconnecting else "SHARED STATE PAUSED",
                theme.focus,
                size=15,
            ),
            Rect(12, 68, context.width - 24, 91, theme.surface, radius=9),
            Text(25, 83, body, theme.text, size=13, max_width=context.width - 50, max_lines=4, scroll=True),
            Text(15, context.height - 26, "CONTROLS LOCKED", theme.muted, size=11, max_width=context.width - 30),
        ]
        return Screen(tuple(operations), scene=self.app_id)

    def _render_terminal(self, battle: BattleView, context: BadgeUiContext) -> Screen:
        theme = self._theme
        phase = battle.normalized_phase
        if phase in {"winner", "finished"}:
            if battle.winner_player_id == battle.viewer_player_id:
                heading, colour = "YOU WIN!", theme.accent
            elif battle.winner_player_id:
                heading, colour = f"{battle.opponent_player_name.upper()} WINS", theme.danger
            else:
                heading, colour = "BATTLE COMPLETE", theme.focus
        elif phase == "cancelled":
            heading, colour = "BATTLE CANCELLED", theme.muted
        elif phase == "disconnected":
            heading, colour = "OPPONENT DISCONNECTED", theme.danger
        else:
            heading, colour = "BATTLE TIMED OUT", theme.danger
        body = battle.end_reason or battle.notice or battle.rationale or "The shared battle has ended."
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Text(12, 13, "BATTLE", theme.text, size=24),
            Rect(12, 63, context.width - 24, 105, theme.surface, radius=10),
            Text(25, 81, heading, colour, size=18, max_width=context.width - 50, scroll=True),
            Text(25, 116, body, theme.text, size=13, max_width=context.width - 50, max_lines=3, scroll=True),
            Text(15, context.height - 26, "A OR B RETURN HOME", theme.muted, size=11),
        ]
        return Screen(tuple(operations), scene=self.app_id)

    @staticmethod
    def _can_choose_move(battle: BattleView) -> bool:
        return bool(
            battle.viewer_turn
            and not battle.input_locked
            and battle.viewer is not None
            and not battle.viewer.fainted
            and battle.viewer.moves
        )

    @staticmethod
    def _append_combatant_card(
        operations: list[DrawOperation],
        *,
        x: int,
        y: int,
        width: int,
        combatant: BattleCombatantView | None,
        label: str,
        theme: Theme,
    ) -> None:
        operations.append(Rect(x, y, width, 67, theme.surface_alt, radius=8))
        operations.append(Text(x + 7, y + 5, label, theme.muted, size=9, max_width=width - 14, scroll=True))
        if combatant is None:
            operations.append(Text(x + 8, y + 31, "Awaiting roster", theme.muted, size=11, max_width=width - 16))
            return
        if combatant.sprite_url:
            operations.append(Image(x + 7, y + 23, 35, 35, combatant.sprite_url))
        else:
            operations.extend(
                (
                    Rect(x + 7, y + 23, 35, 35, _element_colour(combatant.element), radius=10),
                    Text(x + 18, y + 31, combatant.name[:1].upper(), theme.focus_text, size=17),
                )
            )
        name_colour = theme.danger if combatant.fainted else theme.text
        operations.extend(
            (
                Text(x + 47, y + 22, combatant.name, name_colour, size=11, max_width=width - 54, scroll=True),
                Text(x + 47, y + 37, combatant.element.upper(), _element_colour(combatant.element), size=9, max_width=width - 54, scroll=True),
                Text(x + 47, y + 51, f"HP {max(0, combatant.hp)}/{combatant.max_hp}", theme.muted, size=8, max_width=width - 54),
                Rect(x + 47, y + 61, width - 55, 3, "#08111F", radius=2),
                Rect(x + 47, y + 61, round((width - 55) * combatant.hp_ratio), 3, _element_colour(combatant.element), radius=2),
                Text(x + 7, y + 59, f"{combatant.roster_index + 1}/{combatant.roster_size}", theme.muted, size=8, max_width=35),
            )
        )

    def _append_moves(
        self,
        operations: list[DrawOperation],
        battle: BattleView,
        move_index: int,
        *,
        enabled: bool,
        context: BadgeUiContext,
    ) -> None:
        theme = self._theme
        moves = battle.viewer.moves if battle.viewer else ()
        if not moves:
            operations.append(Text(17, 192, "No moves available", theme.muted, size=12))
            return
        cell_width = (context.width - 24) // 2
        for index, move in enumerate(moves[:4]):
            column, row = index % 2, index // 2
            x, y = 8 + column * (cell_width + 8), 178 + row * 28
            selected = enabled and index == move_index
            fill = theme.surface if selected else theme.surface_alt
            text_colour = theme.text if enabled else theme.muted
            operations.extend(
                (
                    Rect(
                        x,
                        y,
                        cell_width,
                        24,
                        fill,
                        stroke=theme.accent if selected else None,
                        stroke_width=2 if selected else 0,
                        radius=6,
                    ),
                    Text(
                        x + 7,
                        y + 6,
                        ("> " if selected else "  ") + move.replace("_", " ").upper(),
                        text_colour,
                        size=10,
                        max_width=cell_width - 14,
                        scroll=True,
                    ),
                )
            )


# ---------------------------------------------------------------------------
# Stateless routing/controller
# ---------------------------------------------------------------------------


class AppRegistry:
    """Immutable-ish registry of stateless scene definitions."""

    def __init__(self, apps: Iterable[BadgeApp]) -> None:
        self._apps: dict[str, BadgeApp] = {}
        for app in apps:
            self.register(app)

    def register(self, app: BadgeApp) -> None:
        app_id = getattr(app, "app_id", "")
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("Every BadgeApp must have a non-empty app_id.")
        if app_id in self._apps:
            raise ValueError(f"An app named {app_id!r} is already registered.")
        self._apps[app_id] = app

    def get(self, app_id: str) -> BadgeApp:
        try:
            return self._apps[app_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._apps))
            raise UiStateError(f"Unknown app {app_id!r}; registered apps: {available}.") from exc

    @property
    def app_ids(self) -> tuple[str, ...]:
        return tuple(self._apps)


class BadgeUi:
    """Routes input to a badge's active scene and produces draw operations.

    A runtime should keep one *session record* per badge in SQLite, but may use
    one shared ``BadgeUi`` instance for every badge connection.  Typical use::

        session = BadgeSessionState.from_dict(row.app_state_json)
        event = ButtonEvent.from_raw(incoming_button)
        session, screen = ui.handle(session, event, render_context)
        save_session(session.to_dict())
        await gateway.render(badge_id, screen.operations)
    """

    def __init__(self, registry: AppRegistry, *, home_app_id: str = "home") -> None:
        self._registry = registry
        self._home_app_id = home_app_id
        self._registry.get(home_app_id)

    @classmethod
    def standard(cls, *, theme: Theme = DEFAULT_THEME) -> "BadgeUi":
        """Build Shutterdex's initial Home, Dex, Habitat, and Battle app set."""

        return cls(
            AppRegistry(
                (
                    HomeApp(theme=theme),
                    DexApp(theme=theme),
                    HabitatApp(theme=theme),
                    BattleApp(theme=theme),
                )
            )
        )

    @property
    def app_ids(self) -> tuple[str, ...]:
        return self._registry.app_ids

    def new_session(self, context: BadgeUiContext) -> BadgeSessionState:
        home = self._registry.get(self._home_app_id)
        return BadgeSessionState(
            active_app=self._home_app_id,
            app_states={self._home_app_id: home.initial_state(context)},
            revision=0,
        )

    def dispatch(
        self,
        session: BadgeSessionState,
        event: ButtonEvent | Button | str,
        context: BadgeUiContext,
    ) -> BadgeSessionState:
        """Apply one button event and return a new, persistable session."""

        if not isinstance(session, BadgeSessionState):
            raise TypeError("session must be a BadgeSessionState; use from_dict for stored JSON.")
        button_event = event if isinstance(event, ButtonEvent) else ButtonEvent.from_raw(event)
        active_app_id = session.active_app
        app_states = {
            app_id: _json_object(app_state, label=f"app state for {app_id!r}")
            for app_id, app_state in session.app_states.items()
        }

        # Home is a framework-level navigation action.  It does not depend on
        # firmware callbacks, and it preserves the old app's state for return.
        app = self._registry.get(active_app_id)
        current = app_states.get(active_app_id)
        if current is None:
            current = _json_object(app.initial_state(context), label=f"initial {active_app_id!r} state")

        if (
            button_event.button is Button.HOME
            and active_app_id != self._home_app_id
            and not app.owns_home_navigation(
                _json_object(current, label=f"current {active_app_id!r} state"), context
            )
        ):
            home_state = app_states.get(self._home_app_id)
            if home_state is None:
                home_state = self._registry.get(self._home_app_id).initial_state(context)
                app_states[self._home_app_id] = _json_object(home_state, label="home app state")
            return BadgeSessionState(
                active_app=self._home_app_id,
                app_states=app_states,
                revision=session.revision + 1,
            )

        update = app.reduce(_json_object(current, label=f"current {active_app_id!r} state"), button_event, context)
        app_states[active_app_id] = _json_object(update.state, label=f"next {active_app_id!r} state")

        target_app_id = update.navigate_to or active_app_id
        target = self._registry.get(target_app_id)
        if target_app_id not in app_states:
            app_states[target_app_id] = _json_object(
                target.initial_state(context), label=f"initial {target_app_id!r} state"
            )
        return BadgeSessionState(
            active_app=target_app_id,
            app_states=app_states,
            revision=session.revision + 1,
        )

    def render(self, session: BadgeSessionState, context: BadgeUiContext) -> Screen:
        """Render the active app from its persisted state and fresh game data."""

        app = self._registry.get(session.active_app)
        state = session.state_for(session.active_app)
        if state is None:
            state = _json_object(app.initial_state(context), label=f"initial {app.app_id!r} state")
        return app.render(state, context)

    def handle(
        self,
        session: BadgeSessionState,
        event: ButtonEvent | Button | str,
        context: BadgeUiContext,
    ) -> tuple[BadgeSessionState, Screen]:
        """Convenience method for the normal input -> state -> screen flow."""

        next_session = self.dispatch(session, event, context)
        return next_session, self.render(next_session, context)


__all__ = [
    "AppRegistry",
    "AppUpdate",
    "BadgeApp",
    "BadgeSessionState",
    "BadgeUi",
    "BadgeUiContext",
    "BattleApp",
    "BattleCombatantView",
    "BattleOpponent",
    "BattlePhase",
    "BattleUiAction",
    "BattleView",
    "Button",
    "ButtonEvent",
    "Clear",
    "DEFAULT_THEME",
    "DexApp",
    "DrawOperation",
    "HabitatApp",
    "HabitatCreature",
    "HomeApp",
    "Image",
    "JsonObject",
    "JsonValue",
    "Leds",
    "PokemonCard",
    "Rect",
    "Screen",
    "Text",
    "Theme",
    "UiStateError",
    "WorldEvent",
]
