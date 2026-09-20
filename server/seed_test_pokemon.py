"""Create one idempotent test Pokemon for a configured HTN badge.

The automatic ``SHUTTERDEX_BADGES`` bootstrap derives the same player ID from
the HTN-ID, so this can safely be run before the badge has connected. Once the
server starts with that badge in ``.env``, its Dex and Habitat will own this
test creature.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path

from badge_store import BadgeStore, pokemon_from_dict
from sprite_assets import SpriteStore


SERVER_DIR = Path(__file__).resolve().parent
SAMPLE_SPRITE = SERVER_DIR / "assets" / "sprites" / "coilkit-v1.png"


def player_id_for(htn_id: str) -> str:
    return f"configured_{sha256(htn_id.encode('utf-8')).hexdigest()[:20]}"


def seed(htn_id: str, data_dir: Path) -> str:
    htn_id = htn_id.strip()
    if not htn_id:
        raise ValueError("--htn-id cannot be blank")
    owner_player_id = player_id_for(htn_id)
    pokemon_id = f"test_{sha256(htn_id.encode('utf-8')).hexdigest()[:16]}"
    store = BadgeStore(data_dir / "shutterdex.sqlite3")
    try:
        if store.get_player(owner_player_id) is None:
            store.create_player(f"Badge {htn_id}", player_id=owner_player_id)
        created = store.get_pokemon(pokemon_id) is None
        if created:
            store.create_pokemon(
                pokemon_from_dict(
                    {
                        "pokemon_id": pokemon_id,
                        "name": "Voltfin",
                        "species": "lantern-tailed river creature",
                        "type": "electric",
                        "stats": {"hp": 68, "attack": 62, "defense": 55, "speed": 92},
                        "moves": ["spark_splash", "ripple_dash", "glow_pulse"],
                        "flavour": "It lights up whenever it thinks it has discovered a secret.",
                        "sprite_prompt": "a small cobalt river creature with a glowing yellow lantern tail",
                        "rarity": "common",
                        "metadata": {
                            "personality": {
                                "curiosity": 88,
                                "sociability": 72,
                                "bravery": 51,
                                "competitiveness": 44,
                            }
                        },
                    },
                    owner_player_id=owner_player_id,
                )
            )
        if not SAMPLE_SPRITE.is_file():
            raise RuntimeError(f"Sample sprite is missing: {SAMPLE_SPRITE}")
        sprite_store = SpriteStore(data_dir / "sprites")
        sprite_key = sprite_store.save_png(pokemon_id, SAMPLE_SPRITE.read_bytes())
        store.update_pokemon_sprite(
            pokemon_id,
            sprite_key,
            expected_owner_player_id=owner_player_id,
        )
        action = "Created" if created else "Updated"
        return f"{action} Voltfin ({pokemon_id}) with sample sprite {sprite_key}."
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a Shutterdex test Pokemon.")
    parser.add_argument("--htn-id", required=True, help="Public five-character HTN badge ID")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=SERVER_DIR / "data",
        help="Directory containing shutterdex.sqlite3",
    )
    args = parser.parse_args()
    print(seed(args.htn_id, args.data_dir))


if __name__ == "__main__":
    main()
