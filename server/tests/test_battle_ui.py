"""Focused display/reducer coverage for the server-rendered Battle scene."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

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
    Text,
)


def _combatant(name: str, *, hp: int = 80) -> BattleCombatantView:
    return BattleCombatantView(
        pokemon_id=f"mon_{name.lower()}",
        name=name,
        element="electric",
        hp=hp,
        max_hp=100,
        moves=("arc_lash", "overclock", "static_nip", "short_circuit"),
        roster_index=0,
        roster_size=6,
    )


def _battle(**changes: object) -> BattleView:
    values: dict[str, object] = {
        "battle_id": "battle_test",
        "phase": "active",
        "viewer_player_id": "player_ada",
        "viewer_player_name": "Ada",
        "opponent_player_id": "player_lee",
        "opponent_player_name": "Lee",
        "active_player_id": "player_ada",
        "viewer": _combatant("Voltfin"),
        "opponent": _combatant("Mugmite", hp=61),
        "turn_number": 3,
        "rationale": "Electric charge jumps from Voltfin's fins into Mugmite's glaze.",
    }
    values.update(changes)
    return BattleView(**values)  # type: ignore[arg-type]


def _context(
    battle: BattleView | None, opponents: tuple[BattleOpponent, ...] = ()
) -> BadgeUiContext:
    return BadgeUiContext(
        badge_id="badge_ada",
        player_id="player_ada",
        battle=battle,
        battle_opponents=opponents,
    )


class BattleUiTests(unittest.TestCase):
    def test_active_turn_renders_two_combatants_and_moves_focus_locally(self) -> None:
        app = BattleApp()
        context = _context(_battle())
        state = app.initial_state(context)

        screen = app.render(state, context)
        labels = [operation.text for operation in screen.operations if isinstance(operation, Text)]
        self.assertIn("YOUR TURN - CHOOSE A MOVE", labels)
        self.assertIn("Voltfin", labels)
        self.assertTrue(any("ARC LASH" in label for label in labels))
        self.assertIn("DIRECTOR RATIONALE", labels)

        moved = app.reduce(state, ButtonEvent.from_raw(Button.RIGHT), context)
        self.assertEqual(moved.state["move_index"], 1)
        self.assertEqual(
            app.requested_action(moved.state, ButtonEvent.from_raw(Button.A), context),
            "select_move",
        )

    def test_turn_lock_does_not_change_focus_or_emit_a_move(self) -> None:
        app = BattleApp()
        context = _context(_battle(active_player_id="player_lee"))
        state = {"move_index": 2}

        reduced = app.reduce(state, ButtonEvent.from_raw(Button.LEFT), context)
        self.assertEqual(reduced.state["move_index"], 2)
        self.assertIsNone(app.requested_action(reduced.state, ButtonEvent.from_raw(Button.A), context))
        labels = [operation.text for operation in app.render(reduced.state, context).operations if isinstance(operation, Text)]
        self.assertTrue(any("CONTROLS LOCKED" in label for label in labels))

    def test_setup_and_terminal_action_contracts(self) -> None:
        app = BattleApp()
        discovery = _context(None, (BattleOpponent("bee42"), BattleOpponent("cee73")))
        self.assertEqual(app.requested_action({}, ButtonEvent.from_raw(Button.A), discovery), "select_opponent")
        self.assertIsNone(app.reduce({}, ButtonEvent.from_raw(Button.A), discovery).navigate_to)
        discovery_labels = [
            operation.text
            for operation in app.render({}, discovery).operations
            if isinstance(operation, Text)
        ]
        self.assertIn("WAITING FOR CHALLENGE", discovery_labels)
        self.assertTrue(any("BEE42" in label for label in discovery_labels))

        no_peer = _context(None)
        self.assertIsNone(app.requested_action({}, ButtonEvent.from_raw(Button.A), no_peer))

        challenge = _context(_battle(phase="challenge", viewer_role="opponent", active_player_id=None))
        self.assertEqual(app.requested_action({}, ButtonEvent.from_raw(Button.A), challenge), "accept_challenge")
        challenge_labels = [operation.text for operation in app.render({}, challenge).operations if isinstance(operation, Text)]
        self.assertIn("CHALLENGE DETECTED", challenge_labels)

        ready = _context(_battle(phase="ready", viewer_ready=False, active_player_id=None))
        self.assertEqual(app.requested_action({}, ButtonEvent.from_raw(Button.A), ready), "ready")
        ready_labels = [operation.text for operation in app.render({}, ready).operations if isinstance(operation, Text)]
        self.assertIn("READY CHECK", ready_labels)

        terminal = _context(_battle(phase="finished", winner_player_id="player_ada"))
        finished = app.reduce({}, ButtonEvent.from_raw(Button.A), terminal)
        self.assertEqual(finished.navigate_to, "home")
        terminal_labels = [operation.text for operation in app.render({}, terminal).operations if isinstance(operation, Text)]
        self.assertIn("YOU WIN!", terminal_labels)

    def test_live_battle_owns_home_but_a_terminal_battle_releases_it(self) -> None:
        ui = BadgeUi.standard()
        active = _context(_battle())
        session = BadgeSessionState(active_app="battle", app_states={"battle": {"move_index": 0}})
        kept = ui.dispatch(session, Button.HOME, active)
        self.assertEqual(kept.active_app, "battle")

        finished = _context(_battle(phase="timed_out", active_player_id=None))
        returned = ui.dispatch(session, Button.HOME, finished)
        self.assertEqual(returned.active_app, "home")

    def test_disconnect_is_a_locked_pause_not_a_terminal_acknowledgement(self) -> None:
        app = BattleApp()
        paused = _context(
            _battle(
                phase="disconnected",
                active_player_id="player_ada",
                input_locked=True,
                notice="Waiting for a player to reconnect.",
            )
        )
        self.assertFalse(paused.battle.is_terminal if paused.battle else True)
        self.assertIsNone(app.requested_action({}, ButtonEvent.from_raw(Button.A), paused))
        labels = [operation.text for operation in app.render({}, paused).operations if isinstance(operation, Text)]
        self.assertIn("RECONNECTING PLAYER", labels)
        self.assertIn("CONTROLS LOCKED", labels)


if __name__ == "__main__":
    unittest.main()
