"""Regression coverage for camera captures entering player-owned Shutterdex."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

import main
from badge_store import BadgeStore
from sprite_assets import SpriteStore


class RecordingRuntime:
    def __init__(self) -> None:
        self.refreshed_players: list[str] = []
        self.capture_loading_calls: list[tuple[str, str, bool | None]] = []

    async def refresh_player(self, player_id: str) -> tuple[()]:
        self.refreshed_players.append(player_id)
        return ()

    async def begin_capture_loading(self, htn_id: str) -> None:
        self.capture_loading_calls.append(("begin", htn_id, None))

    async def end_capture_loading(self, htn_id: str, *, restore: bool) -> None:
        self.capture_loading_calls.append(("end", htn_id, restore))


class PokeballCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.game_data = root / "gamedata"
        self.game_data.mkdir()
        self.store = BadgeStore(root / "shutterdex.sqlite3")
        self.sprites = SpriteStore(root / "sprites")
        self.runtime = RecordingRuntime()
        player = self.store.create_player("Camera Player", player_id="camera_player")
        self.badge = self.store.create_badge(
            "cam01", "opaque-test-key", player_id=player.player_id, badge_id="badge_cam01"
        )
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                shutterdex_store=self.store,
                sprites=self.sprites,
                shutterdex_runtime=self.runtime,
                pokeball_generation_lock=asyncio.Lock(),
            )
        )
        self._game_data_patch = patch.object(main, "POKEBALL_GAME_DATA_DIR", self.game_data)
        self._game_data_patch.start()

    async def asyncTearDown(self) -> None:
        self._game_data_patch.stop()
        self.store.close()
        self._temporary_directory.cleanup()

    async def test_generated_camera_creature_is_owned_by_configured_badges_player(self) -> None:
        sprite_path = self.game_data / "test_sprite.png"
        image = Image.new("RGBA", (48, 48), "#5bc0eb")
        image.save(sprite_path, format="PNG")
        creature = {
            "name": "Snapfin",
            "species": "camera lens",
            "type": "tide",
            "stats": {"hp": 72, "attack": 65, "defense": 58, "speed": 91},
            "moves": ["drizzle_jab", "undertow", "tidal_slam", "rinse"],
            "battle_natures": ["fragile", "reflective", "liquid"],
            "flavour": "It frames every puddle like a masterpiece.",
            "sprite_prompt": "round blue lens creature with fin-like shutters",
            "rarity": "uncommon",
            "sprite": sprite_path.name,
        }

        await main.store_pokeball_creature(
            self.app,
            capture_id="capture_test_01",
            capture_badge=self.badge,
            creature=creature,
        )

        pokemon = self.store.list_pokemon_for_player("camera_player")
        self.assertEqual(len(pokemon), 1)
        self.assertEqual(pokemon[0].name, "Snapfin")
        self.assertEqual(pokemon[0].captured_by_badge_id, self.badge.badge_id)
        self.assertEqual(pokemon[0].metadata["source"], "pokeball-camera")
        self.assertEqual(len(pokemon[0].moves), 4)
        self.assertEqual(pokemon[0].battle_natures, ("fragile", "reflective", "liquid"))
        self.assertEqual(self.runtime.refreshed_players, ["camera_player"])
        assert pokemon[0].sprite_path is not None
        self.assertTrue(self.sprites.source_path(pokemon[0].sprite_path).is_file())

    async def test_processing_keeps_only_capture_badge_loading_until_saved(self) -> None:
        creature = {
            "name": "Blinkbud",
            "species": "camera flash flower",
            "type": "light",
            "stats": {"hp": 40, "attack": 63, "defense": 47, "speed": 88},
            "moves": ["cinder_flick", "scorch_wave", "flare_charge", "heat_haze"],
            "battle_natures": ["heated", "fragile"],
            "flavour": "It opens only when someone smiles for a picture.",
            "sprite_prompt": "small glowing flower creature",
            "rarity": "common",
        }
        photo_path = Path(self._temporary_directory.name) / "photo.jpg"
        photo_path.write_bytes(b"\xff\xd8test-image\xff\xd9")

        with patch.object(main, "process_pokeball_photo", return_value=creature):
            await main.process_and_store_pokeball_capture(
                self.app,
                capture_id="capture_test_02",
                photo_path=photo_path,
                capture_badge=self.badge,
            )

        self.assertEqual(
            self.runtime.capture_loading_calls,
            [("begin", "cam01", None), ("end", "cam01", True)],
        )
        self.assertEqual(
            [record.name for record in self.store.list_pokemon_for_player("camera_player")],
            ["Blinkbud"],
        )


if __name__ == "__main__":
    unittest.main()
