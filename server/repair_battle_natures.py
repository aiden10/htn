"""Repair legacy Shutterdex Pokemon so every battle roster is valid.

Earlier captures predate the battle contract and can have no battle-nature
tags, a single tag, an unbounded model list, or fewer than four moves. This
script derives two to four stable material/nature tags from each Pokemon's
stored type and species, then fills only any missing legacy move slots. It is
deliberately offline: no image, Backboard, or capture data is regenerated.

Run from the ``server`` directory:

    python repair_battle_natures.py --dry-run
    python repair_battle_natures.py

Use ``--force`` only if you deliberately want to replace valid existing
two-to-four-tag profiles with the deterministic type/species recommendation.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import re

from badge_store import BadgeStore, PokemonRecord


SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SERVER_DIR / "data" / "shutterdex.sqlite3"

# Keep this catalogue aligned with ``image-processing/game_data.py`` but do
# not import the capture pipeline: a database repair should work even when its
# optional vision/sprite dependencies are not installed.
KNOWN_TAGS = frozenset(
    {
        "absorbent",
        "buoyant",
        "ceramic",
        "clockwork",
        "conductive",
        "elastic",
        "flammable",
        "fragile",
        "frozen",
        "heated",
        "heavy",
        "insulated",
        "liquid",
        "magnetic",
        "metallic",
        "porous",
        "reflective",
        "rooted",
        "sharp",
        "temporal",
        "verdant",
    }
)

TYPE_TAGS = {
    "ember": ("heated", "flammable"),
    "tide": ("liquid", "buoyant"),
    "verdant": ("verdant", "rooted"),
    "circuit": ("conductive", "magnetic"),
    "stone": ("heavy", "porous"),
    # Names used by the older HTN/Lua prototype and hand-authored test data.
    "electric": ("conductive", "magnetic"),
    "water": ("liquid", "buoyant"),
    "earth": ("heavy", "porous"),
    "grass": ("verdant", "rooted"),
    "moss": ("verdant", "absorbent"),
    "fire": ("heated", "flammable"),
    "ice": ("frozen", "reflective"),
    "metal": ("metallic", "magnetic"),
}

TYPE_MOVES = {
    "circuit": ("arc_lash", "static_field", "overclock", "short_circuit"),
    "electric": ("arc_lash", "static_field", "overclock", "short_circuit"),
    "tide": ("ripple_dash", "current_call", "splash_guard", "undertow"),
    "water": ("ripple_dash", "current_call", "splash_guard", "undertow"),
    "verdant": ("root_sum", "moss_screen", "vine_snare", "spore_burst"),
    "grass": ("root_sum", "moss_screen", "vine_snare", "spore_burst"),
    "moss": ("root_sum", "moss_screen", "vine_snare", "spore_burst"),
    "stone": ("fault_line", "stone_wall", "grounded_shift", "boulder_roll"),
    "earth": ("fault_line", "stone_wall", "grounded_shift", "boulder_roll"),
    "ember": ("ember_pop", "heat_wave", "cinder_guard", "flare_dash"),
    "fire": ("ember_pop", "heat_wave", "cinder_guard", "flare_dash"),
    "metal": ("gear_jolt", "iron_guard", "magnet_pull", "alloy_rush"),
}

SPECIES_HINTS = {
    "clock": ("clockwork", "temporal"),
    "watch": ("clockwork", "temporal"),
    "gear": ("clockwork", "metallic"),
    "spring": ("clockwork", "elastic"),
    "magnet": ("magnetic", "metallic"),
    "battery": ("conductive", "insulated"),
    "wire": ("conductive", "metallic"),
    "glass": ("fragile", "reflective"),
    "lens": ("fragile", "reflective"),
    "mirror": ("fragile", "reflective"),
    "mug": ("ceramic", "heavy"),
    "cup": ("ceramic", "porous"),
    "ceramic": ("ceramic", "fragile"),
    "ice": ("frozen", "reflective"),
    "rubber": ("elastic", "insulated"),
    "sponge": ("absorbent", "porous"),
    "leaf": ("verdant", "rooted"),
    "plant": ("verdant", "rooted"),
    "moss": ("verdant", "absorbent"),
    "fin": ("liquid", "buoyant"),
    "fish": ("liquid", "buoyant"),
    "aquatic": ("liquid", "buoyant"),
}


def slug(value: object) -> str:
    """Normalize a user/model-supplied tag without accepting punctuation."""

    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def repaired_natures(pokemon: PokemonRecord) -> tuple[str, ...]:
    """Return 2--4 deterministic catalogued tags for one legacy record."""

    result: list[str] = []

    def add(tag: str) -> None:
        if tag in KNOWN_TAGS and tag not in result and len(result) < 4:
            result.append(tag)

    # Preserve meaningful tags that are already in the supported catalogue.
    for tag in pokemon.battle_natures:
        add(slug(tag))

    species = slug(pokemon.species)
    # Specific material hints precede broad type behavior. For example, a
    # ceramic electric mug stays ceramic rather than becoming generic metal.
    for keyword, tags in SPECIES_HINTS.items():
        if keyword in species:
            for tag in tags:
                add(tag)
    for pokemon_type in pokemon.types:
        for tag in TYPE_TAGS.get(slug(pokemon_type), ()):
            add(tag)

    # A malformed unknown type/species still becomes battle-ready in a stable
    # way. The fixed fallback order is intentionally boring and reproducible.
    for tag in sorted(KNOWN_TAGS):
        if len(result) >= 2:
            break
        add(tag)
    return tuple(result[:4])


def repaired_moves(pokemon: PokemonRecord) -> tuple[str, ...]:
    """Keep legacy move names and fill only the missing battle slots."""

    result: list[str] = []
    for move in pokemon.moves:
        normalized = slug(move)
        if normalized and normalized not in result and len(result) < 4:
            result.append(normalized)
    for pokemon_type in pokemon.types:
        for move in TYPE_MOVES.get(slug(pokemon_type), ()):
            if move not in result and len(result) < 4:
                result.append(move)
    for move in ("steady_step", "focus_guard", "wild_swing", "last_resort"):
        if move not in result and len(result) < 4:
            result.append(move)
    return tuple(result[:4])


def has_valid_natures(pokemon: PokemonRecord) -> bool:
    tags = tuple(slug(tag) for tag in pokemon.battle_natures)
    return 2 <= len(tags) <= 4 and len(set(tags)) == len(tags) and all(
        tag in KNOWN_TAGS for tag in tags
    )


def has_valid_moves(pokemon: PokemonRecord) -> bool:
    moves = tuple(slug(move) for move in pokemon.moves)
    return len(moves) == 4 and len(set(moves)) == 4 and all(moves)


def repair_database(database: Path, *, dry_run: bool, force: bool) -> tuple[int, int]:
    """Repair rows and return ``(changed, skipped)`` for automation/tests."""

    changed = 0
    skipped = 0
    with BadgeStore(database) as store:
        for pokemon in store.list_pokemon():
            if has_valid_natures(pokemon) and has_valid_moves(pokemon) and not force:
                skipped += 1
                continue
            natures = repaired_natures(pokemon)
            moves = repaired_moves(pokemon)
            if tuple(pokemon.battle_natures) == natures and tuple(pokemon.moves) == moves:
                skipped += 1
                continue
            print(
                f"{pokemon.pokemon_id} ({pokemon.name}): "
                f"tags [{', '.join(pokemon.battle_natures) or 'none'}] -> [{', '.join(natures)}]; "
                f"moves [{', '.join(pokemon.moves) or 'none'}] -> [{', '.join(moves)}]"
            )
            if not dry_run:
                store.replace_pokemon(
                    replace(pokemon, battle_natures=natures, moves=moves),
                    expected_owner_player_id=pokemon.owner_player_id,
                )
            changed += 1
    return changed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair Shutterdex battle-nature tags.")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite database path (default: server/data/shutterdex.sqlite3)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing them.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace even already-valid two-to-four-tag profiles.",
    )
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"Database not found: {args.database}")
    changed, skipped = repair_database(args.database, dry_run=args.dry_run, force=args.force)
    verb = "Would repair" if args.dry_run else "Repaired"
    print(f"{verb} {changed} Pokemon; left {skipped} valid/unchanged.")


if __name__ == "__main__":
    main()
