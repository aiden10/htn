"""Deterministic two-player coverage for the Shutterdex battle HTTP API.

The test deliberately injects a fixed Writer and Jev decision into
``BattleService``.  It exercises the real API, roster snapshots, SQLite
state transitions, shared badge presentation, and persisted candidate audit
records without sending any traffic to Backboard or requiring physical badges.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi import FastAPI
import httpx


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_renderer import ScreenRenderer, SpriteImageResolver
from badge_store import BadgeStore
from badge_ui import BadgeUi
from battle_service import BattleService, JEV_MODEL
from htn_gateway import HTNBadgeGateway, InMemoryBadgeTransport
from shutterdex_api import router as shutterdex_router
from shutterdex_runtime import ShutterdexRuntime
from sprite_assets import SpriteStore


class MemoryVault:
    """Enough credential-vault behavior for local gateway registration."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    def seal(self, value: str) -> str:
        ciphertext = "test-vault-" + sha256(value.encode("utf-8")).hexdigest()
        self._entries[ciphertext] = value
        return ciphertext

    def open(self, ciphertext: str) -> str:
        return self._entries[ciphertext]


class BattleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.store = BadgeStore(root / "shutterdex.sqlite3")
        self.sprites = SpriteStore(root / "sprites")
        self.transport = InMemoryBadgeTransport()
        self.gateway = HTNBadgeGateway(
            self.transport, commands_per_second=1_000_000_000.0
        )
        self.runtime = ShutterdexRuntime(
            store=self.store,
            gateway=self.gateway,
            vault=MemoryVault(),
            ui=BadgeUi.standard(),
            renderer=ScreenRenderer(SpriteImageResolver(self.sprites)),
            sprites=self.sprites,
        )
        self.now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        self.writer_requests: list[dict[str, object]] = []
        self.director_requests: list[dict[str, object]] = []

        async def fixed_writer(request: dict[str, object]) -> dict[str, object]:
            self.writer_requests.append(dict(request))
            return {
                "outcomes": [
                    {
                        "summary": "A static spark grazes the opponent.",
                        "rationale": "The conductive move finds a gap in the guard.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": -5,
                        "actor_stat": None,
                        "actor_stat_delta": 0,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                    {
                        "summary": "A focused arc lands a clean hit.",
                        "rationale": "Its charge aligns with the foe's exposed mechanism.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": -9,
                        "actor_stat": None,
                        "actor_stat_delta": 0,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                    {
                        "summary": "The burst boosts the user's speed.",
                        "rationale": "A recoil-free pulse lets it move more sharply.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": 0,
                        "actor_stat": "speed",
                        "actor_stat_delta": 1,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                ]
            }

        async def fixed_director(request: dict[str, object]) -> dict[str, object]:
            self.director_requests.append(dict(request))
            return {
                "system_one": {
                    "answers": {"selected_outcome": {"choice": "option_2"}}
                }
            }

        self.battle_service = BattleService(
            self.store,
            writer_call=fixed_writer,
            jev_call=fixed_director,
            ready_timeout_seconds=5,
            turn_timeout_seconds=5,
            disconnect_grace_seconds=5,
            model_timeout_seconds=1,
            clock=lambda: self.now,
        )
        self.app = FastAPI()
        self.app.include_router(shutterdex_router)
        self.app.state.shutterdex_store = self.store
        self.app.state.shutterdex_runtime = self.runtime
        self.app.state.sprites = self.sprites
        self.app.state.battle_service = self.battle_service
        await self.runtime.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://shutterdex.test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._wait_for_deliveries()
        await self.runtime.close()
        self.store.close()
        self._temporary_directory.cleanup()

    async def _wait_for_deliveries(self) -> None:
        for _ in range(10_000):
            if not self.runtime._delivery_tasks:
                return
            await asyncio.sleep(0)
        self.fail("Timed out waiting for in-memory badge draw delivery")

    async def _create_two_players(self) -> tuple[dict[str, str], dict[str, str]]:
        """Create and pair a valid one-Pokemon roster for each test player."""

        players: dict[str, str] = {}
        badges = {"ada": "ada42", "bea": "bea42"}
        pokemon = {
            "ada": {
                "pokemon_id": "mon_voltfin",
                "name": "Voltfin",
                "species": "a lanternfish made of extension cords",
                "type": "electric",
                "stats": {"hp": 40, "attack": 70, "defense": 58, "speed": 76},
                "moves": ["spark_lance", "coil_dash", "charge_bite", "surge_guard"],
                "battle_natures": ["conductive", "fluid"],
                "flavour": "It hums whenever rain is near.",
                "sprite_prompt": "a blue electric fish with a plug tail",
                "rarity": "common",
            },
            "bea": {
                "pokemon_id": "mon_mossbyte",
                "name": "Mossbyte",
                "species": "a moss covered pocket calculator",
                "type": "earth",
                "stats": {"hp": 40, "attack": 52, "defense": 81, "speed": 35},
                "moves": ["root_sum", "stone_cache", "moss_screen", "decimal_drop"],
                "battle_natures": ["rooted", "absorbent"],
                "flavour": "It solves problems at the speed of growing moss.",
                "sprite_prompt": "a mossy green calculator creature",
                "rarity": "common",
            },
        }
        for short_name, display_name in (("ada", "Ada"), ("bea", "Bea")):
            created = await self.client.post(
                "/shutterdex/players", json={"display_name": display_name}
            )
            self.assertEqual(created.status_code, 201, created.text)
            players[short_name] = created.json()["player"]["player_id"]
            paired = await self.client.post(
                "/shutterdex/badges/pair",
                json={
                    "player_id": players[short_name],
                    "htn_id": badges[short_name],
                    "app_key": f"test-key-{short_name}-1234",
                },
            )
            self.assertEqual(paired.status_code, 201, paired.text)
            created_pokemon = await self.client.post(
                f"/shutterdex/players/{players[short_name]}/pokemon",
                json={
                    "captured_by_htn_id": badges[short_name],
                    "pokemon": pokemon[short_name],
                },
            )
            self.assertEqual(created_pokemon.status_code, 201, created_pokemon.text)
        return players, badges

    async def _challenge(self, badges: dict[str, str]) -> dict[str, object]:
        response = await self.client.post(
            "/shutterdex/battles/challenges",
            json={
                "challenger_htn_id": badges["ada"],
                "opponent_htn_id": badges["bea"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["battle"]

    async def test_two_player_handshake_resolves_fixed_writer_and_director_turn(self) -> None:
        players, badges = await self._create_two_players()
        challenge = await self._challenge(badges)
        battle_id = challenge["battle_id"]

        self.assertEqual(challenge["status"], "challenge")
        self.assertEqual(challenge["challenger"]["player_id"], players["ada"])
        self.assertEqual(challenge["opponent"]["player_id"], players["bea"])
        self.assertEqual(len(challenge["challenger"]["roster"]["pokemon"]), 1)
        self.assertEqual(len(challenge["opponent"]["roster"]["pokemon"]), 1)

        # ``present_battle`` puts both paired badges into the shared screen as
        # soon as the durable challenge row exists.
        session = await self.client.get(f"/shutterdex/badges/{badges['ada']}/session")
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json()["session"]["active_app"], "battle")

        opened = await self.client.get(f"/shutterdex/badges/{badges['bea']}/battle")
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["battle"]["battle_id"], battle_id)

        ada_ready = await self.client.post(
            f"/shutterdex/battles/{battle_id}/ready",
            json={"htn_id": badges["ada"], "expected_revision": 0},
        )
        self.assertEqual(ada_ready.status_code, 200, ada_ready.text)
        self.assertEqual(ada_ready.json()["battle"]["status"], "ready")

        bea_ready = await self.client.post(
            f"/shutterdex/battles/{battle_id}/ready",
            json={"htn_id": badges["bea"], "expected_revision": 1},
        )
        self.assertEqual(bea_ready.status_code, 200, bea_ready.text)
        active = bea_ready.json()["battle"]
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["current_player_id"], players["ada"])
        self.assertEqual(active["turn_number"], 1)

        # A non-active badge cannot issue a move even if it knows the battle
        # ID and current revision.
        wrong_turn = await self.client.post(
            f"/shutterdex/battles/{battle_id}/moves",
            json={"htn_id": badges["bea"], "move_index": 0, "expected_revision": 2},
        )
        self.assertEqual(wrong_turn.status_code, 409, wrong_turn.text)

        move = await self.client.post(
            f"/shutterdex/battles/{battle_id}/moves",
            json={"htn_id": badges["ada"], "move_index": 0, "expected_revision": 2},
        )
        self.assertEqual(move.status_code, 200, move.text)
        payload = move.json()
        self.assertEqual(payload["battle"]["status"], "active")
        self.assertEqual(payload["battle"]["current_player_id"], players["bea"])
        self.assertEqual(payload["battle"]["turn_number"], 2)
        self.assertEqual(payload["selected_candidate"]["candidate_id"], "option_2")
        self.assertEqual(payload["turn"]["selected_candidate_id"], "option_2")
        self.assertEqual(len(payload["turn"]["candidate_outcomes"]), 3)
        self.assertEqual(
            payload["battle"]["last_visible_rationale"],
            "Its charge aligns with the foe's exposed mechanism.",
        )
        self.assertEqual(len(self.writer_requests), 1)
        self.assertEqual(len(self.director_requests), 1)
        self.assertEqual(self.writer_requests[0]["model_name"], "gpt-4.1-nano")
        self.assertEqual(self.director_requests[0]["model_name"], JEV_MODEL)

        replay = await self.client.get(
            f"/shutterdex/battles/{battle_id}?htn_id={badges['bea']}"
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(replay.json()["turns"]), 1)
        persisted_turn = replay.json()["turns"][0]
        self.assertEqual(persisted_turn["status"], "resolved")
        self.assertEqual(persisted_turn["selected_candidate_id"], "option_2")
        self.assertEqual(
            persisted_turn["visible_rationale"],
            "Its charge aligns with the foe's exposed mechanism.",
        )

    async def test_disconnect_reconnect_cancel_and_deadline_expiry(self) -> None:
        _, badges = await self._create_two_players()
        challenge = await self._challenge(badges)
        battle_id = challenge["battle_id"]

        disconnected = await self.client.post(
            f"/shutterdex/battles/{battle_id}/disconnect",
            json={"htn_id": badges["bea"], "expected_revision": 0},
        )
        self.assertEqual(disconnected.status_code, 200, disconnected.text)
        self.assertEqual(disconnected.json()["battle"]["status"], "disconnected")

        reconnected = await self.client.post(
            f"/shutterdex/battles/{battle_id}/reconnect",
            json={"htn_id": badges["bea"], "expected_revision": 1},
        )
        self.assertEqual(reconnected.status_code, 200, reconnected.text)
        self.assertEqual(reconnected.json()["battle"]["status"], "challenge")

        cancelled = await self.client.post(
            f"/shutterdex/battles/{battle_id}/cancel",
            json={
                "htn_id": badges["ada"],
                "reason": "test_cancel",
                "expected_revision": 2,
            },
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["battle"]["status"], "cancelled")
        self.assertEqual(cancelled.json()["battle"]["end_reason"], "test_cancel")

        expired_challenge = await self._challenge(badges)
        self.now += timedelta(seconds=6)
        expiry = await self.client.post("/shutterdex/battles/expire")
        self.assertEqual(expiry.status_code, 200, expiry.text)
        self.assertEqual(len(expiry.json()["expired"]), 1)
        expired = expiry.json()["expired"][0]
        self.assertEqual(expired["battle_id"], expired_challenge["battle_id"])
        self.assertEqual(expired["status"], "timed_out")
        self.assertEqual(expired["end_reason"], "ready_timeout")

    async def test_battle_stays_on_the_two_selected_badges(self) -> None:
        """A sibling Poké Ball may not view, control, or pause a main-badge battle."""

        players, badges = await self._create_two_players()
        secondary = await self.client.post(
            "/shutterdex/badges/pair",
            json={
                "player_id": players["ada"],
                "htn_id": "ada-ball",
                "app_key": "test-key-ada-ball-1234",
            },
        )
        self.assertEqual(secondary.status_code, 201, secondary.text)

        challenge = await self._challenge(badges)
        battle_id = challenge["battle_id"]
        sibling_session = await self.client.get("/shutterdex/badges/ada-ball/session")
        self.assertEqual(sibling_session.status_code, 200, sibling_session.text)
        self.assertEqual(sibling_session.json()["session"]["active_app"], "home")

        hidden = await self.client.get("/shutterdex/badges/ada-ball/battle")
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertIsNone(hidden.json()["battle"])

        forbidden = await self.client.post(
            f"/shutterdex/battles/{battle_id}/ready",
            json={"htn_id": "ada-ball", "expected_revision": 0},
        )
        self.assertEqual(forbidden.status_code, 409, forbidden.text)


if __name__ == "__main__":
    unittest.main()
