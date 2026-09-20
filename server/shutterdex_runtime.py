"""Server-side application runtime for Wi-Fi Shutterdex badges.

HTN OS keeps the firmware connection and device credential on the badge.  This
module owns the *application* side of the relationship: it loads an owner's
collection, reduces a button event against that badge's persisted UI session,
and queues a complete draw plan through :mod:`htn_gateway`.

The UI classes themselves are intentionally stateless.  There is one durable
``BadgeSession`` per physical badge, and one short-lived lock per HTN ID so a
button press, a dashboard action, and a Habitat refresh can never overwrite
each other's rendered state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from collections.abc import Awaitable, Callable, Iterable

from badge_renderer import RenderError, ScreenRenderer
from badge_store import (
    Badge,
    BadgeSession,
    BadgeStore,
    ConflictError,
    NotFoundError,
    PokemonRecord,
)
from badge_ui import (
    BadgeSessionState,
    BadgeUi,
    BadgeUiContext,
    Button,
    ButtonEvent,
    HabitatCreature,
    PokemonCard,
    Rect,
    Screen,
    Text,
    UiStateError,
    WorldEvent,
)
from credential_vault import CredentialError, CredentialVault
from htn_gateway import (
    BadgeCommand,
    BadgeCredentials,
    BadgeEvent,
    CommandTicket,
    GatewayError,
    HTNBadgeGateway,
)
from sprite_assets import SpriteError, SpriteStore


LOGGER = logging.getLogger(__name__)


class ShutterdexRuntimeError(RuntimeError):
    """A user-facing Shutterdex Wi-Fi runtime failure."""


class BadgeUnassignedError(ShutterdexRuntimeError):
    """A paired badge cannot render until it has an owning player."""


@dataclass(frozen=True, slots=True)
class RenderDispatch:
    """A persisted frame that has been accepted by the outbound badge queue."""

    badge_id: str
    htn_id: str
    scene: str
    session_revision: int
    queued_commands: int
    render_hash: str


class ShutterdexRuntime:
    """Coordinates persisted UI sessions, collections, and badge commands.

    One instance belongs to one FastAPI process.  Keep it in a single process
    while the connection manager is in memory; running multiple workers would
    create competing outbound app WebSockets for the same HTN ID.
    """

    # Only the small message panel is redrawn for the marquee (normally three
    # commands). A two-second cadence limits traffic on slow Wi-Fi while the
    # larger jump still moves the text four times faster than the original.
    HABITAT_SCROLL_INTERVAL_SECONDS = 2.0
    HABITAT_SCROLL_CHARACTERS_PER_TICK = 8
    # Writer and Jev calls are serial, but a badge should never remain on a
    # permanent "working" screen if a provider connection stalls.
    HABITAT_ADVANCE_TIMEOUT_SECONDS = 45.0

    def __init__(
        self,
        *,
        store: BadgeStore,
        gateway: HTNBadgeGateway,
        vault: CredentialVault,
        ui: BadgeUi,
        renderer: ScreenRenderer,
        sprites: SpriteStore,
        on_habitat_advance: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.vault = vault
        self.ui = ui
        self.renderer = renderer
        self.sprites = sprites
        self._on_habitat_advance = on_habitat_advance
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsubscribe: Callable[[], None] | None = None
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._habitat_scroll_task: asyncio.Task[None] | None = None
        self._habitat_panel_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._habitat_advance_tasks: dict[str, asyncio.Task[None]] = {}
        self._habitat_scroll_step = 0
        self._started = False

    async def start(self) -> None:
        """Subscribe to gateway events and restore paired app connections.

        A bad/corrupt application key should not keep the capture server from
        starting.  That one badge is logged safely and can be repaired by
        re-pairing; every other badge still gets its own connection attempt.
        """

        if self._started:
            return
        self._unsubscribe = self.gateway.add_event_handler(self._on_gateway_event)
        self._started = True
        self._habitat_scroll_task = asyncio.create_task(
            self._scroll_habitat_labels(), name="shutterdex-habitat-scroll"
        )
        for badge in self.store.list_badges():
            try:
                await self.ensure_registered(badge.htn_id)
            except (CredentialError, GatewayError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not restore Shutterdex badge %s: %s", badge.htn_id, exc)

    async def close(self) -> None:
        """Stop background delivery observers and gateway sessions."""

        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._started = False
        if self._habitat_scroll_task is not None:
            self._habitat_scroll_task.cancel()
            await asyncio.gather(self._habitat_scroll_task, return_exceptions=True)
            self._habitat_scroll_task = None
        for task in tuple(self._habitat_advance_tasks.values()):
            task.cancel()
        if self._habitat_advance_tasks:
            await asyncio.gather(*self._habitat_advance_tasks.values(), return_exceptions=True)
        self._habitat_advance_tasks.clear()
        for task in tuple(self._delivery_tasks):
            task.cancel()
        if self._delivery_tasks:
            await asyncio.gather(*self._delivery_tasks, return_exceptions=True)
        self._delivery_tasks.clear()
        self._habitat_panel_delivery_tasks.clear()
        await self.gateway.close()

    async def pair_badge(self, *, player_id: str, htn_id: str, app_key: str) -> Badge:
        """Pair or rotate a player's app key without ever returning it.

        This endpoint is an administrative/pairing surface; production should
        put its normal user authentication in front of it.  It deliberately
        refuses to silently move a claimed badge from one player to another.
        """

        async with self._lock_for(htn_id):
            self.store.require_player(player_id)
            ciphertext = self.vault.seal(app_key)
            existing = self.store.get_badge_by_htn_id(htn_id)
            if existing is None:
                badge = self.store.create_badge(
                    htn_id, ciphertext, player_id=player_id
                )
            else:
                if existing.player_id not in (None, player_id):
                    raise ConflictError("This HTN badge is already paired to another player.")
                if existing.player_id is None:
                    existing = self.store.assign_badge(existing.badge_id, player_id)
                badge = self.store.replace_badge_credential(
                    existing.badge_id, ciphertext
                )
                # Credentials are immutable for a registered gateway session.
                await self.gateway.unregister_badge(badge.htn_id)

            context = self._context_for_badge(badge)
            initial = self.ui.new_session(context)
            self.store.get_or_create_session(
                badge.badge_id,
                active_app=initial.active_app,
                app_state=initial.app_states,
            )
            await self.ensure_registered(badge.htn_id)
            return self.store.require_badge(badge.badge_id)

    async def ensure_registered(self, htn_id: str) -> Badge:
        """Open the outgoing HTN app connection for one paired badge."""

        badge = self.store.require_badge_by_htn_id(htn_id)
        if badge.htn_id in self.gateway.registered_badge_ids:
            return badge
        app_key = self.vault.open(badge.app_key_ciphertext)
        await self.gateway.register_badge(BadgeCredentials(badge.htn_id, app_key))
        return badge

    async def launch(self, htn_id: str, *, scroll_step: int = 0) -> RenderDispatch:
        """Render the persisted app state and enter HTN OS Canvas mode."""

        async with self._lock_for(htn_id):
            badge = await self.ensure_registered(htn_id)
            return await self._render_current_locked(
                badge, force=True, scroll_step=scroll_step
            )

    async def reset_to_home(self, htn_id: str) -> None:
        """Reset only this badge's UI state before a configured boot launch."""

        async with self._lock_for(htn_id):
            badge = await self.ensure_registered(htn_id)
            context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, context)
            fresh = self.ui.new_session(context)
            self.store.save_session(
                badge.badge_id,
                active_app=fresh.active_app,
                app_state=fresh.app_states,
                canvas_active=False,
                last_render_hash=None,
                expected_revision=stored.revision,
            )

    async def handle_button(
        self,
        htn_id: str,
        button: str,
        *,
        repeat: bool = False,
    ) -> RenderDispatch | None:
        """Reduce one user input against exactly that badge's session."""

        async with self._lock_for(htn_id):
            badge = await self.ensure_registered(htn_id)
            context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, context)
            session = self._session_state(stored, context)
            button_event = ButtonEvent.from_raw(button, repeat=repeat)
            running = self._habitat_advance_tasks.get(htn_id)
            if (
                stored.active_app == "habitat"
                and running is not None
                and not running.done()
            ):
                # A Director turn is automatic. Lock every in-app control
                # until it completes so neither navigation nor Back can be
                # mistaken for a required confirmation or queue extra frames.
                return None
            if (
                stored.active_app == "habitat"
                and button_event.button is Button.A
                and badge.player_id is not None
                and self._on_habitat_advance is not None
            ):
                # Backboard may take several seconds. Persist and queue a
                # visible acknowledgement first, then let the tick run outside
                # this badge lock so marquee updates and other button events
                # do not freeze behind the network call.
                if button_event.repeat:
                    return None
                pending_session = self._with_habitat_feedback(
                    session,
                    busy=True,
                    notice="",
                    event_count=len(context.events),
                )
                pending_screen = self.ui.render(pending_session, context)
                dispatch = await self._persist_and_enqueue_locked(
                    badge,
                    stored,
                    pending_session,
                    screen=pending_screen,
                    force=True,
                    scroll_step=self._habitat_scroll_step,
                )
                task = asyncio.create_task(
                    self._advance_habitat_in_background(htn_id, badge.player_id),
                    name=f"shutterdex-habitat-advance-{htn_id}",
                )
                self._habitat_advance_tasks[htn_id] = task
                task.add_done_callback(
                    lambda finished: self._clear_habitat_advance_task(htn_id, finished)
                )
                return dispatch
            next_session, screen = self.ui.handle(session, button_event, context)
            return await self._persist_and_enqueue_locked(
                badge,
                stored,
                next_session,
                screen=screen,
            )

    async def _advance_habitat_in_background(self, htn_id: str, player_id: str) -> None:
        """Run the slow model work without blocking UI or marquee redraws."""

        succeeded = False
        try:
            callback = self._on_habitat_advance
            if callback is None:
                return
            await asyncio.wait_for(
                callback(player_id), timeout=self.HABITAT_ADVANCE_TIMEOUT_SECONDS
            )
            succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The exact provider/model error is useful only in server logs;
            # retain a safe retry message for the person using the badge.
            LOGGER.warning(
                "Habitat advance failed for badge %s (%s)", htn_id, type(exc).__name__
            )

        try:
            async with self._lock_for(htn_id):
                badge = self.store.get_badge_by_htn_id(htn_id)
                if badge is None:
                    return
                context = self._context_for_badge(badge)
                stored = self._ensure_session(badge, context)
                session = self._session_state(stored, context)
                next_session = self._with_habitat_feedback(
                    session,
                    busy=False,
                    notice=(
                        "Could not advance the Habitat. Try A again."
                        if not succeeded
                        else ""
                    ),
                    event_count=len(context.events),
                    show_latest_event=succeeded,
                )
                if stored.active_app != "habitat" or not stored.canvas_active:
                    self.store.save_session(
                        badge.badge_id,
                        active_app=next_session.active_app,
                        app_state=next_session.app_states,
                        canvas_active=stored.canvas_active,
                        last_render_hash=stored.last_render_hash,
                        expected_revision=stored.revision,
                    )
                    return
                screen = self.ui.render(next_session, context)
                await self._persist_and_enqueue_locked(
                    badge,
                    stored,
                    next_session,
                    screen=screen,
                    force=True,
                    scroll_step=self._habitat_scroll_step,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "Could not update Habitat feedback for badge %s (%s)",
                htn_id,
                type(exc).__name__,
            )

    @staticmethod
    def _with_habitat_feedback(
        session: BadgeSessionState,
        *,
        busy: bool,
        notice: str,
        event_count: int,
        show_latest_event: bool = False,
    ) -> BadgeSessionState:
        """Return one session with transient per-badge Habitat feedback."""

        app_states = {
            app_id: dict(state)
            for app_id, state in session.app_states.items()
        }
        habitat_state = dict(app_states.get("habitat", {}))
        habitat_state["busy"] = busy
        habitat_state["notice"] = notice
        if busy:
            habitat_state["panel"] = "status"
        elif show_latest_event:
            habitat_state["panel"] = "event"
            habitat_state["event_index"] = max(0, event_count - 1)
        app_states["habitat"] = habitat_state
        return BadgeSessionState(
            active_app=session.active_app,
            app_states=app_states,
            revision=session.revision + 1,
        )

    def _clear_habitat_advance_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._habitat_advance_tasks.get(htn_id) is completed:
            self._habitat_advance_tasks.pop(htn_id, None)

    def _clear_habitat_panel_delivery_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._habitat_panel_delivery_tasks.get(htn_id) is completed:
            self._habitat_panel_delivery_tasks.pop(htn_id, None)

    async def refresh_badge(
        self,
        htn_id: str,
        *,
        only_active_app: str | None = None,
        scroll_step: int = 0,
    ) -> RenderDispatch | None:
        """Refresh fresh collection/world data without changing selection."""

        async with self._lock_for(htn_id):
            badge = await self.ensure_registered(htn_id)
            stored = self._ensure_session(badge, self._context_for_badge(badge))
            if only_active_app is not None and stored.active_app != only_active_app:
                return None
            if not stored.canvas_active:
                return None
            return await self._render_current_locked(
                badge, force=False, scroll_step=scroll_step
            )

    async def refresh_player(
        self,
        player_id: str,
        *,
        only_active_app: str | None = None,
    ) -> tuple[RenderDispatch, ...]:
        """Queue a new screen on every live Canvas badge owned by a player."""

        self.store.require_player(player_id)
        rendered: list[RenderDispatch] = []
        for badge in self.store.list_badges_for_player(player_id):
            try:
                result = await self.refresh_badge(
                    badge.htn_id, only_active_app=only_active_app
                )
            except (CredentialError, GatewayError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not refresh Shutterdex badge %s: %s", badge.htn_id, exc)
                continue
            if result is not None:
                rendered.append(result)
        return tuple(rendered)

    async def _scroll_habitat_labels(self) -> None:
        """Redraw active Habitat labels at a safe low rate for marquee text."""

        try:
            while True:
                await asyncio.sleep(self.HABITAT_SCROLL_INTERVAL_SECONDS)
                self._habitat_scroll_step += self.HABITAT_SCROLL_CHARACTERS_PER_TICK
                for badge in self.store.list_badges():
                    session = self.store.get_session(badge.badge_id)
                    if (
                        session is None
                        or session.active_app != "habitat"
                        or not session.canvas_active
                    ):
                        continue
                    try:
                        await self._redraw_habitat_panel(
                            badge.htn_id, self._habitat_scroll_step
                        )
                    except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError):
                        # A reconnect or later scroll frame can redraw it; do
                        # not repeatedly log expected offline failures.
                        continue
        except asyncio.CancelledError:
            raise

    async def _redraw_habitat_panel(self, htn_id: str, scroll_step: int) -> None:
        """Update only Habitat's message panel for marquee text.

        Repainting the entire 320x240 scene for one scrolling sentence causes
        visible flicker and wastes the badge's command budget. The panel's
        background rectangle clears just its own old glyphs, then the latest
        heading/body text is drawn in place.
        """

        existing_delivery = self._habitat_panel_delivery_tasks.get(htn_id)
        if existing_delivery is not None:
            if not existing_delivery.done():
                # Do not let a slow connection accumulate stale marquee frames
                # ahead of a button-triggered screen in the FIFO badge queue.
                return
            self._habitat_panel_delivery_tasks.pop(htn_id, None)

        async with self._lock_for(htn_id):
            badge = await self.ensure_registered(htn_id)
            context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, context)
            if stored.active_app != "habitat" or not stored.canvas_active:
                return
            session = self._session_state(stored, context)
            screen = self.ui.render(session, context)
            panel_top = context.height - 58
            panel_operations = tuple(
                operation
                for operation in screen.operations
                if (
                    isinstance(operation, Rect)
                    and operation.y == panel_top
                )
                or (
                    isinstance(operation, Text)
                    and operation.y >= panel_top
                )
            )
            commands = self.renderer.render(
                Screen(panel_operations, scene="habitat-panel"),
                scroll_step=scroll_step,
            )
            tickets = await self._enqueue_frame(badge.htn_id, commands)
            delivery = self._watch_delivery(badge.htn_id, tickets)
            if delivery is not None:
                self._habitat_panel_delivery_tasks[htn_id] = delivery
                delivery.add_done_callback(
                    lambda completed: self._clear_habitat_panel_delivery_task(
                        htn_id, completed
                    )
                )

    async def _on_gateway_event(self, event: BadgeEvent) -> None:
        """Interpret official HTN input/status events without trusting payloads."""

        # Unknown/unpaired IDs are already filtered by HTNBadgeGateway.  A
        # removed database record can still race a queued event, so keep the
        # handler defensive and avoid leaking user payloads in logs.
        try:
            badge = self.store.require_badge_by_htn_id(event.badge_id)
        except (NotFoundError, ValueError):
            return

        if event.event_type == "button":
            pressed = event.payload.get("pressed", True)
            if pressed is not True:
                return
            raw_button = event.payload.get("button")
            if not isinstance(raw_button, str):
                return
            repeat = bool(event.payload.get("repeat", False))
            try:
                await self.handle_button(badge.htn_id, raw_button, repeat=repeat)
            except (ValueError, UiStateError, RenderError, GatewayError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not apply button event for badge %s: %s", badge.htn_id, exc)
            return

        if event.event_type in {"connected", "online", "transport_reconnected"}:
            self.store.mark_badge_seen(badge.badge_id, online=True)
            if event.event_type == "transport_reconnected":
                try:
                    await self.refresh_badge(badge.htn_id)
                except (GatewayError, ShutterdexRuntimeError, RenderError) as exc:
                    LOGGER.warning("Could not replay screen for badge %s: %s", badge.htn_id, exc)
            return

        if event.event_type in {"disconnected", "offline", "transport_error"}:
            self.store.mark_badge_seen(badge.badge_id, online=False)
            return

        if event.event_type == "mode":
            mode = event.payload.get("mode")
            if not isinstance(mode, str):
                return
            async with self._lock_for(badge.htn_id):
                context = self._context_for_badge(badge)
                stored = self._ensure_session(badge, context)
                is_canvas = mode.lower() == "canvas"
                self.store.save_session(
                    badge.badge_id,
                    active_app=stored.active_app,
                    app_state=stored.app_state,
                    canvas_active=is_canvas,
                    last_render_hash=stored.last_render_hash,
                    expected_revision=stored.revision,
                )
            return

        # NFC is purposefully left as an event for a future trade coordinator.
        # The public HTN OS API reads UID/NDEF; it does not turn the badge into
        # an NFC writer or card emulator, so no trade is committed here.

    async def _render_current_locked(
        self,
        badge: Badge,
        *,
        force: bool,
        scroll_step: int,
    ) -> RenderDispatch:
        context = self._context_for_badge(badge)
        stored = self._ensure_session(badge, context)
        session = self._session_state(stored, context)
        screen = self.ui.render(session, context)
        return await self._persist_and_enqueue_locked(
            badge,
            stored,
            session,
            screen=screen,
            force=force,
            scroll_step=scroll_step,
        )

    async def _persist_and_enqueue_locked(
        self,
        badge: Badge,
        stored: BadgeSession,
        session: BadgeSessionState,
        *,
        screen,
        force: bool = False,
        scroll_step: int = 0,
    ) -> RenderDispatch:
        """Persist session state before queuing its ordered render commands.

        Persistence-first means a reconnect can reconstruct the intended
        screen even if the network drops halfway through this particular frame.
        """

        commands = self.renderer.render(screen, scroll_step=scroll_step)
        render_hash = self.renderer.fingerprint(screen, scroll_step=scroll_step)
        needs_draw = force or not stored.canvas_active or stored.last_render_hash != render_hash
        saved = self.store.save_session(
            badge.badge_id,
            active_app=session.active_app,
            app_state=session.app_states,
            canvas_active=True,
            last_render_hash=render_hash,
            expected_revision=stored.revision,
        )
        if needs_draw:
            tickets = await self._enqueue_frame(badge.htn_id, commands)
            self._watch_delivery(badge.htn_id, tickets)
        else:
            tickets = ()
        return RenderDispatch(
            badge_id=badge.badge_id,
            htn_id=badge.htn_id,
            scene=screen.scene,
            session_revision=saved.revision,
            queued_commands=len(tickets),
            render_hash=render_hash,
        )

    async def _enqueue_frame(
        self, htn_id: str, commands: Iterable[BadgeCommand]
    ) -> tuple[CommandTicket, ...]:
        tickets: list[CommandTicket] = []
        try:
            for command in commands:
                tickets.append(await self.gateway.enqueue(htn_id, command))
        except Exception:
            # The store still has the full desired state, so a later reconnect
            # can draw it.  Do not conceal a queue-full/not-registered error.
            raise
        return tuple(tickets)

    def _watch_delivery(
        self, htn_id: str, tickets: Iterable[CommandTicket]
    ) -> asyncio.Task[None] | None:
        tickets = tuple(tickets)
        if not tickets:
            return None

        async def watch() -> None:
            try:
                for ticket in tickets:
                    await ticket.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The gateway's reconnect loop will restore the endpoint.  Do
                # not log commands/payloads, which may contain image data.
                LOGGER.warning("A Shutterdex frame could not reach badge %s: %s", htn_id, exc)
                try:
                    badge = self.store.get_badge_by_htn_id(htn_id)
                    if badge is not None:
                        self.store.mark_badge_seen(badge.badge_id, online=False)
                except Exception:
                    LOGGER.exception("Could not update delivery state for badge %s", htn_id)

        task = asyncio.create_task(watch(), name=f"shutterdex-frame-{htn_id}")
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)
        return task

    def _context_for_badge(self, badge: Badge) -> BadgeUiContext:
        if badge.player_id is None:
            raise BadgeUnassignedError("This HTN badge has not been assigned to a player.")
        pokemon = self.store.list_pokemon_for_player(badge.player_id)
        states = {
            state.pokemon_id: state
            for state in self.store.list_world_states_for_player(badge.player_id)
        }
        cards = tuple(self._card_for(pokemon_record) for pokemon_record in pokemon)
        creatures: list[HabitatCreature] = []
        for index, pokemon_record in enumerate(pokemon):
            state = states.get(pokemon_record.pokemon_id)
            if state is None:
                # Stable fallback positions make a new capture visible before
                # its first director tick writes explicit movement state.
                x = 16 + (index * 37) % 72
                y = 22 + (index * 53) % 68
                mood, energy, activity = "curious", 75, "exploring"
            else:
                x, y = state.x, state.y
                mood, energy, activity = state.mood, state.energy, state.activity
            del energy  # UI currently shows activity/mood; retain it in SQLite for the director.
            creatures.append(
                HabitatCreature(
                    pokemon_id=pokemon_record.pokemon_id,
                    name=pokemon_record.name,
                    x=x,
                    y=y,
                    mood=mood,
                    activity=activity,
                    sprite_url=self._sprite_source(pokemon_record.sprite_path),
                    element=pokemon_record.types[0],
                )
            )
        events = self.store.list_simulation_events_for_player(badge.player_id, limit=20)
        names = {record.pokemon_id: record.name for record in pokemon}
        visible_events: list[WorldEvent] = []
        for event in events:
            dialogue = " / ".join(
                f"{names.get(line.speaker_pokemon_id, 'Pokemon')}: {line.text}"
                for line in event.dialogue
            )
            visible_events.append(
                WorldEvent(
                    event_id=event.event_id,
                    summary=event.summary,
                    actor_name=names.get(event.actor_pokemon_id, "WORLD MOMENT"),
                    dialogue=dialogue,
                )
            )
        revision = max((event.revision for event in events), default=0)
        return BadgeUiContext(
            badge_id=badge.badge_id,
            player_id=badge.player_id,
            pokemon=cards,
            creatures=tuple(creatures),
            events=tuple(visible_events),
            world_revision=revision,
        )

    def _card_for(self, pokemon: PokemonRecord) -> PokemonCard:
        return PokemonCard(
            pokemon_id=pokemon.pokemon_id,
            name=pokemon.name,
            species=pokemon.species,
            element=" / ".join(pokemon.types),
            flavour=pokemon.flavour,
            sprite_url=self._sprite_source(pokemon.sprite_path),
            rarity=pokemon.rarity,
            stats=pokemon.stats,
            caught_at=pokemon.caught_at.date().isoformat(),
        )

    def _sprite_source(self, sprite_key: str | None) -> str | None:
        if not sprite_key:
            return None
        try:
            if self.sprites.source_path(sprite_key).is_file():
                return f"sprite://{sprite_key}"
        except SpriteError:
            pass
        return None

    def _ensure_session(self, badge: Badge, context: BadgeUiContext) -> BadgeSession:
        session = self.store.get_or_create_session(badge.badge_id)
        try:
            self._session_state(session, context)
        except UiStateError:
            # Old/development app state should never make the player lose their
            # collection.  Reset only the display state to a fresh Home screen.
            fresh = self.ui.new_session(context)
            session = self.store.save_session(
                badge.badge_id,
                active_app=fresh.active_app,
                app_state=fresh.app_states,
                canvas_active=False,
                last_render_hash=None,
                expected_revision=session.revision,
            )
        return session

    def _session_state(
        self, stored: BadgeSession, context: BadgeUiContext
    ) -> BadgeSessionState:
        del context  # Kept in the signature to document its initialization relationship.
        return BadgeSessionState.from_dict(
            {
                "active_app": stored.active_app,
                "app_states": stored.app_state,
                "revision": stored.revision,
            }
        )

    def _lock_for(self, htn_id: str) -> asyncio.Lock:
        # HTN IDs are public opaque names, so do not use them as filesystem
        # paths or SQL.  BadgeStore validates them before persistence.
        lock = self._locks.get(htn_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[htn_id] = lock
        return lock


__all__ = [
    "BadgeUnassignedError",
    "RenderDispatch",
    "ShutterdexRuntime",
    "ShutterdexRuntimeError",
]
