"""Focused persistence coverage for the shared two-player battle domain."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from badge_store import (  # noqa: E402
    BadgeStore,
    BattleCandidateOutcome,
    ConflictError,
    PokemonRecord,
    utc_now,
)


class BattleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.store = BadgeStore(Path(self._temporary_directory.name) / "world.sqlite3")
        self.alice = self.store.create_player("Alice", player_id="player_alice")
        self.bob = self.store.create_player("Bob", player_id="player_bob")
        self.alice_mon = self._create_pokemon("mon_alice", self.alice.player_id, "Coilkit")
        self.bob_mon = self._create_pokemon("mon_bob", self.bob.player_id, "Mossbyte")

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _create_pokemon(self, pokemon_id: str, owner: str, name: str) -> PokemonRecord:
        return self.store.create_pokemon(
            PokemonRecord(
                pokemon_id=pokemon_id,
                owner_player_id=owner,
                name=name,
                species="test creature",
                types=("electric",),
                stats={"hp": 100, "attack": 70, "defense": 60, "speed": 80},
                moves=("spark_dash", "coil_guard", "gear_jolt", "static_field"),
                battle_natures=("conductive", "clockwork"),
                flavour="A test creature for a durable battle.",
                sprite_prompt="small test creature",
                rarity="common",
                caught_at=utc_now(),
                sprite_path="sprites/test.png",
                captured_by_badge_id=None,
            )
        )

    def _ready_battle(self):
        now = utc_now()
        battle = self.store.create_battle_challenge(
            self.alice.player_id,
            self.bob.player_id,
            ready_deadline_at=now + timedelta(minutes=1),
            created_at=now,
        )
        battle = self.store.mark_battle_ready(
            battle.battle_id,
            self.alice.player_id,
            updated_at=now,
        )
        return self.store.mark_battle_ready(
            battle.battle_id,
            self.bob.player_id,
            initial_player_id=self.alice.player_id,
            turn_deadline_at=now + timedelta(minutes=1),
            updated_at=now,
        )

    def test_challenge_turn_resolution_is_atomic_and_auditable(self) -> None:
        battle = self._ready_battle()
        self.assertEqual(battle.status, "active")
        self.assertEqual(battle.current_player_id, self.alice.player_id)
        candidate = BattleCandidateOutcome(
            candidate_id="option_1",
            summary="Coilkit releases a tidy burst of static.",
            rationale="Its conductive coils channel the stored charge.",
            target_hp_delta=-20,
        )
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
            expected_revision=battle.revision,
        )
        self.assertEqual(resolving.status, "resolving")
        self.assertEqual(turn.status, "resolving")
        damaged_target = replace(
            resolving.opponent_roster.active_pokemon,
            current_hp=80,
        )
        opponent_roster = replace(
            resolving.opponent_roster,
            pokemon=(damaged_target,),
        )
        resolved = self.store.resolve_battle_turn(
            resolving.battle_id,
            self.alice.player_id,
            turn_id=turn.turn_id,
            selected_candidate_id="option_1",
            challenger_roster=resolving.challenger_roster,
            opponent_roster=opponent_roster,
            expected_revision=resolving.revision,
        )
        self.assertEqual(resolved.status, "active")
        self.assertEqual(resolved.current_player_id, self.bob.player_id)
        self.assertEqual(resolved.opponent_roster.active_pokemon.current_hp, 80)
        stored_turn = self.store.list_battle_turns(resolved.battle_id)[0]
        self.assertEqual(stored_turn.status, "resolved")
        self.assertEqual(stored_turn.selected_candidate_id, "option_1")
        self.assertIn("conductive", stored_turn.visible_rationale or "")

    def test_abort_releases_the_same_turn_for_retry(self) -> None:
        battle = self._ready_battle()
        candidate = BattleCandidateOutcome(
            candidate_id="option_1",
            summary="A cautious static flicker.",
            rationale="Coilkit tests the air with a conductive spark.",
            target_hp_delta=-5,
        )
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
        )
        retriable = self.store.abort_battle_resolution(
            resolving.battle_id,
            self.alice.player_id,
            turn_id=turn.turn_id,
            reason="Director unavailable",
            expected_revision=resolving.revision,
        )
        self.assertEqual(retriable.status, "active")
        retry, second_turn = self.store.begin_battle_resolution(
            retriable.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
            expected_revision=retriable.revision,
        )
        self.assertEqual(retry.status, "resolving")
        self.assertEqual(second_turn.turn_number, turn.turn_number)
        self.assertEqual(second_turn.attempt, 2)

    def test_disconnect_then_timeout_awards_the_other_player(self) -> None:
        battle = self._ready_battle()
        now = utc_now()
        disconnected = self.store.mark_battle_disconnected(
            battle.battle_id,
            self.alice.player_id,
            disconnect_deadline_at=now + timedelta(seconds=1),
            disconnected_at=now,
        )
        self.assertEqual(disconnected.status, "disconnected")
        expired = self.store.expire_battles(now=now + timedelta(seconds=2))
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].status, "timed_out")
        self.assertEqual(expired[0].winner_player_id, self.bob.player_id)

    def test_disconnect_during_resolution_aborts_attempt_and_retries_active_turn(self) -> None:
        """A process-local model request may never leave the durable match stuck."""

        battle = self._ready_battle()
        candidate = BattleCandidateOutcome(
            candidate_id="option_1",
            summary="A static burst arcs between the combatants.",
            rationale="Coilkit's conductive nature channels the stored charge.",
            target_hp_delta=-12,
        )
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
        )
        now = utc_now()
        disconnected = self.store.mark_battle_disconnected(
            resolving.battle_id,
            self.alice.player_id,
            disconnect_deadline_at=now + timedelta(seconds=30),
            disconnected_at=now,
        )
        self.assertEqual(disconnected.status, "disconnected")
        self.assertEqual(disconnected.status_before_disconnect, "active")
        self.assertIsNone(disconnected.turn_deadline_at)
        self.assertEqual(self.store.list_battle_turns(battle.battle_id)[0].status, "aborted")

        restored = self.store.restore_battle_connection(
            battle.battle_id,
            self.alice.player_id,
            turn_deadline_at=now + timedelta(minutes=1),
            restored_at=now,
        )
        self.assertEqual(restored.status, "active")
        self.assertEqual(restored.current_player_id, self.alice.player_id)
        self.assertIsNotNone(restored.turn_deadline_at)

    def test_resolution_lease_recovers_after_restart_without_forfeiting_move(self) -> None:
        """A dead server releases a resolving move for retry instead of timing out."""

        battle = self._ready_battle()
        started_at = utc_now()
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            # Claim before Writer: no outcomes exist yet, but this is already
            # a legal submitted move under a bounded resolution lease.
            candidate_outcomes=(),
            expected_revision=battle.revision,
            started_at=started_at,
            resolution_deadline_at=started_at + timedelta(seconds=1),
        )
        self.assertEqual(resolving.status, "resolving")
        self.assertEqual(turn.candidate_outcomes, ())

        database_path = Path(self._temporary_directory.name) / "world.sqlite3"
        self.store.close()
        self.store = BadgeStore(database_path)

        recovered = self.store.expire_battles(
            now=started_at + timedelta(seconds=2),
            resolving_retry_timeout_seconds=45,
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, "active")
        self.assertEqual(recovered[0].current_player_id, self.alice.player_id)
        self.assertEqual(
            recovered[0].turn_deadline_at,
            started_at + timedelta(seconds=47),
        )
        stored_turn = self.store.list_battle_turns(battle.battle_id)[0]
        self.assertEqual(stored_turn.status, "aborted")
        self.assertEqual(stored_turn.abort_reason, "resolution_timeout")

        retry, retry_turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(),
            expected_revision=recovered[0].revision,
            started_at=started_at + timedelta(seconds=2),
            resolution_deadline_at=started_at + timedelta(seconds=20),
        )
        self.assertEqual(retry.status, "resolving")
        self.assertEqual(retry_turn.turn_number, turn.turn_number)
        self.assertEqual(retry_turn.attempt, 2)

    def test_late_model_result_cannot_commit_after_resolution_lease(self) -> None:
        """The store rejects a result after its recovery lease, not a player clock."""

        battle = self._ready_battle()
        started_at = utc_now()
        candidate = BattleCandidateOutcome(
            candidate_id="option_1",
            summary="A cautious static pulse.",
            rationale="Coilkit sends a controlled charge through its coils.",
            target_hp_delta=-10,
        )
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
            expected_revision=battle.revision,
            started_at=started_at,
            resolution_deadline_at=started_at + timedelta(seconds=1),
        )
        damaged_target = replace(
            resolving.opponent_roster.active_pokemon,
            current_hp=90,
        )
        opponent_roster = replace(
            resolving.opponent_roster,
            pokemon=(damaged_target,),
        )

        with self.assertRaises(ConflictError):
            self.store.resolve_battle_turn(
                resolving.battle_id,
                self.alice.player_id,
                turn_id=turn.turn_id,
                selected_candidate_id="option_1",
                challenger_roster=resolving.challenger_roster,
                opponent_roster=opponent_roster,
                expected_revision=resolving.revision,
                resolved_at=started_at + timedelta(seconds=2),
            )

        retriable = self.store.abort_battle_resolution(
            resolving.battle_id,
            self.alice.player_id,
            turn_id=turn.turn_id,
            reason="resolution_timeout",
            expected_revision=resolving.revision,
            updated_at=started_at + timedelta(seconds=2),
            retry_turn_deadline_at=started_at + timedelta(seconds=62),
        )
        self.assertEqual(retriable.status, "active")
        self.assertEqual(self.store.list_battle_turns(battle.battle_id)[0].status, "aborted")

    def test_legacy_resolving_row_without_lease_is_eventually_recovered(self) -> None:
        """An older crashed deployment cannot leave a NULL-lease lock forever."""

        battle = self._ready_battle()
        started_at = utc_now()
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(),
            expected_revision=battle.revision,
            started_at=started_at,
            resolution_deadline_at=started_at + timedelta(minutes=1),
        )
        # Simulate a row created by an older version before resolution leases
        # were persisted. This is intentionally a migration-level setup.
        self.store.connection.execute(
            "UPDATE battles SET turn_deadline_at = NULL, updated_at = ? WHERE battle_id = ?",
            (started_at.isoformat(), resolving.battle_id),
        )

        recovered = self.store.expire_battles(
            now=started_at + timedelta(seconds=2),
            resolving_retry_timeout_seconds=30,
            resolving_fallback_timeout_seconds=1,
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, "active")
        self.assertEqual(self.store.list_battle_turns(battle.battle_id)[0].turn_id, turn.turn_id)
        self.assertEqual(self.store.list_battle_turns(battle.battle_id)[0].status, "aborted")

    def test_stale_revision_and_forged_profile_are_rejected(self) -> None:
        battle = self._ready_battle()
        with self.assertRaises(ConflictError):
            self.store.mark_battle_ready(
                battle.battle_id,
                self.alice.player_id,
                expected_revision=0,
            )
        candidate = BattleCandidateOutcome(
            candidate_id="option_1",
            summary="A tiny jolt.",
            rationale="Coilkit lets off a controlled charge.",
            target_hp_delta=-5,
        )
        resolving, turn = self.store.begin_battle_resolution(
            battle.battle_id,
            self.alice.player_id,
            move_id="spark_dash",
            candidate_outcomes=(candidate,),
        )
        forged = replace(
            resolving.opponent_roster.active_pokemon,
            name="Not Mossbyte",
            current_hp=95,
        )
        with self.assertRaises(ValueError):
            self.store.resolve_battle_turn(
                resolving.battle_id,
                self.alice.player_id,
                turn_id=turn.turn_id,
                selected_candidate_id="option_1",
                challenger_roster=resolving.challenger_roster,
                opponent_roster=replace(resolving.opponent_roster, pokemon=(forged,)),
            )


if __name__ == "__main__":
    unittest.main()
