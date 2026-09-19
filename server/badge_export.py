"""Export a server world snapshot into the badge's compact text protocol."""

from __future__ import annotations

from pathlib import Path

from models import WorldSnapshot

MAX_BADGE_POKEMON = 16
MAX_BADGE_EVENTS = 4


def motion_duration_ms(activity: str) -> int:
    """Give the badge a pleasant interpolation time for the current action."""

    activity = activity.lower()
    if "play" in activity:
        return 700
    if "challenge" in activity or "testing" in activity:
        return 900
    if "conversation" in activity or "talking" in activity:
        return 1100
    if "breather" in activity or "rest" in activity:
        return 2200
    return 1400


def record_field(value: object, limit: int = 160) -> str:
    """Make a value safe for one pipe-delimited, one-line badge record."""

    text = str(value).replace("|", "/").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def badge_snapshot_text(world: WorldSnapshot) -> str:
    """Return the complete snapshot which must be written before inbox.ready."""

    lines = [f"revision={world.revision}"]
    # The Lua reader has a 16 KiB file-read limit. These caps keep a complete
    # Habitat snapshot comfortably below it without sending unused relations.
    for pokemon in world.pokemon[:MAX_BADGE_POKEMON]:
        state = world.states.get(pokemon.pokemon_id)
        sprite_key = pokemon.sprite_key or f"{pokemon.pokemon_id}_v1"
        lines.append(
            "pokemon|" + "|".join(
                [
                    record_field(pokemon.pokemon_id, 32),
                    record_field(pokemon.name, 32),
                    pokemon.caught_at.date().isoformat(),
                    record_field(pokemon.species, 48),
                    record_field(pokemon.element, 16),
                    str(pokemon.stats.hp),
                    str(pokemon.stats.attack),
                    str(pokemon.stats.defense),
                    str(pokemon.stats.speed),
                    record_field(pokemon.rarity, 12),
                    record_field(pokemon.flavour, 72),
                    record_field(sprite_key, 48),
                ]
            )
        )
        if state:
            lines.append(
                "state|" + "|".join(
                    [
                        record_field(pokemon.pokemon_id, 32),
                        str(state.x),
                        str(state.y),
                        record_field(state.mood, 24),
                        str(state.energy),
                        record_field(state.activity, 36),
                    ]
                )
            )
            # `state` stays backward-compatible. Newer Habitat clients use
            # this separate record as the next logical destination.
            lines.append(
                "motion|" + "|".join(
                    [
                        record_field(pokemon.pokemon_id, 32),
                        str(state.x),
                        str(state.y),
                        str(motion_duration_ms(state.activity)),
                    ]
                )
            )

    # Relationship records are intentionally omitted: the current badge UI
    # never consumes them, while recent dialogue is materially more useful.
    for event in world.events[-MAX_BADGE_EVENTS:]:
        lines.append(
            "event|" + "|".join(
                [
                    record_field(event.event_id, 32),
                    str(event.revision),
                    record_field(event.actor_id, 32),
                    record_field(event.target_id, 32),
                    record_field(event.kind, 24),
                    record_field(event.summary, 88),
                ]
            )
        )
        for dialogue in event.dialogue[:2]:
            lines.append(
                "dialogue|" + "|".join(
                    [
                        record_field(event.event_id, 32),
                        record_field(dialogue.speaker_id, 32),
                        record_field(dialogue.text, 72),
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
