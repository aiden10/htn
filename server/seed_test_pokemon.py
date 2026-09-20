"""Create two idempotent Habitat test Pokemon for a configured HTN badge.

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
SPRITE_DIR = SERVER_DIR / "assets" / "sprites"


def player_id_for(htn_id: str) -> str:
    return f"configured_{sha256(htn_id.encode('utf-8')).hexdigest()[:20]}"


def _test_pokemon_specs(htn_id: str) -> tuple[tuple[str, str, Path, dict[str, object]], ...]:
    """Return two stable IDs so rerunning this script never duplicates a Dex."""

    digest = sha256(htn_id.encode("utf-8")).hexdigest()
    return (
        (
            f"test_{digest[:16]}",
            "Voltfin",
            SPRITE_DIR / "coilkit-v1.png",
            {
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
        ),
        (
            f"test_{digest[:12]}_mossbyte",
            "Mossbyte",
            SPRITE_DIR / "mossbyte-v1.png",
            {
                "name": "Mossbyte",
                "species": "moss-covered field calculator",
                "type": "earth",
                "stats": {"hp": 92, "attack": 48, "defense": 104, "speed": 34},
                "moves": ["root_sum", "moss_screen", "factor_fall"],
                "flavour": "It solves problems slowly, then refuses to explain its working.",
                "sprite_prompt": "a moss-covered pocket calculator creature with pebble feet",
                "rarity": "common",
                "metadata": {
                    "personality": {
                        "curiosity": 47,
                        "sociability": 36,
                        "bravery": 69,
                        "competitiveness": 74,
                    }
                },
            },
        ),
    )


def seed(htn_id: str, data_dir: Path) -> str:
    htn_id = htn_id.strip()
    if not htn_id:
        raise ValueError("--htn-id cannot be blank")
    owner_player_id = player_id_for(htn_id)
    store = BadgeStore(data_dir / "shutterdex.sqlite3")
    try:
        if store.get_player(owner_player_id) is None:
            store.create_player(f"Badge {htn_id}", player_id=owner_player_id)
        sprite_store = SpriteStore(data_dir / "sprites")
        results: list[str] = []
        for pokemon_id, name, sprite_path, pokemon_data in _test_pokemon_specs(htn_id):
            created = store.get_pokemon(pokemon_id) is None
            if created:
                store.create_pokemon(
                    pokemon_from_dict(
                        {"pokemon_id": pokemon_id, **pokemon_data},
                        owner_player_id=owner_player_id,
                    )
                )
            if not sprite_path.is_file():
                raise RuntimeError(f"Sample sprite is missing: {sprite_path}")
            sprite_key = sprite_store.save_png(pokemon_id, sprite_path.read_bytes())
            store.update_pokemon_sprite(
                pokemon_id,
                sprite_key,
                expected_owner_player_id=owner_player_id,
            )
            action = "Created" if created else "Updated"
            results.append(f"{action} {name} ({pokemon_id}) with {sprite_key}")
        return "; ".join(results) + "."
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed two Shutterdex Habitat test Pokemon.")
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
