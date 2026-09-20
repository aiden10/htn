"""Network-free coverage for the Writer -> Jev battle coordinator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_store import BadgeStore, PokemonRecord, utc_now  # noqa: E402
from battle_service import (  # noqa: E402
    BattleDirectorUnavailableError,
    BattleService,
    JEV_MODEL,
)


class BattleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.store = BadgeStore(Path(self._temporary_directory.name) / "battle.sqlite3")
        self.alice = self.store.create_player("Alice", player_id="player_alice")
        self.bob = self.store.create_player("Bob", player_id="player_bob")
        self._create_pokemon("mon_alice", self.alice.player_id, "Coilkit")
        self._create_pokemon("mon_bob", self.bob.player_id, "Mossbyte")
        self.calls: list[dict[str, object]] = []
        self.resolution_started: list[tuple[str, str, str]] = []

        async def writer(request: dict[str, object]) -> object:
            self.calls.append(dict(request))
            return {
                "content": json.dumps(
                    {
                        "outcomes": [
                            {
                                "summary": "Coilkit sends a careful spark.",
                                "rationale": "Conductive coils give its static a clear path.",
                                "actor_hp_delta": 0,
                                "target_hp_delta": -8,
                                "actor_stat": None,
                                "actor_stat_delta": 0,
                                "target_stat": None,
                                "target_stat_delta": 0,
                            },
                            {
                                "summary": "Coilkit grounds a stronger jolt.",
                                "rationale": "Clockwork timing lets the charge land cleanly.",
                                "actor_hp_delta": 0,
                                "target_hp_delta": -15,
                                "actor_stat": None,
                                "actor_stat_delta": 0,
                                "target_stat": None,
                                "target_stat_delta": 0,
                            },
                            {
                                "summary": "Coilkit raises a humming guard.",
                                "rationale": "Its wound coils turn the current into a defense.",
                                "actor_hp_delta": 0,
                                "target_hp_delta": 0,
                                "actor_stat": "defense",
                                "actor_stat_delta": 1,
                                "target_stat": None,
                                "target_stat_delta": 0,
                            },
                        ]
                    }
                )
            }

        async def director(request: dict[str, object]) -> object:
            self.calls.append(dict(request))
            return {
                "system_one": {
                    "answers": {"selected_outcome": {"choice": "option_2"}}
                }
            }

        self.writer = writer
        self.director = director

        async def resolution_started(battle, turn) -> None:
            self.resolution_started.append(
                (battle.battle_id, battle.status, turn.status)
            )

        self.service = BattleService(
            self.store,
            writer_call=self.writer,
            jev_call=self.director,
            on_resolution_started=resolution_started,
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _create_pokemon(self, pokemon_id: str, owner: str, name: str) -> None:
        self.store.create_pokemon(
            PokemonRecord(
                pokemon_id=pokemon_id,
                owner_player_id=owner,
                name=name,
                species="test battle creature",
                types=("electric",),
                stats={"hp": 100, "attack": 70, "defense": 60, "speed": 80},
                moves=("spark_dash", "coil_guard", "gear_jolt", "static_field"),
                battle_natures=("conductive", "clockwork"),
                flavour="A creature made only for a deterministic battle test.",
                sprite_prompt="small test creature",
                rarity="common",
                caught_at=utc_now(),
                sprite_path="sprites/test.png",
                captured_by_badge_id=None,
            )
        )

    async def _ready_battle(self):
        battle = await self.service.create_challenge(
            self.alice.player_id, self.bob.player_id
        )
        battle = await self.service.ready(battle.battle_id, self.alice.player_id)
        return await self.service.ready(battle.battle_id, self.bob.player_id)

    async def test_sdk_adapter_passes_message_as_backboard_content(self) -> None:
        """The real SDK accepts a positional ``content`` argument, not message."""

        received: dict[str, object] = {}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def send_message(self, content: str, **kwargs: object) -> dict[str, object]:
                received["content"] = content
                received.update(kwargs)
                return {"content": "ok"}

        service = BattleService(
            self.store,
            backboard_api_key="test-key",
            client_factory=lambda **_: FakeClient(),
        )
        response = await service._invoke_model(
            {"message": "writer prompt", "model_name": "fast-model", "stream": False},
            None,
        )

        self.assertEqual(response, {"content": "ok"})
        self.assertEqual(received["content"], "writer prompt")
        self.assertEqual(received["model_name"], "fast-model")
        self.assertNotIn("message", received)

    async def test_writer_then_jev_persists_only_selected_bounded_outcome(self) -> None:
        battle = await self._ready_battle()
        self.assertEqual(battle.status, "active")
        self.assertEqual(battle.current_player_id, self.alice.player_id)

        result = await self.service.resolve_move(
            battle.battle_id, self.alice.player_id, 0
        )

        self.assertEqual(result.battle.status, "active")
        self.assertEqual(result.battle.current_player_id, self.bob.player_id)
        self.assertEqual(result.selected_candidate.candidate_id, "option_2")
        self.assertEqual(result.battle.opponent_roster.active_pokemon.current_hp, 85)
        self.assertEqual(
            result.battle.last_visible_rationale,
            "Clockwork timing lets the charge land cleanly.",
        )
        self.assertEqual(result.turn.status, "resolved")
        self.assertEqual(result.turn.selected_candidate_id, "option_2")
        self.assertEqual(len(result.turn.candidate_outcomes), 3)
        self.assertEqual(
            self.resolution_started,
            [(battle.battle_id, "resolving", "resolving")],
        )
        self.assertEqual(
            [call["model_name"] for call in self.calls],
            ["gpt-4.1-nano", JEV_MODEL],
        )
        writer_context = json.loads(str(self.calls[0]["message"]))
        self.assertEqual(writer_context["move"], "spark_dash")
        self.assertEqual(writer_context["actor"]["name"], "Coilkit")

    async def test_bad_director_choice_aborts_durable_lock_for_retry(self) -> None:
        battle = await self._ready_battle()

        async def bad_director(request: dict[str, object]) -> object:
            self.calls.append(dict(request))
            return {
                "system_one": {
                    "answers": {"selected_outcome": {"choice": "not_offered"}}
                }
            }

        self.service = BattleService(
            self.store,
            writer_call=self.writer,
            jev_call=bad_director,
        )
        with self.assertRaises(BattleDirectorUnavailableError):
            await self.service.resolve_move(battle.battle_id, self.alice.player_id, 0)

        retriable = self.store.require_battle(battle.battle_id)
        self.assertEqual(retriable.status, "active")
        self.assertEqual(retriable.current_player_id, self.alice.player_id)
        turn = self.store.list_battle_turns(battle.battle_id)[0]
        self.assertEqual(turn.status, "aborted")
        self.assertIn("failed", turn.abort_reason or "")

    async def test_writer_overkill_is_clipped_instead_of_aborting_the_turn(self) -> None:
        """A model's bad HP arithmetic must not lock both battle badges."""

        async def overkill_writer(request: dict[str, object]) -> object:
            response = await self.writer(request)
            payload = json.loads(str(response["content"]))
            payload["outcomes"][0]["actor_hp_delta"] = -999
            return {"content": json.dumps(payload)}

        self.service = BattleService(
            self.store,
            writer_call=overkill_writer,
            jev_call=self.director,
        )
        battle = await self._ready_battle()
        result = await self.service.resolve_move(
            battle.battle_id, self.alice.player_id, 0
        )

        # The first unselected candidate is persisted as a legal knockout,
        # while the Director can still choose a separate candidate normally.
        self.assertEqual(result.turn.candidate_outcomes[0].actor_hp_delta, -100)
        self.assertEqual(result.selected_candidate.candidate_id, "option_2")
        self.assertEqual(result.battle.status, "active")

    async def test_selected_knockout_finishes_the_shared_battle(self) -> None:
        """A valid selected outcome ends the snapshot, never the collection."""

        battle = await self._ready_battle()

        async def knockout_writer(request: dict[str, object]) -> object:
            del request
            return {
                "outcomes": [
                    {
                        "summary": "Coilkit releases its full stored charge.",
                        "rationale": "Its conductive coils overwhelm the final guard.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": -100,
                        "actor_stat": None,
                        "actor_stat_delta": 0,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                    {
                        "summary": "Coilkit tests the opponent with a spark.",
                        "rationale": "A small arc reveals where the guard is weakest.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": -10,
                        "actor_stat": None,
                        "actor_stat_delta": 0,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                    {
                        "summary": "Coilkit braces its gears.",
                        "rationale": "Clockwork timing improves its defensive stance.",
                        "actor_hp_delta": 0,
                        "target_hp_delta": 0,
                        "actor_stat": "defense",
                        "actor_stat_delta": 1,
                        "target_stat": None,
                        "target_stat_delta": 0,
                    },
                ]
            }

        async def choose_knockout(request: dict[str, object]) -> object:
            del request
            return {"system_one": {"answers": {"selected_outcome": {"choice": "option_1"}}}}

        service = BattleService(
            self.store,
            writer_call=knockout_writer,
            jev_call=choose_knockout,
        )
        result = await service.resolve_move(
            battle.battle_id, self.alice.player_id, 0
        )

        self.assertEqual(result.battle.status, "finished")
        self.assertEqual(result.battle.winner_player_id, self.alice.player_id)
        self.assertEqual(result.battle.end_reason, "knockout")
        self.assertIsNone(result.battle.current_player_id)
        self.assertEqual(result.battle.opponent_roster.active_pokemon.current_hp, 0)
        # Battle HP is snapshot-only: the stored Pokémon remains unharmed.
        self.assertEqual(self.store.require_pokemon("mon_bob").stats["hp"], 100)

    async def test_submitted_move_pauses_turn_clock_until_commit(self) -> None:
        """Writer latency past the original move deadline cannot forfeit a move."""

        current_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return current_time

        battle_id: str | None = None

        async def delayed_writer(request: dict[str, object]) -> object:
            nonlocal current_time
            # The ordinary ten-second player clock has passed, but the
            # durable record is now resolving under its separate lease.
            current_time += timedelta(seconds=11)
            self.assertIsNotNone(battle_id)
            self.assertEqual(
                self.store.expire_battles(
                    now=current_time,
                    resolving_retry_timeout_seconds=10,
                ),
                [],
            )
            self.assertEqual(self.store.require_battle(battle_id).status, "resolving")
            return await self.writer(request)

        self.service = BattleService(
            self.store,
            writer_call=delayed_writer,
            jev_call=self.director,
            clock=clock,
            turn_timeout_seconds=10,
            model_timeout_seconds=1,
        )
        battle = await self._ready_battle()
        battle_id = battle.battle_id

        result = await self.service.resolve_move(
            battle.battle_id, self.alice.player_id, 0
        )

        self.assertEqual(result.battle.status, "active")
        self.assertEqual(result.battle.current_player_id, self.bob.player_id)
        self.assertGreater(
            result.battle.turn_deadline_at or current_time,
            current_time,
        )
        self.assertEqual(self.store.list_battle_turns(battle.battle_id)[0].status, "resolved")

    async def test_cancelling_during_resolving_publish_releases_durable_turn(self) -> None:
        """A cancelled request cannot strand the shared battle in resolving."""

        publish_started = asyncio.Event()
        let_publish_finish = asyncio.Event()

        async def blocked_publish(battle, turn) -> None:
            self.assertEqual(battle.status, "resolving")
            self.assertEqual(turn.status, "resolving")
            publish_started.set()
            await let_publish_finish.wait()

        self.service = BattleService(
            self.store,
            writer_call=self.writer,
            jev_call=self.director,
            on_resolution_started=blocked_publish,
        )
        battle = await self._ready_battle()
        task = asyncio.create_task(
            self.service.resolve_move(battle.battle_id, self.alice.player_id, 0)
        )
        await asyncio.wait_for(publish_started.wait(), timeout=1)
        self.assertEqual(self.store.require_battle(battle.battle_id).status, "resolving")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        retriable = self.store.require_battle(battle.battle_id)
        self.assertEqual(retriable.status, "active")
        self.assertEqual(retriable.current_player_id, self.alice.player_id)
        self.assertIsNotNone(retriable.turn_deadline_at)
        turn = self.store.list_battle_turns(battle.battle_id)[0]
        self.assertEqual(turn.status, "aborted")
        self.assertIn("cancelled", turn.abort_reason or "")


if __name__ == "__main__":
    unittest.main()
