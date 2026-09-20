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
from dataclasses import dataclass, replace
import logging
from collections.abc import Awaitable, Callable, Iterable

from badge_renderer import RenderError, ScreenRenderer
from badge_store import (
    Badge,
    BattleRecord,
    BattleRosterSnapshot,
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
    BattleApp,
    BattleCombatantView,
    BattleOpponent,
    BattleView,
    Button,
    ButtonEvent,
    Clear,
    DexApp,
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


@dataclass(slots=True)
class CaptureOffer:
    """One generated creature waiting for a player badge to claim it."""

    capture_id: str
    name: str
    species: str
    element: str
    rarity: str
    recipient_htn_ids: set[str]
    result: asyncio.Future[str | None]
    declined_htn_ids: set[str]


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
    CAPTURE_LOADING_INTERVAL_SECONDS = 1.25
    # Writer and Jev calls are serial, but a badge should never remain on a
    # permanent "working" screen if a provider connection stalls.
    HABITAT_ADVANCE_TIMEOUT_SECONDS = 45.0
    # Battle Writer + Jev work happens outside the badge lock.  This deadline
    # is an extra guard around the service's own provider handling, so a
    # transient SDK/network stall cannot leave one badge locally input-locked.
    BATTLE_ACTION_TIMEOUT_SECONDS = 60.0
    BATTLE_EXPIRY_INTERVAL_SECONDS = 2.0

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
        on_battle_action: Callable[[str, str | None, str, int | None], Awaitable[object]] | None = None,
        on_battle_discovery_select: Callable[[str, str, str], Awaitable[BattleRecord | None]] | None = None,
        on_battle_disconnect: Callable[[str], Awaitable[object]] | None = None,
        on_battle_reconnect: Callable[[str], Awaitable[object]] | None = None,
        on_battle_expire: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.vault = vault
        self.ui = ui
        self.renderer = renderer
        self.sprites = sprites
        self._on_habitat_advance = on_habitat_advance
        self._on_battle_action = on_battle_action
        self._on_battle_discovery_select = on_battle_discovery_select
        self._on_battle_disconnect = on_battle_disconnect
        self._on_battle_reconnect = on_battle_reconnect
        self._on_battle_expire = on_battle_expire
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsubscribe: Callable[[], None] | None = None
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._latest_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._habitat_scroll_task: asyncio.Task[None] | None = None
        self._habitat_panel_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._battle_narrative_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._habitat_advance_tasks: dict[str, asyncio.Task[None]] = {}
        self._battle_action_tasks: dict[str, asyncio.Task[None]] = {}
        # A move starts with Writer work before its candidates can be durably
        # saved. Keep this short-lived process-local marker so both selected
        # participant badges lock immediately rather than exposing a tiny
        # second-input window before SQLite switches to ``resolving``.
        self._battle_preparing_ids: set[str] = set()
        self._battle_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._battle_lobby_refresh_task: asyncio.Task[None] | None = None
        self._battle_expiry_task: asyncio.Task[None] | None = None
        self._capture_loading_badges: set[str] = set()
        self._capture_loading_tasks: dict[str, asyncio.Task[None]] = {}
        self._capture_offer: CaptureOffer | None = None
        # The renderer takes a numeric marquee offset, but a global offset
        # made a newly selected creature inherit another screen's position.
        # Keep one lightweight origin per physical badge instead.
        self._scroll_origins: dict[str, int] = {}
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
        if self._on_battle_expire is not None:
            self._battle_expiry_task = asyncio.create_task(
                self._expire_battles_in_background(), name="shutterdex-battle-expiry"
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
        if self._battle_expiry_task is not None:
            self._battle_expiry_task.cancel()
            await asyncio.gather(self._battle_expiry_task, return_exceptions=True)
            self._battle_expiry_task = None
        for task in tuple(self._habitat_advance_tasks.values()):
            task.cancel()
        if self._habitat_advance_tasks:
            await asyncio.gather(*self._habitat_advance_tasks.values(), return_exceptions=True)
        self._habitat_advance_tasks.clear()
        for task in tuple(self._battle_action_tasks.values()):
            task.cancel()
        if self._battle_action_tasks:
            await asyncio.gather(*self._battle_action_tasks.values(), return_exceptions=True)
        self._battle_action_tasks.clear()
        self._battle_preparing_ids.clear()
        for task in tuple(self._battle_refresh_tasks.values()):
            task.cancel()
        if self._battle_refresh_tasks:
            await asyncio.gather(*self._battle_refresh_tasks.values(), return_exceptions=True)
        self._battle_refresh_tasks.clear()
        if self._battle_lobby_refresh_task is not None:
            self._battle_lobby_refresh_task.cancel()
            await asyncio.gather(self._battle_lobby_refresh_task, return_exceptions=True)
            self._battle_lobby_refresh_task = None
        for task in tuple(self._capture_loading_tasks.values()):
            task.cancel()
        if self._capture_loading_tasks:
            await asyncio.gather(*self._capture_loading_tasks.values(), return_exceptions=True)
        self._capture_loading_tasks.clear()
        self._capture_loading_badges.clear()
        self._scroll_origins.clear()
        if self._capture_offer is not None and not self._capture_offer.result.done():
            self._capture_offer.result.set_result(None)
        self._capture_offer = None
        for task in tuple(self._delivery_tasks):
            task.cancel()
        if self._delivery_tasks:
            await asyncio.gather(*self._delivery_tasks, return_exceptions=True)
        self._delivery_tasks.clear()
        self._latest_delivery_tasks.clear()
        self._habitat_panel_delivery_tasks.clear()
        self._battle_narrative_delivery_tasks.clear()
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
            if context.battle is not None and not context.battle.is_terminal:
                # A server restart/configured reconnect must not silently
                # strand one participant at Home while a durable shared battle
                # still expects their turn. Recreate only the lightweight
                # local move focus and keep the authoritative battle visible.
                battle_app = self.ui._registry.get("battle")
                if not isinstance(battle_app, BattleApp):
                    raise ShutterdexRuntimeError("Battle UI is not registered.")
                fresh = BadgeSessionState(
                    active_app="battle",
                    app_states={
                        **fresh.app_states,
                        "battle": battle_app.initial_state(context),
                    },
                    revision=fresh.revision,
                )
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
            if htn_id in self._capture_loading_badges:
                # A physical Poké Ball capture owns this badge's screen until
                # its image has been processed and persisted. Other badges
                # remain completely independent and keep accepting events.
                return None
            offer = self._capture_offer
            if offer is not None and htn_id in offer.recipient_htn_ids:
                # This modal owns every input until it is claimed or declined.
                # ``set_result`` has no await point, so two simultaneous A
                # presses still resolve deterministically: the event handler
                # that reaches this line first wins.
                button_event = ButtonEvent.from_raw(button, repeat=repeat)
                if button_event.repeat:
                    return None
                if button_event.button is Button.A and not offer.result.done():
                    offer.result.set_result(htn_id)
                elif button_event.button is Button.B:
                    offer.declined_htn_ids.add(htn_id)
                    offer.recipient_htn_ids.discard(htn_id)
                    if not offer.recipient_htn_ids and not offer.result.done():
                        offer.result.set_result(None)
                    # A pass is final for this offer. Restore that badge's
                    # saved app immediately while other players still choose.
                    context = self._context_for_badge(badge)
                    stored = self._ensure_session(badge, context)
                    session = self._session_state(stored, context)
                    return await self._persist_and_enqueue_locked(
                        badge,
                        stored,
                        session,
                        screen=self.ui.render(session, context),
                        force=True,
                        scroll_step=self._habitat_scroll_step,
                    )
                return None
            context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, context)
            session = self._session_state(stored, context)
            button_event = ButtonEvent.from_raw(button, repeat=repeat)
            if stored.active_app == "dex":
                dex_app = self.ui._registry.get("dex")
                dex_state = session.state_for("dex")
                if (
                    isinstance(dex_app, DexApp)
                    and dex_state is not None
                    and bool(dex_state.get("release_confirm", False))
                    and button_event.button is Button.A
                    and not button_event.repeat
                ):
                    return await self._release_dex_selection_locked(
                        badge, stored, session, context, dex_state
                    )
            running = self._habitat_advance_tasks.get(htn_id)
            if (
                stored.active_app == "habitat"
                and running is not None
                and not running.done()
            ):
                # A Jev turn is automatic. Lock every in-app control
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

            # Battle lifecycle is shared state, while the four-move focus is
            # local state.  Let the stateless UI identify a valid intent, then
            # run the authoritative command outside this badge lock.  Every
            # button is ignored while a command is in flight: otherwise a
            # second A/Home/D-pad event can look like a separate move while a
            # Writer or Jev call is still deciding the first one.
            if stored.active_app == "battle":
                battle_task = self._battle_action_tasks.get(htn_id)
                if battle_task is not None and not battle_task.done():
                    return None
                if (
                    context.battle is not None
                    and context.battle.battle_id in self._battle_preparing_ids
                ):
                    return None
                if context.battle is not None and context.battle.input_locked:
                    return None
                battle_app = self.ui._registry.get("battle")
                if isinstance(battle_app, BattleApp):
                    battle_state = session.state_for("battle") or battle_app.initial_state(context)
                    action = battle_app.requested_action(battle_state, button_event, context)
                    if action is not None and action != "leave_terminal":
                        if action == "select_opponent" and context.battle is None:
                            opponent_htn_id = battle_app.selected_opponent_htn_id(
                                battle_state, context
                            )
                            if opponent_htn_id is None:
                                return None
                            callback = self._on_battle_discovery_select
                            if callback is None:
                                return None
                            pending_session = self._with_battle_discovery_selection(
                                session, opponent_htn_id, busy=True
                            )
                            pending_screen = self.ui.render(pending_session, context)
                            dispatch = await self._persist_battle_frame_locked(
                                badge,
                                stored,
                                pending_session,
                                screen=pending_screen,
                                force=True,
                                scroll_step=self._habitat_scroll_step,
                            )
                            task = asyncio.create_task(
                                self._run_battle_discovery_selection(
                                    htn_id=htn_id,
                                    badge_id=badge.badge_id,
                                    player_id=badge.player_id or "",
                                    opponent_htn_id=opponent_htn_id,
                                ),
                                name=f"shutterdex-battle-select-{htn_id}",
                            )
                            self._battle_action_tasks[htn_id] = task
                            task.add_done_callback(
                                lambda finished: self._clear_battle_action_task(htn_id, finished)
                            )
                            return dispatch
                        if action == "select_opponent":
                            return None
                        callback = self._on_battle_action
                        if callback is None:
                            return None
                        move_index = (
                            battle_app.selected_move_index(battle_state, context)
                            if action == "select_move"
                            else None
                        )
                        if action == "select_move" and context.battle is not None:
                            self._battle_preparing_ids.add(context.battle.battle_id)
                        pending_session = self._with_battle_feedback(session, busy=True)
                        pending_screen = self.ui.render(pending_session, context)
                        dispatch = await self._persist_battle_frame_locked(
                            badge,
                            stored,
                            pending_session,
                            screen=pending_screen,
                            force=True,
                            scroll_step=self._habitat_scroll_step,
                        )
                        task = asyncio.create_task(
                            self._run_battle_action(
                                htn_id=htn_id,
                                player_id=badge.player_id or "",
                                battle_id=context.battle.battle_id if context.battle else None,
                                action=action,
                                move_index=move_index,
                            ),
                            name=f"shutterdex-battle-{action}-{htn_id}",
                        )
                        self._battle_action_tasks[htn_id] = task
                        task.add_done_callback(
                            lambda finished: self._clear_battle_action_task(htn_id, finished)
                        )
                        return dispatch
            next_session, screen = self.ui.handle(session, button_event, context)
            dispatch = await self._persist_and_enqueue_locked(
                badge,
                stored,
                next_session,
                screen=screen,
            )
            if next_session.active_app == "battle" and context.battle is None:
                # A badge has just entered the lobby. Refresh any other live
                # lobby screens so its HTN ID appears without either person
                # having to leave and reopen Battle.
                self._schedule_battle_lobby_refresh()
            return dispatch

    async def _release_dex_selection_locked(
        self,
        badge: Badge,
        stored: BadgeSession,
        session: BadgeSessionState,
        context: BadgeUiContext,
        dex_state: dict[str, object],
    ) -> RenderDispatch:
        """Release the confirmed owned Dex creature while this badge is locked."""

        if badge.player_id is None or not context.pokemon:
            raise BadgeUnassignedError("Only an assigned player can release a creature.")
        selected = min(
            max(0, int(dex_state.get("selected", 0))), len(context.pokemon) - 1
        )
        pokemon_id = context.pokemon[selected].pokemon_id
        released = None
        notice = ""
        try:
            released = await asyncio.to_thread(
                self.store.release_pokemon,
                pokemon_id,
                expected_owner_player_id=badge.player_id,
            )
            if released.sprite_path:
                try:
                    await asyncio.to_thread(self.sprites.delete, released.sprite_path)
                except (OSError, SpriteError) as exc:
                    # Database ownership is already final. An orphaned cached
                    # image is harmless and can never be rendered again.
                    LOGGER.warning("Could not remove released sprite (%s)", type(exc).__name__)
            notice = f"{released.name} was released."
        except ConflictError:
            notice = "Release unavailable during an active battle."
        except (NotFoundError, ValueError):
            notice = "That creature is no longer available."

        fresh_context = self._context_for_badge(badge)
        next_dex_state = dict(dex_state)
        next_dex_state["selected"] = min(
            selected, max(0, len(fresh_context.pokemon) - 1)
        )
        next_dex_state["release_confirm"] = False
        next_dex_state["release_notice"] = notice
        next_session = BadgeSessionState(
            active_app=session.active_app,
            app_states={**session.app_states, "dex": next_dex_state},
            revision=session.revision + 1,
        )
        screen = self.ui.render(next_session, fresh_context)
        dispatch = await self._persist_and_enqueue_locked(
            badge, stored, next_session, screen=screen, force=True
        )
        if released is not None:
            task = asyncio.create_task(
                self._refresh_player_after_release(badge.player_id, except_htn_id=badge.htn_id),
                name=f"shutterdex-release-refresh-{badge.htn_id}",
            )
            self._delivery_tasks.add(task)
            task.add_done_callback(self._delivery_tasks.discard)
        return dispatch

    async def _refresh_player_after_release(self, player_id: str, *, except_htn_id: str) -> None:
        """Refresh sibling player badges without recursively re-locking this one."""

        for sibling in self.store.list_badges_for_player(player_id):
            if sibling.htn_id == except_htn_id:
                continue
            try:
                await self.refresh_badge(sibling.htn_id)
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not refresh badge after release: %s", exc)

    async def _run_battle_discovery_selection(
        self,
        *,
        htn_id: str,
        badge_id: str,
        player_id: str,
        opponent_htn_id: str,
    ) -> None:
        """Persist a reciprocal lobby choice and create a battle when matched.

        This is intentionally separate from ordinary Battle actions because
        there is not a battle row yet.  The callback re-validates both badges'
        persisted selections before it snapshots either roster.
        """

        try:
            callback = self._on_battle_discovery_select
            if callback is None:
                return
            battle = await asyncio.wait_for(
                callback(player_id, badge_id, opponent_htn_id),
                timeout=self.BATTLE_ACTION_TIMEOUT_SECONDS,
            )
            if isinstance(battle, BattleRecord):
                await self.present_battle(battle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "Battle lobby selection failed for badge %s (%s)",
                htn_id,
                type(exc).__name__,
            )
        finally:
            try:
                async with self._lock_for(htn_id):
                    badge = self.store.get_badge_by_htn_id(htn_id)
                    if badge is None:
                        return
                    context = self._context_for_badge(badge)
                    stored = self._ensure_session(badge, context)
                    session = self._session_state(stored, context)
                    next_session = self._with_battle_feedback(session, busy=False)
                    if (
                        htn_id in self._capture_loading_badges
                        or not stored.canvas_active
                        or stored.active_app != "battle"
                    ):
                        self.store.save_session(
                            badge.badge_id,
                            active_app=next_session.active_app,
                            app_state=next_session.app_states,
                            canvas_active=stored.canvas_active,
                            last_render_hash=stored.last_render_hash,
                            expected_revision=stored.revision,
                        )
                    else:
                        screen = self.ui.render(next_session, context)
                        await self._persist_battle_frame_locked(
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
                    "Could not clear Battle lobby feedback for badge %s (%s)",
                    htn_id,
                    type(exc).__name__,
                )
            self._schedule_battle_lobby_refresh()

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
                    advance_background=succeeded,
                )
                if (
                    htn_id in self._capture_loading_badges
                    or stored.active_app != "habitat"
                    or not stored.canvas_active
                ):
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

    async def _run_battle_action(
        self,
        *,
        htn_id: str,
        player_id: str,
        battle_id: str | None,
        action: str,
        move_index: int | None,
    ) -> None:
        """Run one shared-battle command and always release local feedback.

        ``BadgeStore`` remains the race authority: the local task map merely
        prevents one physical badge from queuing a confusing duplicate press
        before the database transition has reached ``resolving``.
        """

        succeeded = False
        result: object | None = None
        preparing = action == "select_move" and battle_id is not None
        cancelled = False
        try:
            if preparing:
                # This task starts just after the initiating button handler
                # releases its per-badge lock. Both participants can now see
                # a server-owned, input-locked preparation frame while the
                # Writer generates the three persisted candidate outcomes.
                await self._present_preparing_battle(battle_id)
            callback = self._on_battle_action
            if callback is None:
                return
            result = await asyncio.wait_for(
                callback(player_id, battle_id, action, move_index),
                timeout=self.BATTLE_ACTION_TIMEOUT_SECONDS,
            )
            succeeded = True
            if isinstance(result, BattleRecord):
                if preparing:
                    self._battle_preparing_ids.discard(result.battle_id)
                await self.present_battle(result)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            LOGGER.warning(
                "Battle action %s failed for badge %s (%s)",
                action,
                htn_id,
                type(exc).__name__,
            )
        finally:
            if preparing and battle_id is not None:
                self._battle_preparing_ids.discard(battle_id)
                # A Writer/Director failure normally makes the service abort
                # back to ``active``. Repaint that newest durable state for
                # *both* badges so the opponent is not left watching the
                # short-lived preparation overlay. Do not create work while
                # process shutdown is cancelling this task.
                if not cancelled and not isinstance(result, BattleRecord):
                    try:
                        latest = self.store.get_battle(battle_id)
                        if latest is not None:
                            await self.present_battle(latest)
                    except Exception as exc:
                        LOGGER.warning(
                            "Could not clear Battle preparation for %s (%s)",
                            battle_id,
                            type(exc).__name__,
                        )

        try:
            async with self._lock_for(htn_id):
                badge = self.store.get_badge_by_htn_id(htn_id)
                if badge is None:
                    return
                context = self._context_for_badge(badge)
                stored = self._ensure_session(badge, context)
                session = self._session_state(stored, context)
                next_session = self._with_battle_feedback(session, busy=False)
                if (
                    htn_id in self._capture_loading_badges
                    or not stored.canvas_active
                    or stored.active_app != "battle"
                ):
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
                await self._persist_battle_frame_locked(
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
                "Could not update Battle feedback for badge %s (%s)",
                htn_id,
                type(exc).__name__,
            )

        if not succeeded:
            # The shared status is either still active or was restored by the
            # service's abort path.  A later button press is now safe.
            return

    @staticmethod
    def _with_battle_feedback(
        session: BadgeSessionState, *, busy: bool
    ) -> BadgeSessionState:
        """Add a transient local input lock without changing shared state."""

        app_states = {app_id: dict(state) for app_id, state in session.app_states.items()}
        battle_state = dict(app_states.get("battle", {}))
        battle_state["busy"] = busy
        app_states["battle"] = battle_state
        return BadgeSessionState(
            active_app=session.active_app,
            app_states=app_states,
            revision=session.revision + 1,
        )

    @staticmethod
    def _with_battle_discovery_selection(
        session: BadgeSessionState, opponent_htn_id: str, *, busy: bool
    ) -> BadgeSessionState:
        """Store a lobby choice before inspecting the other badge's choice."""

        app_states = {app_id: dict(state) for app_id, state in session.app_states.items()}
        battle_state = dict(app_states.get("battle", {}))
        battle_state["challenge_target"] = opponent_htn_id
        battle_state["busy"] = busy
        app_states["battle"] = battle_state
        return BadgeSessionState(
            active_app=session.active_app,
            app_states=app_states,
            revision=session.revision + 1,
        )

    def _schedule_battle_lobby_refresh(self) -> None:
        """Coalesce lobby redraws caused by badges entering/changing the list."""

        current = self._battle_lobby_refresh_task
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self.refresh_battle_lobbies(), name="shutterdex-battle-lobby-refresh"
        )
        self._battle_lobby_refresh_task = task

        def clear(completed: asyncio.Task[None]) -> None:
            if self._battle_lobby_refresh_task is completed:
                self._battle_lobby_refresh_task = None

        task.add_done_callback(clear)

    async def refresh_battle_lobbies(self) -> tuple[RenderDispatch, ...]:
        """Redraw every live, unpaired Battle lobby with fresh peer IDs."""

        rendered: list[RenderDispatch] = []
        for badge in self.store.list_badges():
            session = self.store.get_session(badge.badge_id)
            if (
                badge.player_id is None
                or session is None
                or not session.canvas_active
                or session.active_app != "battle"
                or self._open_battle_for_badge(badge) is not None
            ):
                continue
            try:
                dispatch = await self.refresh_badge(
                    badge.htn_id, only_active_app="battle"
                )
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError):
                continue
            if dispatch is not None:
                rendered.append(dispatch)
        return tuple(rendered)

    def _clear_battle_action_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._battle_action_tasks.get(htn_id) is completed:
            self._battle_action_tasks.pop(htn_id, None)

    async def _present_preparing_battle(self, battle_id: str) -> None:
        """Render the transient shared Writer-preparation lock to both sides."""

        battle = self.store.get_battle(battle_id)
        if battle is None or battle.status != "active":
            return
        await self.present_battle(battle)

    @staticmethod
    def _with_habitat_feedback(
        session: BadgeSessionState,
        *,
        busy: bool,
        notice: str,
        event_count: int,
        show_latest_event: bool = False,
        advance_background: bool = False,
    ) -> BadgeSessionState:
        """Return one session with transient per-badge Habitat feedback."""

        app_states = {
            app_id: dict(state)
            for app_id, state in session.app_states.items()
        }
        habitat_state = dict(app_states.get("habitat", {}))
        habitat_state["busy"] = busy
        habitat_state["notice"] = notice
        background_index = habitat_state.get("background_index", 0)
        if not isinstance(background_index, int) or isinstance(background_index, bool):
            background_index = 0
        if advance_background:
            # One validated world advance moves this badge to the next static
            # time-of-day image. The modulo is essential: ``+= 1 % count``
            # would never wrap around.
            background_index = (background_index + 1) % 8
        habitat_state["background_index"] = background_index
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

    def _clear_battle_narrative_delivery_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._battle_narrative_delivery_tasks.get(htn_id) is completed:
            self._battle_narrative_delivery_tasks.pop(htn_id, None)

    async def begin_capture_loading(self, htn_id: str) -> None:
        """Lock one badge and animate a physical Poké Ball capture overlay."""

        current = self._capture_loading_tasks.get(htn_id)
        if current is not None and not current.done():
            return
        self._capture_loading_tasks.pop(htn_id, None)
        self._capture_loading_badges.add(htn_id)
        task = asyncio.create_task(
            self._animate_capture_loading(htn_id),
            name=f"shutterdex-capture-loading-{htn_id}",
        )
        self._capture_loading_tasks[htn_id] = task
        task.add_done_callback(
            lambda completed: self._clear_capture_loading_task(htn_id, completed)
        )

    async def end_capture_loading(self, htn_id: str, *, restore: bool) -> None:
        """Unlock a capture badge and optionally redraw its saved app screen."""

        task = self._capture_loading_tasks.pop(htn_id, None)
        self._capture_loading_badges.discard(htn_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not restore:
            return
        try:
            async with self._lock_for(htn_id):
                badge = await self.ensure_registered(htn_id)
                await self._render_current_locked(
                    badge, force=True, scroll_step=self._habitat_scroll_step
                )
        except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
            LOGGER.warning("Could not restore capture badge %s: %s", htn_id, exc)

    async def present_capture_offer(
        self,
        *,
        capture_id: str,
        name: str,
        species: str,
        element: str,
        rarity: str,
    ) -> asyncio.Future[str | None]:
        """Show a generated creature to every currently connected player badge.

        The returned future resolves to the HTN ID of the first badge pressing
        A, or ``None`` if all recipients decline/this runtime closes. The
        generated profile remains server-side until the winner is known, so a
        capture never temporarily belongs to the Poké Ball badge's player.
        """

        if self._capture_offer is not None:
            raise ShutterdexRuntimeError("Another Poké Ball creature is already awaiting a player.")
        recipients = {
            badge.htn_id
            for badge in self.store.list_badges()
            if badge.player_id is not None and badge.htn_id in self.gateway.registered_badge_ids
        }
        result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        offer = CaptureOffer(
            capture_id=capture_id,
            name=name,
            species=species,
            element=element,
            rarity=rarity,
            recipient_htn_ids=recipients,
            result=result,
            declined_htn_ids=set(),
        )
        self._capture_offer = offer
        if not recipients:
            result.set_result(None)
            return result

        delivered: set[str] = set()
        for htn_id in tuple(recipients):
            try:
                await self._render_capture_offer(htn_id, offer)
                delivered.add(htn_id)
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not offer capture to badge %s: %s", htn_id, exc)
        offer.recipient_htn_ids.intersection_update(delivered)
        if not offer.recipient_htn_ids and not result.done():
            result.set_result(None)
        return result

    async def dismiss_capture_offer(self, capture_id: str, *, restore: bool) -> None:
        """Remove an offer after its winner is stored or it expires."""

        offer = self._capture_offer
        if offer is None or offer.capture_id != capture_id:
            return
        self._capture_offer = None
        if not restore:
            return
        for htn_id in tuple(offer.recipient_htn_ids):
            try:
                async with self._lock_for(htn_id):
                    badge = await self.ensure_registered(htn_id)
                    await self._render_current_locked(
                        badge, force=True, scroll_step=self._habitat_scroll_step
                    )
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not restore badge %s after capture offer: %s", htn_id, exc)

    async def _render_capture_offer(self, htn_id: str, offer: CaptureOffer) -> None:
        """Paint the temporary claim modal without changing the saved app."""

        async with self._lock_for(htn_id):
            if self._capture_offer is not offer or htn_id not in offer.recipient_htn_ids:
                return
            badge = await self.ensure_registered(htn_id)
            commands = self.renderer.render(self._capture_offer_screen(offer))
            tickets = await self._enqueue_frame(badge.htn_id, commands)
            self._watch_delivery(badge.htn_id, tickets)

    @staticmethod
    def _capture_offer_screen(offer: CaptureOffer) -> Screen:
        """A small modal rendered identically on every eligible badge."""

        element = offer.element.upper()[:18] or "MYSTERY"
        return Screen(
            (
                Clear("#241F1B"),
                Rect(16, 20, 288, 200, "#39302A", radius=16),
                Rect(34, 43, 252, 9, "#B3E3A7", radius=5),
                Text(38, 73, "WILD CAPTURE!", "#B3E3A7", size=19, max_width=244),
                Text(38, 105, offer.name, "#FFF8F0", size=24, max_width=244, scroll=True),
                Text(38, 136, offer.species, "#C7B7A7", size=12, max_width=244, scroll=True),
                Text(38, 158, f"{element} - {offer.rarity.upper()}", "#DECBB5", size=11, max_width=244),
                Text(38, 191, "A CLAIM   B PASS", "#FFF8F0", size=13, max_width=244),
            ),
            scene="capture-offer",
        )

    def _clear_capture_loading_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._capture_loading_tasks.get(htn_id) is completed:
            self._capture_loading_tasks.pop(htn_id, None)
        # Rendering a decorative phase can fail independently of the camera
        # pipeline. Do not silently re-enable controls in that case: only the
        # capture job (or shutdown) is allowed to release this badge.

    async def _animate_capture_loading(self, htn_id: str) -> None:
        """Render the three capture phases without blocking any other badge."""

        phase = 0
        try:
            while htn_id in self._capture_loading_badges:
                tickets = await self._queue_capture_loading_frame(htn_id, phase)
                for ticket in tickets:
                    try:
                        await ticket.wait()
                    except GatewayError:
                        # The next phase can retry after a normal badge
                        # reconnect; never let one offline badge stop others.
                        break
                phase = (phase + 1) % 3
                await asyncio.sleep(self.CAPTURE_LOADING_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Capture loading animation failed for badge %s: %s", htn_id, exc)

    async def _queue_capture_loading_frame(
        self, htn_id: str, phase: int
    ) -> tuple[CommandTicket, ...]:
        async with self._lock_for(htn_id):
            if htn_id not in self._capture_loading_badges:
                return ()
            badge = await self.ensure_registered(htn_id)
            stored = self._ensure_session(badge, self._context_for_badge(badge))
            if not stored.canvas_active:
                await self._render_current_locked(
                    badge, force=True, scroll_step=self._habitat_scroll_step
                )
            commands = self.renderer.render(self._capture_loading_screen(phase))
            return await self._enqueue_frame(htn_id, commands)

    @staticmethod
    def _capture_loading_screen(phase: int) -> Screen:
        """A deliberately small animation for the one badge that made a capture."""

        title, detail, accent = (
            (
                "CAPTURE RECEIVED",
                "Storing the Shutterball snapshot...",
                "#B3E3A7",
            ),
            (
                "IDENTIFYING",
                "Finding the creature inside...",
                "#DECBB5",
            ),
            (
                "SUMMONING",
                "Giving your new Pokemon a form...",
                "#B3E3A7",
            ),
        )[phase % 3]
        dot_count = phase % 3 + 1
        return Screen(
            (
                Clear("#241F1B"),
                Rect(18, 25, 284, 190, "#39302A", radius=16),
                Rect(35, 48, 250, 10, accent, radius=5),
                Text(38, 78, "SHUTTERBALL", "#FFF8F0", size=13, max_width=244),
                Text(38, 108, title, accent, size=20, max_width=244),
                Text(38, 142, detail, "#C7B7A7", size=12, max_width=244, scroll=True),
                Text(38, 181, "PLEASE WAIT" + "." * dot_count, "#C7B7A7", size=11, max_width=244),
            ),
            scene="capture-loading",
        )

    async def refresh_badge(
        self,
        htn_id: str,
        *,
        only_active_app: str | None = None,
        scroll_step: int = 0,
    ) -> RenderDispatch | None:
        """Refresh fresh collection/world data without changing selection."""

        async with self._lock_for(htn_id):
            # A capture overlay is intentionally authoritative until the
            # image job completes. Background collection refreshes and
            # reconnect replays must not paint over it.
            if htn_id in self._capture_loading_badges:
                return None
            if self._capture_offer is not None and htn_id in self._capture_offer.recipient_htn_ids:
                # The claim modal is intentionally above the saved app until
                # this player accepts, passes, or another player wins.
                return None
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

    async def present_battle(self, battle: BattleRecord) -> tuple[RenderDispatch, ...]:
        """Put the two chosen participant badges on their authoritative scene.

        A challenge may be created by a dashboard test, a future NFC
        coordinator, or a button action.  It presents the shared state only
        to the concrete badges recorded on the battle. A player can own a
        Poké Ball plus a main badge (or another secondary device); neither
        sibling should be pulled into, control, or pause this match. Legacy
        rows without designated endpoints retain the old owner-wide fallback.
        """

        rendered: list[RenderDispatch] = []
        for owned_badge in self._participant_badges_for_battle(battle):
            try:
                dispatch = await self._present_battle_on_badge(
                    owned_badge, battle.battle_id
                )
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
                LOGGER.warning(
                    "Could not present battle %s on badge %s: %s",
                    battle.battle_id,
                    owned_badge.htn_id,
                    exc,
                )
                continue
            if dispatch is not None:
                rendered.append(dispatch)
        return tuple(rendered)

    def _participant_badges_for_battle(self, battle: BattleRecord) -> tuple[Badge, ...]:
        """Return exactly the badge endpoints named by a battle record.

        Prior development databases can contain snapshot rows that predate
        per-battle badge IDs. Those rows intentionally fall back to a player's
        available badges, but all new challenge rows select one endpoint per
        participant and are therefore isolated from sibling devices.
        """

        selected: list[Badge] = []
        seen: set[str] = set()
        for player_id, badge_id in (
            (battle.challenger_player_id, battle.challenger_badge_id),
            (battle.opponent_player_id, battle.opponent_badge_id),
        ):
            if badge_id is None:
                candidates = self.store.list_badges_for_player(player_id)
            else:
                badge = self.store.get_badge(badge_id)
                candidates = (
                    [badge]
                    if badge is not None and badge.player_id == player_id
                    else []
                )
            for candidate in candidates:
                if candidate.badge_id not in seen:
                    selected.append(candidate)
                    seen.add(candidate.badge_id)
        return tuple(selected)

    @staticmethod
    def _is_participant_badge_for_battle(badge: Badge, battle: BattleRecord) -> bool:
        """Whether this concrete badge is entitled to render/control a match."""

        if badge.player_id == battle.challenger_player_id:
            return (
                battle.challenger_badge_id is None
                or battle.challenger_badge_id == badge.badge_id
            )
        if badge.player_id == battle.opponent_player_id:
            return (
                battle.opponent_badge_id is None
                or battle.opponent_badge_id == badge.badge_id
            )
        return False

    async def _present_battle_on_badge(
        self, candidate: Badge, battle_id: str
    ) -> RenderDispatch | None:
        """Persist a Battle focus and render it when the Canvas is live."""

        async with self._lock_for(candidate.htn_id):
            badge = self.store.require_badge(candidate.badge_id)
            # Terminal outcomes are presented only when this transition
            # explicitly names them. A historical row is not a UI state and
            # must never be rediscovered when somebody opens Battle later.
            initial_context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, initial_context)
            explicit_battle = self.store.get_battle(battle_id)
            if (
                explicit_battle is not None
                and explicit_battle.status in {"finished", "cancelled", "timed_out"}
            ):
                app_states = {
                    app_id: dict(state) for app_id, state in stored.app_state.items()
                }
                battle_state = dict(app_states.get("battle", {}))
                battle_state["terminal_battle_id"] = battle_id
                battle_state["busy"] = False
                app_states["battle"] = battle_state
                stored = self.store.save_session(
                    badge.badge_id,
                    active_app="battle",
                    app_state=app_states,
                    canvas_active=stored.canvas_active,
                    last_render_hash=None,
                    expected_revision=stored.revision,
                )
            context = self._context_for_badge(badge)
            if context.battle is None or context.battle.battle_id != battle_id:
                # A newer battle may have replaced this one while a stale
                # background task was completing. Never overwrite that newer
                # session with an old battle presentation.
                return None
            session = self._session_state(stored, context)
            app_states = {app_id: dict(state) for app_id, state in session.app_states.items()}
            battle_state = app_states.get("battle")
            if battle_state is None:
                battle_app = self.ui._registry.get("battle")
                if not isinstance(battle_app, BattleApp):
                    raise ShutterdexRuntimeError("Battle UI is not registered.")
                battle_state = dict(battle_app.initial_state(context))
            # A reciprocal lobby selection is complete now. The durable
            # battle phase supplies its own lock, so a stale local "busy"
            # flag must not disguise a new challenge as Director resolution.
            battle_state["busy"] = False
            battle_state["challenge_target"] = ""
            if context.battle is not None and not context.battle.is_terminal:
                battle_state["terminal_battle_id"] = ""
            app_states["battle"] = dict(battle_state)
            next_session = BadgeSessionState(
                active_app="battle",
                app_states=app_states,
                revision=session.revision + 1,
            )
            if candidate.htn_id in self._capture_loading_badges or not stored.canvas_active:
                self.store.save_session(
                    badge.badge_id,
                    active_app=next_session.active_app,
                    app_state=next_session.app_states,
                    canvas_active=stored.canvas_active,
                    last_render_hash=stored.last_render_hash,
                    expected_revision=stored.revision,
                )
                return None
            await self.ensure_registered(badge.htn_id)
            screen = self.ui.render(next_session, context)
            return await self._persist_battle_frame_locked(
                badge,
                stored,
                next_session,
                screen=screen,
                force=True,
                scroll_step=self._habitat_scroll_step,
            )

    async def _expire_battles_in_background(self) -> None:
        """Periodically make persisted ready/turn/disconnect deadlines real."""

        try:
            while True:
                await asyncio.sleep(self.BATTLE_EXPIRY_INTERVAL_SECONDS)
                callback = self._on_battle_expire
                if callback is None:
                    continue
                expired = await callback()
                if isinstance(expired, BattleRecord):
                    await self.present_battle(expired)
                elif isinstance(expired, Iterable) and not isinstance(expired, (str, bytes)):
                    for battle in expired:
                        if isinstance(battle, BattleRecord):
                            await self.present_battle(battle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The rows remain durable. A later heartbeat will retry expiry;
            # one broken screen or provider integration must not stop it.
            LOGGER.warning("Battle deadline worker stopped (%s)", type(exc).__name__)

    async def _scroll_habitat_labels(self) -> None:
        """Redraw active Habitat and Dex marquee fields at a safe low rate."""

        try:
            while True:
                await asyncio.sleep(self.HABITAT_SCROLL_INTERVAL_SECONDS)
                self._habitat_scroll_step += self.HABITAT_SCROLL_CHARACTERS_PER_TICK
                for badge in self.store.list_badges():
                    if (
                        self._capture_offer is not None
                        and badge.htn_id in self._capture_offer.recipient_htn_ids
                    ):
                        continue
                    session = self.store.get_session(badge.badge_id)
                    if session is None or not session.canvas_active:
                        continue
                    try:
                        if session.active_app == "habitat":
                            await self._redraw_habitat_panel(
                                badge.htn_id, self._habitat_scroll_step
                            )
                        elif session.active_app == "dex":
                            await self._redraw_dex_description(
                                badge.htn_id, self._habitat_scroll_step
                            )
                        elif session.active_app == "battle":
                            await self._redraw_battle_narrative(
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
            if htn_id in self._capture_loading_badges:
                # The current world may update during a camera job, but a
                # marquee repaint would replace the capture overlay.
                return
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
                scroll_step=self._relative_scroll_step(htn_id),
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

    async def _redraw_dex_description(self, htn_id: str, scroll_step: int) -> None:
        """Advance only the selected Dex entry's description marquee.

        The Dex scene includes sprites and a grid, so repainting the complete
        scene for a single moving sentence is unnecessary visual churn. This
        deliberately clears and redraws only the description region.
        """

        existing_delivery = self._habitat_panel_delivery_tasks.get(htn_id)
        if existing_delivery is not None:
            if not existing_delivery.done():
                return
            self._habitat_panel_delivery_tasks.pop(htn_id, None)

        async with self._lock_for(htn_id):
            if htn_id in self._capture_loading_badges:
                return
            badge = await self.ensure_registered(htn_id)
            context = self._context_for_badge(badge)
            stored = self._ensure_session(badge, context)
            if (
                stored.active_app != "dex"
                or not stored.canvas_active
                or not context.pokemon
            ):
                return
            session = self._session_state(stored, context)
            selected = min(
                max(0, int(session.app_states.get("dex", {}).get("selected", 0))),
                len(context.pokemon) - 1,
            )
            flavour = context.pokemon[selected].flavour
            screen = self.ui.render(session, context)
            description = next(
                (
                    operation
                    for operation in screen.operations
                    if isinstance(operation, Text) and operation.text == flavour
                ),
                None,
            )
            panel = next(
                (
                    operation
                    for operation in screen.operations
                    if isinstance(operation, Rect) and operation.x == 164 and operation.y == 48
                ),
                None,
            )
            if description is None or panel is None:
                return
            # The renderer currently emits one marquee line at a time. Leave
            # enough height for a future multi-line description without
            # touching the Stats/Profile label above it.
            clear_width = (description.max_width or 0) + 4
            operations = (
                Rect(
                    description.x - 2,
                    description.y - 2,
                    clear_width,
                    42,
                    panel.fill,
                ),
                description,
            )
            commands = self.renderer.render(
                Screen(operations, scene="dex-description"),
                scroll_step=self._relative_scroll_step(htn_id),
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

    async def _redraw_battle_narrative(self, htn_id: str, scroll_step: int) -> None:
        """Advance only the Director rationale marquee in an active arena.

        A full battle redraw would resend both sprites and all four move tiles
        every marquee tick. Repaint the narrow narrative panel instead, while
        refusing to queue another frame behind a slow Wi-Fi delivery.
        """

        existing_delivery = self._battle_narrative_delivery_tasks.get(htn_id)
        if existing_delivery is not None:
            if not existing_delivery.done():
                return
            self._battle_narrative_delivery_tasks.pop(htn_id, None)

        async with self._lock_for(htn_id):
            if htn_id in self._capture_loading_badges:
                return
            badge = await self.ensure_registered(htn_id)
            context = self._context_for_badge(badge)
            if context.battle is None or context.battle.normalized_phase not in {
                "active",
                "resolving",
            }:
                return
            stored = self._ensure_session(badge, context)
            if stored.active_app != "battle" or not stored.canvas_active:
                return
            session = self._session_state(stored, context)
            screen = self.ui.render(session, context)
            panel_operations = tuple(
                operation
                for operation in screen.operations
                if (
                    isinstance(operation, Rect)
                    and operation.x == 8
                    and operation.y == 124
                )
                or (
                    isinstance(operation, Text)
                    and operation.x == 17
                    and operation.y in {129, 143}
                )
            )
            if not panel_operations:
                return
            commands = self.renderer.render(
                Screen(panel_operations, scene="battle-narrative"),
                scroll_step=self._relative_scroll_step(htn_id),
            )
            tickets = await self._enqueue_frame(badge.htn_id, commands)
            delivery = self._watch_delivery(badge.htn_id, tickets)
            if delivery is not None:
                self._battle_narrative_delivery_tasks[htn_id] = delivery
                delivery.add_done_callback(
                    lambda completed: self._clear_battle_narrative_delivery_task(
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
            battle = self._open_battle_for_badge(badge)
            if (
                battle is not None
                and battle.status == "disconnected"
                and battle.disconnected_player_id == badge.player_id
                and self._on_battle_reconnect is not None
            ):
                try:
                    restored = await self._on_battle_reconnect(badge.player_id)
                    if isinstance(restored, BattleRecord):
                        await self.present_battle(restored)
                except Exception as exc:
                    LOGGER.warning(
                        "Could not restore Battle connection for badge %s (%s)",
                        badge.htn_id,
                        type(exc).__name__,
                    )
            if event.event_type == "transport_reconnected":
                try:
                    offer = self._capture_offer
                    if offer is not None and badge.htn_id in offer.recipient_htn_ids:
                        await self._render_capture_offer(badge.htn_id, offer)
                    else:
                        await self.refresh_badge(badge.htn_id)
                except (GatewayError, ShutterdexRuntimeError, RenderError) as exc:
                    LOGGER.warning("Could not replay screen for badge %s: %s", badge.htn_id, exc)
            return

        if event.event_type in {"disconnected", "offline", "transport_error"}:
            self.store.mark_badge_seen(badge.badge_id, online=False)
            battle = self._open_battle_for_badge(badge)
            if (
                battle is not None
                and battle.status != "disconnected"
                and self._on_battle_disconnect is not None
            ):
                try:
                    disconnected = await self._on_battle_disconnect(badge.player_id)
                    if isinstance(disconnected, BattleRecord):
                        await self.present_battle(disconnected)
                except Exception as exc:
                    LOGGER.warning(
                        "Could not pause Battle for disconnected badge %s (%s)",
                        badge.htn_id,
                        type(exc).__name__,
                    )
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

    async def _persist_battle_frame_locked(
        self,
        badge: Badge,
        stored: BadgeSession,
        session: BadgeSessionState,
        *,
        screen,
        force: bool = False,
        scroll_step: int = 0,
    ) -> RenderDispatch:
        """Persist the newest Battle frame without queuing stale full scenes.

        Battle transitions can happen in quick succession: a local input lock,
        a durable ``resolving`` state, then a selected outcome. A badge has a
        deliberately bounded command queue, so once one complete frame is in
        flight we persist only the newest desired state and arrange one redraw
        after that frame reaches the device. This is lossless for state and
        prevents a slow Wi-Fi badge from replaying outdated battle screens.

        The caller already owns ``badge.htn_id``'s lock.
        """

        delivery = self._latest_delivery_tasks.get(badge.htn_id)
        queued_refresh = self._battle_refresh_tasks.get(badge.htn_id)
        if (
            (delivery is not None and not delivery.done())
            or (queued_refresh is not None and not queued_refresh.done())
        ):
            render_hash = self.renderer.fingerprint(screen, scroll_step=scroll_step)
            saved = self.store.save_session(
                badge.badge_id,
                active_app=session.active_app,
                app_state=session.app_states,
                canvas_active=True,
                # Deliberately invalidate the old frame fingerprint. The
                # coalesced refresh must paint this newer shared state even if
                # it otherwise has the same visual shape as a prior scene.
                last_render_hash=None,
                expected_revision=stored.revision,
            )
            if queued_refresh is None or queued_refresh.done():
                self._schedule_battle_refresh(badge.htn_id, delivery)
            return RenderDispatch(
                badge_id=badge.badge_id,
                htn_id=badge.htn_id,
                scene=screen.scene,
                session_revision=saved.revision,
                queued_commands=0,
                render_hash=render_hash,
            )
        return await self._persist_and_enqueue_locked(
            badge,
            stored,
            session,
            screen=screen,
            force=force,
            scroll_step=scroll_step,
        )

    def _schedule_battle_refresh(
        self, htn_id: str, delivery: asyncio.Task[None] | None
    ) -> None:
        """Redraw one latest Battle state once the preceding frame is gone."""

        async def refresh_after_delivery() -> None:
            try:
                if delivery is not None:
                    await delivery
                # Let a delivery completion callback update its bookkeeping
                # before we sample the persisted state under the badge lock.
                await asyncio.sleep(0)
                await self.refresh_badge(htn_id, only_active_app="battle")
            except asyncio.CancelledError:
                raise
            except (CredentialError, GatewayError, RenderError, ShutterdexRuntimeError) as exc:
                LOGGER.warning("Could not coalesce Battle frame for badge %s: %s", htn_id, exc)

        task = asyncio.create_task(
            refresh_after_delivery(), name=f"shutterdex-battle-refresh-{htn_id}"
        )
        self._battle_refresh_tasks[htn_id] = task
        task.add_done_callback(
            lambda completed: self._clear_battle_refresh_task(htn_id, completed)
        )

    def _clear_battle_refresh_task(
        self, htn_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._battle_refresh_tasks.get(htn_id) is completed:
            self._battle_refresh_tasks.pop(htn_id, None)

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

        # A complete scene is a new visual context: start every marquee at its
        # first character, then let the small periodic redraws advance it from
        # this badge-specific origin. ``scroll_step`` is intentionally not
        # carried into a new screen.
        self._scroll_origins[badge.htn_id] = self._habitat_scroll_step
        commands = self.renderer.render(screen, scroll_step=0)
        render_hash = self.renderer.fingerprint(screen, scroll_step=0)
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

    def _relative_scroll_step(self, htn_id: str) -> int:
        """Return the marquee distance since this badge's last full screen."""

        origin = self._scroll_origins.get(htn_id, self._habitat_scroll_step)
        return max(0, self._habitat_scroll_step - origin)

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

        # Keep one per-badge reference for Battle's full-frame coalescer. The
        # general set still owns shutdown/cancellation for every scene.
        self._latest_delivery_tasks[htn_id] = task

        def completed(finished: asyncio.Task[None]) -> None:
            self._delivery_tasks.discard(finished)
            if self._latest_delivery_tasks.get(htn_id) is finished:
                self._latest_delivery_tasks.pop(htn_id, None)

        task.add_done_callback(completed)
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
                # its first Jev tick writes explicit movement state.
                x = 16 + (index * 37) % 72
                y = 22 + (index * 53) % 68
                mood, energy, activity = "curious", 75, "exploring"
            else:
                x, y = state.x, state.y
                mood, energy, activity = state.mood, state.energy, state.activity
            del energy  # UI currently shows activity/mood; retain it in SQLite for Jev.
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
            battle=self._battle_view_for_badge(badge),
            battle_opponents=self._battle_opponents_for_badge(badge),
        )

    def _battle_opponents_for_badge(self, badge: Badge) -> tuple[BattleOpponent, ...]:
        """Return other live Battle-lobby endpoints, identified by HTN ID.

        A badge is eligible only while it has a Canvas session actively open
        on Battle and no open shared battle. This is server-side presence, not
        radio proximity: the two people still confirm each other by choosing
        the visible ID on their own badges.
        """

        if badge.player_id is None or self._open_battle_for_badge(badge) is not None:
            return ()
        opponents: list[BattleOpponent] = []
        for candidate in self.store.list_badges():
            if (
                candidate.badge_id == badge.badge_id
                or candidate.player_id is None
                or candidate.player_id == badge.player_id
            ):
                continue
            session = self.store.get_session(candidate.badge_id)
            if (
                session is None
                or not session.canvas_active
                or session.active_app != "battle"
                or self._open_battle_for_badge(candidate) is not None
            ):
                continue
            opponents.append(BattleOpponent(candidate.htn_id))
        # The 320x240 lobby deliberately exposes four whole touch-free rows.
        # Returning the same bound keeps focus from moving to an invisible
        # peer; pagination can be added later without weakening consent.
        return tuple(sorted(opponents, key=lambda opponent: opponent.htn_id.lower())[:4])

    def _open_battle_for_badge(self, badge: Badge) -> BattleRecord | None:
        """Read a nonterminal battle only when this badge is its endpoint."""

        if badge.player_id is None:
            return None
        battle = self.store.get_open_battle_for_player(badge.player_id)
        if battle is None or not self._is_participant_badge_for_battle(badge, battle):
            return None
        return battle

    def _battle_view_for_badge(self, badge: Badge) -> BattleView | None:
        """Map a durable battle record into one participant-relative view."""

        player_id = badge.player_id
        if player_id is None:
            return None
        battle = self._open_battle_for_badge(badge)
        if battle is None:
            # Terminal results are one-shot presentation state. Do *not* look
            # at battle history: an audit record must never decide the next
            # lobby a player sees.
            session = self.store.get_session(badge.badge_id)
            terminal_battle_id = ""
            if session is not None:
                battle_state = session.app_state.get("battle", {})
                if isinstance(battle_state, dict):
                    value = battle_state.get("terminal_battle_id", "")
                    if isinstance(value, str):
                        terminal_battle_id = value.strip()
            if terminal_battle_id:
                candidate = self.store.get_battle(terminal_battle_id)
                if candidate is not None and candidate.status in {
                    "finished",
                    "cancelled",
                    "timed_out",
                }:
                    battle = candidate
        if battle is not None and not self._is_participant_badge_for_battle(badge, battle):
            return None
        if battle is None:
            return None
        if player_id == battle.challenger_player_id:
            viewer_name = battle.challenger_display_name
            viewer_roster = battle.challenger_roster
            viewer_ready = battle.challenger_ready
            opponent_id = battle.opponent_player_id
            opponent_name = battle.opponent_display_name
            opponent_roster = battle.opponent_roster
            opponent_ready = battle.opponent_ready
            role = "challenger"
        elif player_id == battle.opponent_player_id:
            viewer_name = battle.opponent_display_name
            viewer_roster = battle.opponent_roster
            viewer_ready = battle.opponent_ready
            opponent_id = battle.challenger_player_id
            opponent_name = battle.challenger_display_name
            opponent_roster = battle.challenger_roster
            opponent_ready = battle.challenger_ready
            role = "opponent"
        else:
            return None
        view = BattleView(
            battle_id=battle.battle_id,
            phase=battle.status,
            viewer_player_id=player_id,
            viewer_player_name=viewer_name,
            opponent_player_id=opponent_id,
            opponent_player_name=opponent_name,
            viewer_role=role,
            viewer_ready=viewer_ready,
            opponent_ready=opponent_ready,
            active_player_id=battle.current_player_id,
            viewer=self._battle_combatant_view(viewer_roster),
            opponent=self._battle_combatant_view(opponent_roster),
            turn_number=battle.turn_number,
            rationale=battle.last_visible_rationale or "",
            last_action=battle.last_visible_rationale or "",
            notice=battle.notice or "",
            winner_player_id=battle.winner_player_id,
            end_reason=battle.end_reason or "",
            input_locked=battle.status in {"resolving", "disconnected"},
        )
        if (
            battle.battle_id in self._battle_preparing_ids
            and view.normalized_phase == "active"
        ):
            return replace(
                view,
                phase="resolving",
                input_locked=True,
                notice="The Director is preparing this turn.",
            )
        return view

    def _battle_combatant_view(
        self, roster: BattleRosterSnapshot
    ) -> BattleCombatantView:
        snapshot = roster.active_pokemon
        return BattleCombatantView(
            pokemon_id=snapshot.pokemon_id,
            name=snapshot.name,
            element=snapshot.types[0] if snapshot.types else "neutral",
            hp=max(0, snapshot.current_hp),
            max_hp=snapshot.max_hp,
            moves=snapshot.moves,
            sprite_url=self._sprite_source(snapshot.sprite_path),
            roster_index=roster.active_index,
            roster_size=len(roster.pokemon),
            fainted=snapshot.fainted,
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
            moves=pokemon.moves,
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
