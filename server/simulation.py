"""Persistent, server-authoritative Pokemon simulation with an optional Jev director."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Literal

try:
    from backboard import BackboardClient
except ImportError:  # The rest of the local server remains usable before pip install.
    BackboardClient = None  # type: ignore[assignment,misc]

from badge_export import write_badge_mirror
from models import (
    DialogueLine,
    Pokemon,
    PokemonPersonality,
    PokemonRelationship,
    PokemonSimulationState,
    PokemonStats,
    SimulationEvent,
    WorldSnapshot,
    utc_now,
)


INTERACTIONS = ("observe", "greet", "play", "challenge", "rest")


class WorldStore:
    """A one-row SQLite world document, saved transactionally at each revision."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS world_snapshot (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def load(self) -> WorldSnapshot:
        row = self.connection.execute(
            "SELECT payload FROM world_snapshot WHERE singleton = 1"
        ).fetchone()
        return WorldSnapshot() if row is None else WorldSnapshot.model_validate_json(row[0])

    def save(self, world: WorldSnapshot) -> None:
        payload = world.model_dump_json(by_alias=True)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO world_snapshot(singleton, payload) VALUES(1, ?)
                ON CONFLICT(singleton) DO UPDATE SET payload = excluded.payload
                """,
                (payload,),
            )

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class SimulationDecision:
    kind: str
    bond_delta: int
    source: Literal["fallback", "jev"]
    note: str | None = None


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _state_for(world: WorldSnapshot, pokemon: Pokemon) -> PokemonSimulationState:
    state = world.states.get(pokemon.pokemon_id)
    if state is None:
        state = PokemonSimulationState(pokemon_id=pokemon.pokemon_id)
        world.states[pokemon.pokemon_id] = state
    return state


def _relationship_for(
    world: WorldSnapshot, first_id: str, second_id: str
) -> PokemonRelationship | None:
    if not second_id:
        return None
    for relationship in world.relationships:
        if {relationship.first_id, relationship.second_id} == {first_id, second_id}:
            return relationship
    relationship = PokemonRelationship(first_id=first_id, second_id=second_id)
    world.relationships.append(relationship)
    return relationship


class SimulationService:
    """Owns state changes; the badge is only a consumer of exported revisions."""

    def __init__(
        self,
        store: WorldStore,
        *,
        backboard_api_key: str | None = None,
        badge_mirror_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.backboard_api_key = backboard_api_key
        self.badge_mirror_dir = badge_mirror_dir
        self.lock = asyncio.Lock()

    def _persist(self, world: WorldSnapshot) -> None:
        self.store.save(world)
        if self.badge_mirror_dir is not None:
            write_badge_mirror(world, self.badge_mirror_dir)

    async def snapshot(self) -> WorldSnapshot:
        async with self.lock:
            return self.store.load()

    async def add_pokemon(
        self,
        pokemon: Pokemon,
        *,
        personality: PokemonPersonality | None = None,
    ) -> WorldSnapshot:
        async with self.lock:
            world = self.store.load()
            if any(item.pokemon_id == pokemon.pokemon_id for item in world.pokemon):
                raise ValueError(f"Pokemon ID {pokemon.pokemon_id!r} already exists.")
            world.pokemon.append(pokemon)
            world.states[pokemon.pokemon_id] = PokemonSimulationState(
                pokemon_id=pokemon.pokemon_id,
                personality=personality or PokemonPersonality(),
            )
            world.revision += 1
            self._persist(world)
            return world

    async def seed_demo_world(self) -> WorldSnapshot:
        """Create a two-Pokemon world for an immediate end-to-end badge test."""

        async with self.lock:
            world = self.store.load()
            if world.pokemon:
                return world

            mugmite = Pokemon(
                pokemon_id="mon_mugmite",
                name="Mugmite",
                species="ceramic drinking vessel",
                element="earth",
                stats=PokemonStats(hp=110, attack=70, defense=130, speed=90),
                moves=["chip_shot", "steam_vent", "handle_bash", "glaze_over"],
                flavour="Fiercely territorial about its coaster.",
                sprite_prompt=(
                    "a squat round ceramic creature with a curved handle arm, "
                    "chipped rim, stubby legs, steam wisps"
                ),
                rarity="common",
            )
            spriglet = Pokemon(
                pokemon_id="mon_spriglet",
                name="Spriglet",
                species="seedling Pokemon",
                element="grass",
                stats=PokemonStats(hp=72, attack=58, defense=64, speed=76),
                moves=["leaf_peek", "sprout_dash", "root_tap", "pollen_puff"],
                flavour="Collects unusual leaves and asks too many questions.",
                sprite_prompt="a tiny bright green seedling creature with leaf ears and a curious face",
                rarity="common",
            )
            world.pokemon = [mugmite, spriglet]
            world.states = {
                mugmite.pokemon_id: PokemonSimulationState(
                    pokemon_id=mugmite.pokemon_id,
                    x=28,
                    y=66,
                    mood="alert",
                    energy=82,
                    activity="guarding coaster",
                    personality=PokemonPersonality(
                        curiosity=34, sociability=42, bravery=81, competitiveness=78
                    ),
                ),
                spriglet.pokemon_id: PokemonSimulationState(
                    pokemon_id=spriglet.pokemon_id,
                    x=72,
                    y=44,
                    mood="curious",
                    energy=74,
                    activity="examining leaves",
                    personality=PokemonPersonality(
                        curiosity=91, sociability=73, bravery=48, competitiveness=22
                    ),
                ),
            }
            world.relationships = [
                PokemonRelationship(
                    first_id=mugmite.pokemon_id,
                    second_id=spriglet.pokemon_id,
                    friendship=31,
                    rivalry=8,
                )
            ]
            world.revision = 1
            world.events = [
                SimulationEvent(
                    event_id="evt_1_01",
                    revision=1,
                    actor_id=mugmite.pokemon_id,
                    target_id=spriglet.pokemon_id,
                    kind="greet",
                    summary="Mugmite challenged Spriglet near its coaster.",
                    dialogue=[
                        DialogueLine(speaker_id=mugmite.pokemon_id, text="This spot is taken."),
                        DialogueLine(speaker_id=spriglet.pokemon_id, text="I was only looking."),
                    ],
                )
            ]
            self._persist(world)
            return world

    async def reset_test_world(self) -> WorldSnapshot:
        """Replace this service's world with harmless dummy Pokemon for testing.

        The FastAPI app creates a separate service/database for this method, so
        this reset never touches the real capture/simulation world.
        """

        async with self.lock:
            coilkit = Pokemon(
                pokemon_id="test_coilkit",
                name="Coilkit",
                species="clockwork kitten",
                element="electric",
                stats=PokemonStats(hp=64, attack=78, defense=52, speed=118),
                moves=["static_pounce", "spring_dash", "copper_purr", "bolt_ball"],
                flavour="Keeps a meticulous collection of shiny screws.",
                sprite_prompt="a small brass clockwork kitten with a coiled tail and blue sparks",
                rarity="common",
            )
            world = WorldSnapshot(
                revision=1,
                pokemon=[coilkit],
                states={
                    coilkit.pokemon_id: PokemonSimulationState(
                        pokemon_id=coilkit.pokemon_id,
                        x=22,
                        y=66,
                        mood="restless",
                        energy=88,
                        activity="sorting screws",
                        personality=PokemonPersonality(
                            curiosity=74, sociability=66, bravery=71, competitiveness=39
                        ),
                    ),
                },
                relationships=[],
            )
            self._persist(world)
            return world

    async def attach_sprite(self, pokemon_id: str, sprite_key: str) -> WorldSnapshot:
        async with self.lock:
            world = self.store.load()
            pokemon = next((item for item in world.pokemon if item.pokemon_id == pokemon_id), None)
            if pokemon is None:
                raise KeyError(pokemon_id)
            pokemon.sprite_key = sprite_key
            pokemon.sprite_status = "ready"
            world.revision += 1
            self._persist(world)
            return world

    async def tick(self, prefer_jev: bool = True) -> tuple[WorldSnapshot, SimulationEvent, SimulationDecision]:
        """Commit one complete simulation revision and return its new event."""

        async with self.lock:
            world = self.store.load()
            if not world.pokemon:
                raise ValueError("Add a Pokemon or seed the demo world before simulating.")

            actor_index = world.revision % len(world.pokemon)
            actor = world.pokemon[actor_index]
            target = world.pokemon[(actor_index + 1) % len(world.pokemon)] if len(world.pokemon) > 1 else None
            actor_state = _state_for(world, actor)
            target_state = _state_for(world, target) if target else None

            decision = self._fallback_decision(actor_state, target_state)
            if prefer_jev:
                decision = await self._jev_decision(
                    world, actor, actor_state, target, target_state, decision
                )

            event = self._apply_decision(world, actor, actor_state, target, target_state, decision)
            self._persist(world)
            return world, event, decision

    @staticmethod
    def _fallback_decision(
        actor_state: PokemonSimulationState,
        target_state: PokemonSimulationState | None,
    ) -> SimulationDecision:
        if actor_state.energy < 25:
            return SimulationDecision("rest", 0, "fallback")
        if target_state is None:
            return SimulationDecision("observe", 0, "fallback")
        personality = actor_state.personality
        if personality.competitiveness >= 70:
            return SimulationDecision("challenge", -1, "fallback")
        if personality.sociability >= 65:
            return SimulationDecision("greet", 1, "fallback")
        if personality.curiosity >= 65:
            return SimulationDecision("observe", 1, "fallback")
        return SimulationDecision("play", 1, "fallback")

    async def _jev_decision(
        self,
        world: WorldSnapshot,
        actor: Pokemon,
        actor_state: PokemonSimulationState,
        target: Pokemon | None,
        target_state: PokemonSimulationState | None,
        fallback: SimulationDecision,
    ) -> SimulationDecision:
        """Ask TypeSafe Jev for a constrained simulation choice, never free prose."""

        if not self.backboard_api_key:
            return SimulationDecision(
                fallback.kind,
                fallback.bond_delta,
                "fallback",
                "BACKBOARD_API_KEY is not configured.",
            )
        if BackboardClient is None:
            return SimulationDecision(
                fallback.kind,
                fallback.bond_delta,
                "fallback",
                "backboard-sdk is not installed.",
            )

        relationship = _relationship_for(
            world, actor.pokemon_id, target.pokemon_id if target else ""
        )
        state = {
            "actor": {
                "profile": actor.model_dump(by_alias=True, mode="json"),
                "state": actor_state.model_dump(mode="json"),
            },
            "target": (
                {
                    "profile": target.model_dump(by_alias=True, mode="json"),
                    "state": target_state.model_dump(mode="json") if target_state else {},
                }
                if target
                else None
            ),
            "relationship": relationship.model_dump(mode="json") if relationship else None,
            "recent_events": [event.model_dump(mode="json") for event in world.events[-3:]],
        }
        questions = {
            "next_interaction": {
                "type": "choice",
                "instructions": (
                    "Choose one low-stakes next interaction that best reflects the Pokemon "
                    "profiles, personalities, energy, relationship, and recent events."
                ),
                "criteria": {
                    "observe": "Watch, investigate, or quietly study the surroundings or another Pokemon.",
                    "greet": "Start a cautious or friendly conversation.",
                    "play": "Invite a cooperative, playful shared activity.",
                    "challenge": "Begin a non-destructive competitive contest or territorial disagreement.",
                    "rest": "Pause to recover energy or avoid social pressure.",
                },
            },
            "bond_shift": {
                "type": "score",
                "instructions": "Estimate how this interaction should affect the pair's friendship.",
                "criteria": [
                    "Stronger tension: friendship should drop by two.",
                    "Slight tension: friendship should drop by one.",
                    "Neutral: friendship should not change.",
                    "Warmth: friendship should rise by one.",
                    "Strong connection: friendship should rise by two.",
                ],
            },
        }
        try:
            async with BackboardClient(api_key=self.backboard_api_key) as client:
                response = await client.send_message(
                    "Select the next simulation beat from the supplied structured world state.",
                    llm_provider="typesafe",
                    model_name="jev-latest",
                    stream=False,
                    system_one={"state": state, "questions": questions},
                )
            answers = response.system_one.answers if response.system_one else {}
            interaction = answers.get("next_interaction", {}).get("choice")
            if interaction not in INTERACTIONS:
                raise ValueError("Jev did not return a supported interaction.")
            score = answers.get("bond_shift", {}).get("score", 2)
            bond_delta = _bounded(int(round(float(score))) - 2, -2, 2)
            return SimulationDecision(interaction, bond_delta, "jev")
        except Exception:
            # A flaky AI response must not corrupt or halt the world simulation.
            return SimulationDecision(
                fallback.kind,
                fallback.bond_delta,
                "fallback",
                "Jev was unavailable or returned an invalid decision.",
            )

    def _apply_decision(
        self,
        world: WorldSnapshot,
        actor: Pokemon,
        actor_state: PokemonSimulationState,
        target: Pokemon | None,
        target_state: PokemonSimulationState | None,
        decision: SimulationDecision,
    ) -> SimulationEvent:
        world.revision += 1
        now = datetime.now(timezone.utc)
        kind = decision.kind

        actor_state.updated_at = now
        actor_state.activity = {
            "observe": "watching closely",
            "greet": "starting a conversation",
            "play": "playing together",
            "challenge": "testing a rival",
            "rest": "taking a breather",
        }[kind]
        actor_state.mood = {
            "observe": "curious",
            "greet": "open",
            "play": "happy",
            "challenge": "focused",
            "rest": "calm",
        }[kind]
        energy_cost = {"observe": 1, "greet": 2, "play": 7, "challenge": 6, "rest": -12}[kind]
        actor_state.energy = _bounded(actor_state.energy - energy_cost, 0, 100)
        # A badge snapshot is only published every few seconds, so a tiny
        # coordinate nudge reads as jitter rather than travel.  Keep movement
        # inside the habitat, but make each committed simulation beat visibly
        # distinct and turn before an actor reaches the edge.
        actor_dx = -16 if actor_state.x >= 82 else 14
        actor_dy = -10 if actor_state.y >= 78 else 8
        actor_state.x = _bounded(actor_state.x + actor_dx, 8, 92)
        actor_state.y = _bounded(actor_state.y + actor_dy, 12, 88)

        relationship = _relationship_for(
            world, actor.pokemon_id, target.pokemon_id if target else ""
        )
        if target_state:
            target_state.updated_at = now
            target_state.activity = "talking with " + actor.name if kind == "greet" else target_state.activity
            target_state.mood = "engaged" if kind in {"greet", "play"} else target_state.mood
            target_dx = 14 if target_state.x <= 18 else -12
            target_dy = 8 if target_state.y <= 20 else -6
            target_state.x = _bounded(target_state.x + target_dx, 8, 92)
            target_state.y = _bounded(target_state.y + target_dy, 12, 88)
        if relationship:
            relationship.friendship = _bounded(
                relationship.friendship + decision.bond_delta, -100, 100
            )
            if kind == "challenge":
                relationship.rivalry = _bounded(relationship.rivalry + 2, 0, 100)
            elif kind == "play":
                relationship.rivalry = _bounded(relationship.rivalry - 1, 0, 100)

        summary, dialogue = self._narrative(kind, actor, target)
        event = SimulationEvent(
            revision=world.revision,
            actor_id=actor.pokemon_id,
            target_id=target.pokemon_id if target else "",
            kind=kind,
            summary=summary,
            dialogue=dialogue,
            created_at=now,
            director=decision.source,
        )
        world.events.append(event)
        world.events = world.events[-100:]
        return event

    @staticmethod
    def _narrative(
        kind: str, actor: Pokemon, target: Pokemon | None
    ) -> tuple[str, list[DialogueLine]]:
        if target is None:
            return (
                f"{actor.name} takes a quiet moment to {kind}.",
                [DialogueLine(speaker_id=actor.pokemon_id, text="I need a moment to think.")],
            )

        if kind == "observe":
            return (
                f"{actor.name} studies {target.name}'s habits from a careful distance.",
                [
                    DialogueLine(speaker_id=actor.pokemon_id, text="There is more to you than I expected."),
                    DialogueLine(speaker_id=target.pokemon_id, text="I noticed you watching."),
                ],
            )
        if kind == "greet":
            return (
                f"{actor.name} opens a cautious conversation with {target.name}.",
                [
                    DialogueLine(speaker_id=actor.pokemon_id, text="Your energy is interesting."),
                    DialogueLine(speaker_id=target.pokemon_id, text="Then let us see where this goes."),
                ],
            )
        if kind == "play":
            return (
                f"{actor.name} invites {target.name} into a small shared game.",
                [
                    DialogueLine(speaker_id=actor.pokemon_id, text="First one to the leaf pile wins."),
                    DialogueLine(speaker_id=target.pokemon_id, text="You are on."),
                ],
            )
        if kind == "challenge":
            return (
                f"{actor.name} tests {target.name} with a spirited challenge.",
                [
                    DialogueLine(speaker_id=actor.pokemon_id, text="Show me you can keep up."),
                    DialogueLine(speaker_id=target.pokemon_id, text="I have been waiting for that."),
                ],
            )
        return (
            f"{actor.name} steps back from {target.name} to recover some energy.",
            [
                DialogueLine(speaker_id=actor.pokemon_id, text="I will be back when my steam settles."),
                DialogueLine(speaker_id=target.pokemon_id, text="Take your time."),
            ],
        )
