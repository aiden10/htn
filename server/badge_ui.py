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
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class Theme:
    background: str = "#08111F"
    surface: str = "#12243A"
    surface_alt: str = "#17314A"
    focus: str = "#F8C94A"
    focus_text: str = "#101827"
    text: str = "#F6F7FB"
    muted: str = "#A5BACD"
    accent: str = "#55D6BE"
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

    def __init__(self, *, theme: Theme = DEFAULT_THEME, destinations: Sequence[str] = ("dex", "habitat")) -> None:
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
        }
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Leds((theme.accent, theme.focus, theme.accent), brightness=40),
            Text(16, 16, "SHUTTERDEX", theme.text, size=26),
            Text(16, 45, "Choose an app", theme.muted, size=14),
        ]
        card_y = 77
        for index, destination in enumerate(self._destinations):
            selected = index == focus
            fill = theme.focus if selected else theme.surface
            title_color = theme.focus_text if selected else theme.text
            sub_color = "#504319" if selected else theme.muted
            title, subtitle = labels.get(destination, (destination.upper(), ""))
            operations.extend(
                (
                    Rect(14, card_y, context.width - 28, 56, fill, radius=9),
                    Text(29, card_y + 10, ("> " if selected else "  ") + title, title_color, size=19),
                    Text(48, card_y + 33, subtitle, sub_color, size=12, max_width=context.width - 70),
                )
            )
            card_y += 66
        return Screen(tuple(operations), scene=self.app_id)


class DexApp(BadgeApp):
    """Pokedex grid/detail scene backed by the current player's Pokemon rows."""

    app_id = "dex"

    def __init__(self, *, theme: Theme = DEFAULT_THEME, page_size: int = 4, columns: int = 2) -> None:
        if page_size <= 0 or columns <= 0 or page_size % columns:
            raise ValueError("page_size must be positive and divisible by columns.")
        self._theme = theme
        self._page_size = page_size
        self._columns = columns

    def initial_state(self, context: BadgeUiContext) -> JsonObject:
        return {"selected": 0, "detail_tab": 0}

    def reduce(self, state: JsonObject, event: ButtonEvent, context: BadgeUiContext) -> AppUpdate:
        count = len(context.pokemon)
        selected = _clamp(_int(state.get("selected")), 0, max(0, count - 1))
        detail_tab = _clamp(_int(state.get("detail_tab")), 0, 1)
        if event.button in (Button.UP, Button.DOWN, Button.LEFT, Button.RIGHT):
            selected = _focus_after_button(selected, count, event.button, columns=self._columns)
        elif event.button is Button.A:
            detail_tab = 1 - detail_tab
        elif event.button is Button.B:
            return AppUpdate({"selected": selected, "detail_tab": detail_tab}, navigate_to="home")
        return AppUpdate({"selected": selected, "detail_tab": detail_tab})

    def render(self, state: JsonObject, context: BadgeUiContext) -> Screen:
        theme = self._theme
        pokemon = context.pokemon
        operations: list[DrawOperation] = [
            Clear(theme.background),
            Leds((theme.accent, theme.accent, theme.focus), brightness=35),
            Text(10, 10, "POKEDEX", theme.text, size=23),
            Text(10, 33, f"{len(pokemon)} CAPTURE{'S' if len(pokemon) != 1 else ''}", theme.muted, size=11),
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
            fill = theme.focus if highlighted else theme.surface_alt
            text_color = theme.focus_text if highlighted else theme.text
            operations.append(Rect(x, y, cell_width, cell_height, fill, radius=7))
            if mon.sprite_url:
                operations.append(Image(x + 17, y + 6, 34, 34, mon.sprite_url))
            else:
                operations.append(Text(x + 26, y + 14, mon.name[:1].upper(), text_color, size=25))
            operations.append(Text(x + 5, y + 43, mon.name, text_color, size=12, max_width=58, scroll=True))
            operations.append(
                Text(x + 5, y + 60, mon.element.upper(), "#57491B" if highlighted else _element_colour(mon.element), size=10, max_width=58, scroll=True)
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
                Text(detail_x, detail_y + 64, "PROFILE" if detail_tab == 0 else "STATS", theme.focus, size=11),
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
            for label, value in stats:
                operations.append(Text(detail_x, stat_y, f"{label.upper()[:3]}  {value}", theme.text, size=13, max_width=detail_width))
                stat_y += 21
        operations.append(Text(14, context.height - 19, f"PAGE {page + 1}/{max(1, (len(pokemon) + self._page_size - 1) // self._page_size)}", theme.muted, size=10))
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
            Leds(("#5BC0EB", "#6BCB77", "#F8C94A"), brightness=34),
            Text(10, 10, "HABITAT", theme.text, size=23),
            Text(
                context.width - 105,
                15,
                "DIRECTING..." if busy else f"WORLD {context.world_revision}",
                theme.focus if busy else theme.muted,
                size=10,
                max_width=96,
                align="right",
            ),
            Text(
                10,
                36,
                "DIRECTING - CONTROLS LOCKED" if busy else "L/R CREATURE   U/D MOMENTS   A ADVANCE",
                theme.focus if busy else theme.muted,
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
                operations.append(Rect(x - 4, y - 4, 48, 48, "#0D1F2B", stroke=theme.focus, stroke_width=2, radius=10))
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
                    Text(17, panel_y + 7, "HABITAT DIRECTOR", theme.focus, size=11, max_width=context.width - 34),
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
        """Supply a harmless static fallback when only Pokedex data is loaded."""

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
        """Build Shutterdex's initial Home, Dex, and Habitat application set."""

        return cls(AppRegistry((HomeApp(theme=theme), DexApp(theme=theme), HabitatApp(theme=theme))))

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
        if button_event.button is Button.HOME and active_app_id != self._home_app_id:
            home_state = app_states.get(self._home_app_id)
            if home_state is None:
                home_state = self._registry.get(self._home_app_id).initial_state(context)
                app_states[self._home_app_id] = _json_object(home_state, label="home app state")
            return BadgeSessionState(
                active_app=self._home_app_id,
                app_states=app_states,
                revision=session.revision + 1,
            )

        app = self._registry.get(active_app_id)
        current = app_states.get(active_app_id)
        if current is None:
            current = _json_object(app.initial_state(context), label=f"initial {active_app_id!r} state")
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
