"""HTTP regression coverage for the server-rendered Shutterdex API.

The test creates a tiny FastAPI application instead of importing ``main``.
That keeps the legacy image/serial lifespan out of the test and proves that
the Wi-Fi routes only need their explicitly injected ownership, UI, gateway,
sprite, and simulation services. Its badge transport and Jev response are
purely in memory, so no device, app WebSocket, credential, or network request
is involved.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx


# Support both ``python -m unittest discover -s tests`` from ``server/`` and
# directly running this file from elsewhere in the repository.
SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_renderer import ScreenRenderer, SpriteImageResolver
from badge_store import BadgeStore
from badge_ui import BadgeUi
from htn_gateway import HTNBadgeGateway, InMemoryBadgeTransport
from player_simulation import JEV_MODEL, PlayerSimulationService
from shutterdex_api import router as shutterdex_router
from shutterdex_runtime import ShutterdexRuntime
from sprite_assets import SpriteStore


class MemoryVault:
    """Reversible fake vault whose stored form deliberately excludes plaintext."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    def seal(self, value: str) -> str:
        ciphertext = "test-vault-" + sha256(value.encode("utf-8")).hexdigest()
        self._entries[ciphertext] = value
        return ciphertext

    def open(self, ciphertext: str) -> str:
        return self._entries[ciphertext]


class FakeBackboardClient:
    """A deterministic local writer + Jev pair; no network leaves the test."""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def __aenter__(self) -> "FakeBackboardClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    async def send_message(self, message: str, **kwargs: object) -> object:
        if kwargs.get("model_name") == "gpt-4.1-nano":
            actor = json.loads(message)["actor"]["name"]
            return type(
                "FakeBackboardResponse",
                (),
                {
                    "content": json.dumps(
                        {
                            "events": [
                                {
                                    "kind": "observe",
                                    "summary": f"{actor} checks the warm stones.",
                                    "dialogue": [
                                        {"speaker": "actor", "text": "These stones remember sunlight."}
                                    ],
                                },
                                {
                                    "kind": "greet",
                                    "summary": f"{actor} practises a new greeting.",
                                    "dialogue": [
                                        {"speaker": "actor", "text": "Hello, Habitat. I am ready."}
                                    ],
                                },
                                {
                                    "kind": "play",
                                    "summary": f"{actor} sends a leaf skimming across water.",
                                    "dialogue": [
                                        {"speaker": "actor", "text": "That one almost skipped twice."}
                                    ],
                                },
                            ]
                        }
                    )
                },
            )()
        if kwargs.get("model_name") == JEV_MODEL:
            return type(
                "FakeBackboardResponse",
                (),
                {"system_one": {"answers": {"next_event": {"choice": "option_3"}}}},
            )()
        raise AssertionError(f"Unexpected Backboard test model: {kwargs.get('model_name')!r}")


class ShutterdexApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.store = BadgeStore(root / "shutterdex.sqlite3")
        self.sprites = SpriteStore(root / "sprites")
        self.transport = InMemoryBadgeTransport()
        # Fast rate is safe here because the transport is local and lets the
        # test assert behavior rather than wait on the production 20 Hz limit.
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
        self.app = FastAPI()
        self.app.include_router(shutterdex_router)
        self.app.state.shutterdex_store = self.store
        self.app.state.shutterdex_runtime = self.runtime
        self.app.state.sprites = self.sprites
        self._backboard_patch = patch("player_simulation.BackboardClient", FakeBackboardClient)
        self._backboard_patch.start()
        self.app.state.player_simulation = PlayerSimulationService(
            self.store, backboard_api_key="test-backboard-key"
        )
        await self.runtime.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://shutterdex.test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._wait_for_deliveries()
        await self.runtime.close()
        self._backboard_patch.stop()
        self.store.close()
        self._temporary_directory.cleanup()

    async def _wait_for_deliveries(self) -> None:
        """Let local queued draw receipts settle before gateway shutdown."""

        for _ in range(10_000):
            if not self.runtime._delivery_tasks:
                return
            await asyncio.sleep(0)
        self.fail("Timed out waiting for in-memory badge draw delivery")

    def assert_secret_absent(self, secret: str, response: httpx.Response) -> None:
        """Pairing data must not expose either the raw key or encrypted field."""

        serialized = json.dumps(response.json(), sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("app_key_ciphertext", serialized)
        self.assertNotIn("app_key", serialized)

    async def test_owned_badge_api_flow_and_jev_directed_habitat_tick(self) -> None:
        secret = "never-return-this-key"

        created_player = await self.client.post(
            "/shutterdex/players", json={"display_name": "Ada"}
        )
        self.assertEqual(created_player.status_code, 201, created_player.text)
        player_id = created_player.json()["player"]["player_id"]

        paired = await self.client.post(
            "/shutterdex/badges/pair",
            json={
                "player_id": player_id,
                "htn_id": "ada42",
                "app_key": secret,
            },
        )
        self.assertEqual(paired.status_code, 201, paired.text)
        self.assertEqual(paired.json()["connection"], "registered")
        self.assert_secret_absent(secret, paired)
        self.assertEqual(self.transport.connected_badges, {"ada42"})
        persisted_badge = self.store.require_badge_by_htn_id("ada42")
        self.assertNotEqual(persisted_badge.app_key_ciphertext, secret)

        badge_lookup = await self.client.get("/shutterdex/badges/ada42")
        self.assertEqual(badge_lookup.status_code, 200, badge_lookup.text)
        self.assertEqual(badge_lookup.json()["badge"]["badge_id"], persisted_badge.badge_id)
        self.assert_secret_absent(secret, badge_lookup)

        created_pokemon = await self.client.post(
            f"/shutterdex/players/{player_id}/pokemon",
            json={
                "captured_by_htn_id": "ada42",
                "pokemon": {
                    "pokemon_id": "mon_sprocket",
                    "name": "Sprocket",
                    "species": "pocket-sized clockwork fox",
                    "type": "electric",
                    "stats": {"hp": 54, "attack": 72, "defense": 46, "speed": 89},
                    "moves": ["spark_dash"],
                    "flavour": "It sleeps curled around a warm battery.",
                    "sprite_prompt": "a tiny brass fox with a lightning tail",
                    "rarity": "common",
                },
            },
        )
        self.assertEqual(created_pokemon.status_code, 201, created_pokemon.text)
        self.assertEqual(created_pokemon.json()["pokemon"]["owner_player_id"], player_id)
        self.assertEqual(
            created_pokemon.json()["pokemon"]["captured_by_badge_id"],
            persisted_badge.badge_id,
        )
        self.assert_secret_absent(secret, created_pokemon)

        launched = await self.client.post("/shutterdex/badges/ada42/launch")
        self.assertEqual(launched.status_code, 200, launched.text)
        self.assertEqual(launched.json()["render"]["scene"], "home")
        self.assertGreater(launched.json()["render"]["queued_commands"], 0)
        self.assert_secret_absent(secret, launched)

        opened_dex = await self.client.post(
            "/shutterdex/badges/ada42/actions", json={"button": "a"}
        )
        self.assertEqual(opened_dex.status_code, 200, opened_dex.text)
        self.assertTrue(opened_dex.json()["handled"])
        self.assertEqual(opened_dex.json()["render"]["scene"], "dex")
        self.assert_secret_absent(secret, opened_dex)

        session = await self.client.get("/shutterdex/badges/ada42/session")
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json()["session"]["active_app"], "dex")
        self.assertTrue(session.json()["session"]["canvas_active"])
        self.assert_secret_absent(secret, session)

        tick = await self.client.post(f"/shutterdex/players/{player_id}/simulation/tick")
        self.assertEqual(tick.status_code, 200, tick.text)
        simulation = tick.json()["simulation"]
        self.assertEqual(simulation["jev_used"], "jev")
        self.assertEqual(simulation["player_id"], player_id)
        self.assertEqual(simulation["event"]["actor_pokemon_id"], "mon_sprocket")
        self.assertEqual(
            simulation["event"]["summary"],
            "Sprocket sends a leaf skimming across water.",
        )
        self.assertEqual(simulation["event"]["kind"], "play")
        self.assertEqual(len(simulation["world_states"]), 1)
        self.assert_secret_absent(secret, tick)

        # Let the in-memory command worker settle, then ensure normal draw
        # instructions do not serialize an app key either.
        await self._wait_for_deliveries()
        recorded_payloads = json.dumps(
            [record.command.payload for record in self.transport.commands], sort_keys=True
        )
        self.assertNotIn(secret, recorded_payloads)


if __name__ == "__main__":
    unittest.main()
