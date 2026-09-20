"""Regression coverage for battle-ready Poké Ball captures."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from pydantic import ValidationError


SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
IMAGE_PROCESSING_ROOT = PROJECT_ROOT / "image-processing"
for path in (SERVER_ROOT, IMAGE_PROCESSING_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import generate  # noqa: E402
import main  # noqa: E402
from badge_store import BadgeStore  # noqa: E402


class CaptureContractTests(unittest.TestCase):
    def test_generator_repairs_invalid_moves_and_sparse_natures(self) -> None:
        creature = generate.validate(
            {
                "name": "Ticktockle",
                "species": "clock",
                "type": "circuit",
                "stats": {"hp": 100, "attack": 100, "defense": 100, "speed": 100},
                "moves": ["overclock", "made up move", "overclock"],
                "battle_natures": ["clockwork", "not a real tag", "clockwork"],
                "flavour": "It always knows when the kettle is done.",
                "rarity": "common",
                "sprite_prompt": "a small brass clock creature with spring legs",
            }
        )

        self.assertEqual(len(creature["moves"]), 4)
        self.assertEqual(len(set(creature["moves"])), 4)
        self.assertTrue(all(move in generate.G.MOVES for move in creature["moves"]))
        self.assertGreaterEqual(
            sum(
                generate.G.MOVES[move]["type"] == creature["type"]
                for move in creature["moves"]
            ),
            2,
        )
        self.assertGreaterEqual(len(creature["battle_natures"]), 2)
        self.assertLessEqual(len(creature["battle_natures"]), 4)
        self.assertEqual(len(creature["battle_natures"]), len(set(creature["battle_natures"])))
        self.assertIn("clockwork", creature["battle_natures"])
        self.assertIn("temporal", creature["battle_natures"])

    def test_generator_caps_type_fallback_tags(self) -> None:
        """Type defaults alone must not expand to the full tag catalogue."""

        self.assertEqual(
            generate.normalise_battle_natures([], "verdant", "moss"),
            ["verdant", "rooted"],
        )

    def test_capture_profile_rejects_incomplete_battle_loadout(self) -> None:
        creature = {
            "name": "Shortstack",
            "species": "small box",
            "type": "stone",
            "stats": {"hp": 80, "attack": 80, "defense": 120, "speed": 60},
            "moves": ["pebble_toss"],
            "battle_natures": ["heavy"],
            "flavour": "It refuses to be packed away.",
            "rarity": "common",
            "sprite_prompt": "a compact cardboard box creature with sturdy legs",
        }

        with self.assertRaises(ValidationError):
            main.pokeball_profile(creature)

    def test_existing_database_gains_battle_nature_column(self) -> None:
        """A running hackathon database must migrate without a reset."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """
                CREATE TABLE pokemon (
                    pokemon_id TEXT PRIMARY KEY,
                    owner_player_id TEXT NOT NULL,
                    captured_by_badge_id TEXT,
                    name TEXT NOT NULL,
                    species TEXT NOT NULL,
                    types_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    moves_json TEXT NOT NULL,
                    flavour TEXT NOT NULL,
                    sprite_prompt TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    sprite_path TEXT,
                    caught_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            store = BadgeStore(database_path)
            try:
                columns = {
                    row["name"]
                    for row in store.connection.execute("PRAGMA table_info(pokemon)").fetchall()
                }
            finally:
                store.close()

        self.assertIn("battle_natures_json", columns)


if __name__ == "__main__":
    unittest.main()
