"""Regression coverage for per-player Habitat simulation isolation."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_store import BadgeStore, pokemon_from_dict
from player_simulation import JevUnavailableError, PlayerSimulationService


class FakeBackboardClient:
    """Local stand-in that proves every test tick makes a Jev request."""

    calls: list[dict[str, object]] = []

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def __aenter__(self) -> "FakeBackboardClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    async def send_message(self, message: str, **kwargs: object) -> object:
        self.calls.append({"message": message, **kwargs})
        choice = ("greet", "play")[min(len(self.calls) - 1, 1)]
        return type(
            "FakeBackboardResponse",
            (),
            {"system_one": {"answers": {"next_interaction": {"choice": choice}}}},
        )()


def _pokemon(*, pokemon_id: str, owner_player_id: str, name: str):
    return pokemon_from_dict(
        {
            "pokemon_id": pokemon_id,
            "name": name,
            "species": "test Habitat creature",
            "type": "earth",
            "stats": {"hp": 50, "attack": 60, "defense": 55, "speed": 45},
            "moves": ["test_move"],
            "flavour": "A creature created only for an isolated simulation test.",
            "sprite_prompt": "small test creature",
            "rarity": "common",
        },
        owner_player_id=owner_player_id,
    )


class PlayerSimulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.store = BadgeStore(Path(self._temporary_directory.name) / "world.sqlite3")
        FakeBackboardClient.calls = []
        self._backboard_patch = patch("player_simulation.BackboardClient", FakeBackboardClient)
        self._backboard_patch.start()
        self.service = PlayerSimulationService(
            self.store, backboard_api_key="test-backboard-key"
        )
        self.player_a = self.store.create_player("Player A", player_id="player_a")
        self.player_b = self.store.create_player("Player B", player_id="player_b")
        self.store.create_pokemon(
            _pokemon(pokemon_id="mon_a_one", owner_player_id=self.player_a.player_id, name="Aster")
        )
        self.store.create_pokemon(
            _pokemon(pokemon_id="mon_a_two", owner_player_id=self.player_a.player_id, name="Bramble")
        )
        self.store.create_pokemon(
            _pokemon(pokemon_id="mon_b_one", owner_player_id=self.player_b.player_id, name="Cinder")
        )

    async def asyncTearDown(self) -> None:
        self._backboard_patch.stop()
        self.store.close()
        self._temporary_directory.cleanup()

    async def test_tick_persists_only_requested_players_world(self) -> None:
        first = await self.service.tick(self.player_a.player_id)

        self.assertEqual(first.player_id, self.player_a.player_id)
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.director_used, "jev")
        self.assertEqual(len(first.world_states), 2)
        self.assertEqual(
            {state.pokemon_id for state in first.world_states}, {"mon_a_one", "mon_a_two"}
        )
        self.assertEqual(
            {state.pokemon_id for state in self.store.list_world_states_for_player(self.player_a.player_id)},
            {"mon_a_one", "mon_a_two"},
        )
        self.assertEqual(self.store.list_world_states_for_player(self.player_b.player_id), [])
        self.assertEqual(len(self.store.list_simulation_events_for_player(self.player_a.player_id)), 1)
        self.assertEqual(self.store.list_simulation_events_for_player(self.player_b.player_id), [])

        second = await self.service.tick(self.player_a.player_id)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(second.event.event_id, first.event.event_id)
        self.assertEqual(len(self.store.list_simulation_events_for_player(self.player_a.player_id)), 2)
        self.assertEqual(len(FakeBackboardClient.calls), 2)
        self.assertEqual(FakeBackboardClient.calls[0]["model_name"], "jev-latest")

    async def test_result_is_safe_serializable_data(self) -> None:
        result = await self.service.tick(self.player_a.player_id)
        payload = result.to_dict()

        self.assertEqual(payload["player_id"], self.player_a.player_id)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["event"]["player_id"], self.player_a.player_id)
        self.assertNotIn("app_key", repr(payload))
        self.assertNotIn("thread_id", payload)

    async def test_missing_backboard_key_preserves_the_world(self) -> None:
        unavailable_service = PlayerSimulationService(self.store, backboard_api_key="")

        with self.assertRaises(JevUnavailableError):
            await unavailable_service.tick(self.player_a.player_id)

        self.assertEqual(
            self.store.list_world_states_for_player(self.player_a.player_id), []
        )
        self.assertEqual(
            self.store.list_simulation_events_for_player(self.player_a.player_id), []
        )


if __name__ == "__main__":
    unittest.main()
