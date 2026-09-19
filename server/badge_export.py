"""Export a server world snapshot into the badge's compact text protocol."""

from __future__ import annotations

from pathlib import Path

from models import WorldSnapshot


def record_field(value: object, limit: int = 160) -> str:
    """Make a value safe for one pipe-delimited, one-line badge record."""

    text = str(value).replace("|", "/").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def badge_snapshot_text(world: WorldSnapshot) -> str:
    """Return the complete snapshot which must be written before inbox.ready."""

    lines = [f"revision={world.revision}"]
    for pokemon in world.pokemon:
        state = world.states.get(pokemon.pokemon_id)
        sprite_key = pokemon.sprite_key or f"{pokemon.pokemon_id}_v1"
        lines.append(
            "pokemon|" + "|".join(
                [
                    record_field(pokemon.pokemon_id, 48),
                    record_field(pokemon.name, 48),
                    pokemon.caught_at.date().isoformat(),
                    record_field(pokemon.species, 80),
                    record_field(pokemon.element, 32),
                    str(pokemon.stats.hp),
                    str(pokemon.stats.attack),
                    str(pokemon.stats.defense),
                    str(pokemon.stats.speed),
                    record_field(pokemon.rarity, 16),
                    record_field(pokemon.flavour, 160),
                    record_field(sprite_key, 64),
                ]
            )
        )
        if state:
            lines.append(
                "state|" + "|".join(
                    [
                        record_field(pokemon.pokemon_id, 48),
                        str(state.x),
                        str(state.y),
                        record_field(state.mood, 32),
                        str(state.energy),
                        record_field(state.activity, 48),
                    ]
                )
            )

    for relationship in world.relationships:
        lines.append(
            "relationship|" + "|".join(
                [
                    record_field(relationship.first_id, 48),
                    record_field(relationship.second_id, 48),
                    str(relationship.friendship),
                    str(relationship.rivalry),
                ]
            )
        )

    # The badge app renders the newest event last and limits itself to 12.
    for event in world.events[-12:]:
        lines.append(
            "event|" + "|".join(
                [
                    record_field(event.event_id, 48),
                    str(event.revision),
                    record_field(event.actor_id, 48),
                    record_field(event.target_id, 48),
                    record_field(event.kind, 32),
                    record_field(event.summary, 160),
                ]
            )
        )
        for dialogue in event.dialogue[:2]:
            lines.append(
                "dialogue|" + "|".join(
                    [
                        record_field(event.event_id, 48),
                        record_field(dialogue.speaker_id, 48),
                        record_field(dialogue.text, 160),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


def write_badge_mirror(world: WorldSnapshot, destination: Path) -> None:
    """Write a local bridge mirror using the same publish order as the badge.

    `destination` is normally a mounted/bridged app directory. The physical
    badge does not mount itself automatically, so a USB bridge still owns the
    actual device transfer.
    """

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "inbox.tmp").write_text(badge_snapshot_text(world), encoding="utf-8", newline="\n")
    (destination / "inbox.ready").write_text(f"revision={world.revision}\n", encoding="utf-8", newline="\n")
