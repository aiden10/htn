"""Offline regression coverage for the HTN OS Shutterdex runtime.

These tests deliberately use the in-memory gateway.  They exercise the same
event and command path as a Wi-Fi badge without storing a real app key or
opening a network connection.
"""

from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest


# Make ``python -m unittest discover -s tests`` work from ``server/`` as well
# as invoking this test file directly from another working directory.
SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_renderer import ScreenRenderer
from badge_store import BadgeStore, pokemon_from_dict
from badge_ui import BadgeUi, Clear, Image, Leds, Rect, Screen, Text
from htn_gateway import HTNBadgeGateway, InMemoryBadgeTransport
from shutterdex_runtime import ShutterdexRuntime
from sprite_assets import SpriteStore


class MemoryVault:
    """Small reversible vault fake that never writes an app key to SQLite."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def seal(self, app_key: str) -> str:
        token = "test-vault-" + sha256(app_key.encode("utf-8")).hexdigest()
        self._values[token] = app_key
        return token

    def open(self, ciphertext: str) -> str:
        return self._values[ciphertext]


class StaticImageResolver:
    """A renderer image source with no filesystem fixture."""

    def load_png(self, source: str, width: int, height: int) -> bytes:
        if source.startswith("habitat://"):
            if (width, height) != (304, 124):
                raise AssertionError(f"Unexpected Habitat size: {(width, height)}")
            return b"static-habitat-background"
        if source != "sprite://diagnostic":
            raise AssertionError(f"Unexpected image source: {source}")
        if (width, height) != (12, 10):
            raise AssertionError(f"Unexpected image size: {(width, height)}")
        return b"not-a-real-png-but-the-renderer-only-base64-encodes-it"


def _pokemon(
    *,
    pokemon_id: str,
    owner_player_id: str,
    captured_by_badge_id: str,
    name: str,
    element: str,
):
    return pokemon_from_dict(
        {
            "pokemon_id": pokemon_id,
            "name": name,
            "species": "test creature",
            "type": element,
            "stats": {"hp": 55, "attack": 61, "defense": 49, "speed": 72},
            "moves": ["test_move"],
            "flavour": "Exists only to verify badge isolation.",
            "sprite_prompt": "small test creature",
            "rarity": "common",
        },
        owner_player_id=owner_player_id,
        captured_by_badge_id=captured_by_badge_id,
    )


class ScreenRendererTests(unittest.TestCase):
    def test_renderer_generates_safe_ordered_commands(self) -> None:
        renderer = ScreenRenderer(StaticImageResolver())
        screen = Screen(
            (
                Clear("#010203"),
                Rect(4, 5, 30, 20, "#112233", stroke="#ffffff", stroke_width=2),
                Text(9, 12, "Mönster long label", "#abcdef", size=12, max_width=48, scroll=True),
                Image(18, 19, 12, 10, "sprite://diagnostic"),
                Leds(("#ff0000", "#00ff00"), brightness=42),
            ),
            scene="diagnostic",
        )

        commands = renderer.render(screen, scroll_step=3)

        self.assertEqual(
            [command.name for command in commands],
            ["clear", "rect", "rect", "text", "image", "leds"],
        )
        self.assertEqual(commands[0].payload, {"color": "#010203"})
        self.assertEqual(commands[1].payload["color"], "#ffffff")
        self.assertEqual(commands[2].payload["color"], "#112233")
        self.assertTrue(commands[3].payload["text"].isascii())
        self.assertLessEqual(len(commands[3].payload["text"]), 8)
        self.assertEqual(
            base64.b64decode(commands[4].payload["image"]),
            b"not-a-real-png-but-the-renderer-only-base64-encodes-it",
        )
        self.assertEqual(commands[4].payload["fit"], "none")
        self.assertEqual(
            commands[5].payload["leds"],
            ["#ff0000", "#00ff00", "#ff0000", "#00ff00", "#ff0000", "#00ff00"],
        )
        self.assertEqual(renderer.fingerprint(screen), renderer.fingerprint(screen))


class WifiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.store = BadgeStore(root / "shutterdex.sqlite3")
        self.sprites = SpriteStore(root / "sprites")
        self.transport = InMemoryBadgeTransport()
        # A deliberately high rate keeps this pure in-memory unit test fast;
        # production continues to use the HTN service's 20-command/sec cap.
        self.gateway = HTNBadgeGateway(self.transport, commands_per_second=1_000_000_000.0)
        self.habitat_advance_calls: list[str] = []

        async def advance_habitat(player_id: str) -> None:
            self.habitat_advance_calls.append(player_id)

        self.runtime = ShutterdexRuntime(
            store=self.store,
            gateway=self.gateway,
            vault=MemoryVault(),
            ui=BadgeUi.standard(),
            renderer=ScreenRenderer(StaticImageResolver()),
            sprites=self.sprites,
            on_habitat_advance=advance_habitat,
        )
        await self.runtime.start()

    async def asyncTearDown(self) -> None:
        # Let every queued render receipt settle before closing the in-memory
        # gateway.  This mirrors a normal graceful server shutdown and keeps
        # the test runner from reporting intentionally-cancelled ticket
        # futures as unobserved exceptions.
        await self._wait_until(lambda: not self.runtime._delivery_tasks, timeout=2.0)
        await self.runtime.close()
        self.store.close()
        self._temporary_directory.cleanup()

    async def _wait_until(self, predicate, *, timeout: float = 1.0) -> None:
        # All fakes in this file are non-blocking.  Yielding instead of sleeping
        # lets the command/event worker run immediately and keeps the suite
        # deterministic on slower Windows timers. ``timeout`` is retained for
        # an informative call site and bounded by the fixed iteration count.
        del timeout
        for _ in range(10_000):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("Timed out waiting for the in-memory badge gateway")

    async def test_sessions_and_collections_are_isolated_per_badge(self) -> None:
        player_a = self.store.create_player("Player A", player_id="player_a")
        player_b = self.store.create_player("Player B", player_id="player_b")
        badge_a = await self.runtime.pair_badge(
            player_id=player_a.player_id, htn_id="a1b2c", app_key="key-a"
        )
        badge_b = await self.runtime.pair_badge(
            player_id=player_b.player_id, htn_id="d3e4f", app_key="key-b"
        )
        self.store.create_pokemon(
            _pokemon(
                pokemon_id="mon_aster",
                owner_player_id=player_a.player_id,
                captured_by_badge_id=badge_a.badge_id,
                name="Aster",
                element="electric",
            )
        )
        self.store.create_pokemon(
            _pokemon(
                pokemon_id="mon_boulder",
                owner_player_id=player_b.player_id,
                captured_by_badge_id=badge_b.badge_id,
                name="Boulder",
                element="earth",
            )
        )

        self.assertEqual(
            [record.name for record in self.store.list_pokemon_for_player(player_a.player_id)],
            ["Aster"],
        )
        self.assertEqual(
            [record.name for record in self.store.list_pokemon_for_player(player_b.player_id)],
            ["Boulder"],
        )
        self.assertEqual(self.transport.connected_badges, {"a1b2c", "d3e4f"})

        await self.runtime.launch(badge_a.htn_id)
        await asyncio.sleep(0)
        await self.runtime.launch(badge_b.htn_id)
        await asyncio.sleep(0)
        dex_dispatch = await self.runtime.handle_button(badge_a.htn_id, "a")
        await asyncio.sleep(0)
        await self.runtime.handle_button(badge_b.htn_id, "down")
        await asyncio.sleep(0)

        session_a = self.store.get_session(badge_a.badge_id)
        session_b = self.store.get_session(badge_b.badge_id)
        assert session_a is not None and session_b is not None
        self.assertEqual(session_a.active_app, "dex")
        self.assertEqual(session_b.active_app, "home")
        self.assertEqual(session_b.app_state["home"]["focus"], 1)
        self.assertGreater(dex_dispatch.queued_commands, 0)
        # This is the runtime's context-builder boundary: it may only load the
        # records owned by the badge's assigned player before it renders Dex.
        context_a = self.runtime._context_for_badge(badge_a)
        self.assertEqual([card.name for card in context_a.pokemon], ["Aster"])
        self.assertNotIn("Boulder", [card.name for card in context_a.pokemon])

        # The Dex description is a partial marquee redraw: it advances the
        # flavour text without repainting the collection grid or its sprite.
        await self._wait_until(lambda: not self.runtime._delivery_tasks)
        command_count = len(self.transport.commands)
        await self.runtime._redraw_dex_description(badge_a.htn_id, scroll_step=8)
        await self._wait_until(
            lambda: any(
                entry.command.name == "text"
                and entry.command.payload.get("x") == 171
                and entry.command.payload.get("y") == 186
                for entry in self.transport.commands[command_count:]
            )
        )
        marquee_commands = [
            entry.command for entry in self.transport.commands[command_count:]
        ]
        self.assertTrue(
            any(
                command.name == "rect"
                and command.payload.get("x") == 169
                and command.payload.get("y") == 184
                for command in marquee_commands
            )
        )
        description_command = next(
            command
            for command in marquee_commands
            if command.name == "text"
            and command.payload.get("x") == 171
            and command.payload.get("y") == 186
        )
        self.assertNotEqual(
            description_command.payload["text"],
            "Exists only to verify badge isolation.",
        )

    async def test_gateway_button_event_only_changes_its_origin_badge(self) -> None:
        player_a = self.store.create_player("Player A", player_id="player_a")
        player_b = self.store.create_player("Player B", player_id="player_b")
        badge_a = await self.runtime.pair_badge(
            player_id=player_a.player_id, htn_id="a1b2c", app_key="key-a"
        )
        badge_b = await self.runtime.pair_badge(
            player_id=player_b.player_id, htn_id="d3e4f", app_key="key-b"
        )
        await self.runtime.launch(badge_a.htn_id)
        await self.runtime.launch(badge_b.htn_id)

        # Releases are deliberately ignored; only the following press should
        # navigate badge A from Home into its Dex.
        await self.transport.emit(badge_a.htn_id, "button", {"button": "a", "pressed": False})
        await asyncio.sleep(0.02)
        await self.transport.emit(badge_a.htn_id, "button", {"button": "a", "pressed": True})

        await self._wait_until(
            lambda: (
                (session := self.store.get_session(badge_a.badge_id)) is not None
                and session.active_app == "dex"
            )
        )
        session_b = self.store.get_session(badge_b.badge_id)
        assert session_b is not None
        self.assertEqual(session_b.active_app, "home")

    async def test_capture_loading_locks_only_the_pokeball_badge(self) -> None:
        """A slow image job may never freeze another player's controller."""

        player_a = self.store.create_player("Poké Ball Player", player_id="player_a")
        player_b = self.store.create_player("Other Player", player_id="player_b")
        capture_badge = await self.runtime.pair_badge(
            player_id=player_a.player_id, htn_id="a1b2c", app_key="key-a"
        )
        other_badge = await self.runtime.pair_badge(
            player_id=player_b.player_id, htn_id="d3e4f", app_key="key-b"
        )
        await self.runtime.launch(capture_badge.htn_id)
        await self.runtime.launch(other_badge.htn_id)

        await self.runtime.begin_capture_loading(capture_badge.htn_id)
        self.assertIn(capture_badge.htn_id, self.runtime._capture_loading_badges)
        # Every button on the paired capture badge is a no-op while its image
        # job owns the screen.
        self.assertIsNone(await self.runtime.handle_button(capture_badge.htn_id, "a"))
        self.assertIsNone(await self.runtime.handle_button(capture_badge.htn_id, "right"))

        # The independent badge remains live and can enter its Dex normally.
        other_dispatch = await self.runtime.handle_button(other_badge.htn_id, "a")
        self.assertIsNotNone(other_dispatch)
        other_session = self.store.get_session(other_badge.badge_id)
        assert other_session is not None
        self.assertEqual(other_session.active_app, "dex")

        await self.runtime.end_capture_loading(capture_badge.htn_id, restore=True)
        self.assertNotIn(capture_badge.htn_id, self.runtime._capture_loading_badges)
        resumed_dispatch = await self.runtime.handle_button(capture_badge.htn_id, "a")
        self.assertIsNotNone(resumed_dispatch)

    async def test_habitat_a_advances_only_that_badges_world(self) -> None:
        player = self.store.create_player("Player A", player_id="player_a")
        badge = await self.runtime.pair_badge(
            player_id=player.player_id, htn_id="a1b2c", app_key="key-a"
        )
        self.store.create_pokemon(
            _pokemon(
                pokemon_id="mon_aster",
                owner_player_id=player.player_id,
                captured_by_badge_id=badge.badge_id,
                name="Aster",
                element="electric",
            )
        )
        self.store.create_pokemon(
            _pokemon(
                pokemon_id="mon_bramble",
                owner_player_id=player.player_id,
                captured_by_badge_id=badge.badge_id,
                name="Bramble",
                element="earth",
            )
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_advance(player_id: str) -> None:
            self.habitat_advance_calls.append(player_id)
            started.set()
            await release.wait()

        self.runtime._on_habitat_advance = delayed_advance
        await self.runtime.launch(badge.htn_id)
        await self.runtime.handle_button(badge.htn_id, "down")
        await self.runtime.handle_button(badge.htn_id, "a")

        session = self.store.get_session(badge.badge_id)
        assert session is not None
        self.assertEqual(session.active_app, "habitat")

        await self.runtime.handle_button(badge.htn_id, "right")
        selected = self.store.get_session(badge.badge_id)
        assert selected is not None
        self.assertEqual(selected.app_state["habitat"]["selected"], 1)
        self.assertEqual(selected.app_state["habitat"]["panel"], "creature")

        pending = await self.runtime.handle_button(badge.htn_id, "a")
        self.assertGreater(pending.queued_commands, 0)
        session = self.store.get_session(badge.badge_id)
        assert session is not None
        self.assertTrue(session.app_state["habitat"]["busy"])
        await started.wait()

        # The Jev turn is automatic. Every in-app control is a true
        # no-op until it completes: no duplicate transaction, navigation, or
        # redundant frame.
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "a"))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "a", repeat=True))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "left"))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "right"))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "up"))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "down"))
        self.assertIsNone(await self.runtime.handle_button(badge.htn_id, "b"))
        self.assertEqual(self.habitat_advance_calls, [player.player_id])
        locked = self.store.get_session(badge.badge_id)
        assert locked is not None
        self.assertEqual(locked.active_app, "habitat")
        self.assertEqual(locked.app_state["habitat"]["selected"], 1)

        release.set()
        await self._wait_until(
            lambda: (
                (saved := self.store.get_session(badge.badge_id)) is not None
                and saved.app_state["habitat"]["busy"] is False
            )
        )
        saved = self.store.get_session(badge.badge_id)
        assert saved is not None
        self.assertEqual(saved.app_state["habitat"]["background_index"], 1)


if __name__ == "__main__":
    unittest.main()
