"""Regression coverage for per-player Habitat simulation isolation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_store import BadgeStore, pokemon_from_dict
from player_simulation import (
    HabitatWriterUnavailableError,
    JevUnavailableError,
    PlayerSimulationService,
)


class FakeBackboardClient:
    """Local writer + Jev stand-in; no test sends a Backboard request."""

    calls: list[dict[str, object]] = []
    writer_mode = "valid"
    jev_choice = "option_2"

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def __aenter__(self) -> "FakeBackboardClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    async def send_message(self, message: str, **kwargs: object) -> object:
        self.calls.append({"message": message, **kwargs})
        model_name = kwargs.get("model_name")
        if model_name == "gpt-4.1-nano":
            if self.writer_mode == "invalid":
                return type("FakeBackboardResponse", (), {"content": "not JSON"})()
            context = json.loads(message)
            actor = context["actor"]["name"]
            target = context["target"]
            target_name = target["name"] if target else None
            events = []
            for kind, action in (
                ("observe", "maps the silver ripples near the reeds"),
                ("greet", "offers a curious hello beside the water"),
                ("play", "starts a pebble-skipping game"),
            ):
                dialogue = [
                    {"speaker": "actor", "text": f"{actor}: {action.title()}. Join me?"}
                ]
                summary = f"{actor} {action}."
                if target_name:
                    summary = f"{actor} {action} with {target_name}."
                    dialogue.append(
                        {
                            "speaker": "target",
                            "text": f"{target_name} studies {actor}'s idea.",
                        }
                    )
                events.append({"kind": kind, "summary": summary, "dialogue": dialogue})
            return type(
                "FakeBackboardResponse", (), {"content": json.dumps({"events": events})}
            )()
        if model_name == "jev-latest":
            return type(
                "FakeBackboardResponse",
                (),
                {"system_one": {"answers": {"next_event": {"choice": self.jev_choice}}}},
            )()
        raise AssertionError(f"Unexpected Backboard model in test: {model_name!r}")


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
        FakeBackboardClient.writer_mode = "valid"
        FakeBackboardClient.jev_choice = "option_2"
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
        self.assertEqual(first.event.kind, "greet")
        first_writer_context = json.loads(str(FakeBackboardClient.calls[0]["message"]))
        first_actor = first_writer_context["actor"]["name"]
        first_target = first_writer_context["target"]["name"]
        self.assertEqual(
            first.event.summary,
            f"{first_actor} offers a curious hello beside the water with {first_target}.",
        )
        self.assertEqual(
            [line.text for line in first.event.dialogue],
            [
                f"{first_actor}: Offers A Curious Hello Beside The Water. Join me?",
                f"{first_target} studies {first_actor}'s idea.",
            ],
        )

        second = await self.service.tick(self.player_a.player_id)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(second.event.event_id, first.event.event_id)
        self.assertEqual(len(self.store.list_simulation_events_for_player(self.player_a.player_id)), 2)
        self.assertEqual(len(FakeBackboardClient.calls), 4)
        self.assertEqual(
            [call["model_name"] for call in FakeBackboardClient.calls],
            ["gpt-4.1-nano", "jev-latest", "gpt-4.1-nano", "jev-latest"],
        )
        second_writer_context = json.loads(str(FakeBackboardClient.calls[2]["message"]))
        self.assertNotEqual(second_writer_context["actor"]["name"], first_actor)
        self.assertEqual(
            second.event.summary,
            f"{second_writer_context['actor']['name']} offers a curious hello beside the water with "
            f"{second_writer_context['target']['name']}.",
        )
        self.assertEqual(
            second_writer_context["recent_events"][-1]["summary"], first.event.summary
        )
        self.assertEqual(
            second_writer_context["recent_events"][-1]["dialogue"][0]["text"],
            first.event.dialogue[0].text,
        )

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

        with self.assertRaises(HabitatWriterUnavailableError):
            await unavailable_service.tick(self.player_a.player_id)

        self.assertEqual(
            self.store.list_world_states_for_player(self.player_a.player_id), []
        )
        self.assertEqual(
            self.store.list_simulation_events_for_player(self.player_a.player_id), []
        )

    async def test_invalid_writer_or_jev_response_preserves_the_world(self) -> None:
        FakeBackboardClient.writer_mode = "invalid"
        with self.assertRaises(HabitatWriterUnavailableError):
            await self.service.tick(self.player_a.player_id)
        self.assertEqual(self.store.list_world_states_for_player(self.player_a.player_id), [])
        self.assertEqual(self.store.list_simulation_events_for_player(self.player_a.player_id), [])

        FakeBackboardClient.writer_mode = "valid"
        FakeBackboardClient.jev_choice = "not_an_offered_option"
        with self.assertRaises(JevUnavailableError):
            await self.service.tick(self.player_a.player_id)
        self.assertEqual(self.store.list_world_states_for_player(self.player_a.player_id), [])
        self.assertEqual(self.store.list_simulation_events_for_player(self.player_a.player_id), [])

    def test_writer_keeps_valid_dialogue_roles_and_ignores_surplus_lines(self) -> None:
        events = []
        for index, kind in enumerate(("observe", "greet", "play"), start=1):
            events.append(
                {
                    "kind": kind,
                    "summary": f"Aster performs test action {index} with Bramble.",
                    "dialogue": [
                        {"speaker": "narrator", "text": "Ignored scene-setting."},
                        {"speaker": "target", "text": "Bramble replies."},
                        {"speaker": "actor", "text": "Aster begins the action."},
                        {"speaker": "actor", "text": "Ignored duplicate."},
                    ],
                }
            )
        response = type(
            "FakeBackboardResponse", (), {"content": json.dumps({"events": events})}
        )()

        proposals = PlayerSimulationService._proposals_from_writer_response(
            response,
            target_present=True,
            recent_events=(),
            actor_name="Aster",
            target_name="Bramble",
        )

        self.assertEqual(len(proposals), 3)
        self.assertEqual(
            proposals[0].dialogue_roles,
            (("actor", "Aster begins the action."), ("target", "Bramble replies.")),
        )

    def test_writer_accepts_pokemon_names_and_summary_fallback(self) -> None:
        events = [
            {
                "kind": kind,
                "summary": f"Aster performs fallback action {index}.",
                "dialogue": ([] if index == 3 else [
                    {"speaker": "Aster", "text": "Aster acts."},
                    {"speaker": "Bramble", "text": "Bramble responds."},
                ]),
            }
            for index, kind in enumerate(("observe", "greet", "play"), start=1)
        ]
        response = type(
            "FakeBackboardResponse", (), {"content": json.dumps({"events": events})}
        )()

        proposals = PlayerSimulationService._proposals_from_writer_response(
            response,
            target_present=True,
            recent_events=(),
            actor_name="Aster",
            target_name="Bramble",
        )

        self.assertEqual(
            proposals[0].dialogue_roles,
            (("actor", "Aster acts."), ("target", "Bramble responds.")),
        )
        self.assertEqual(
            proposals[2].dialogue_roles,
            (("actor", "Aster performs fallback action 3."),),
        )


if __name__ == "__main__":
    unittest.main()
