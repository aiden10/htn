"""Per-player, server-authoritative Habitat simulation for Shutterdex.

The old :mod:`simulation` module stores one shared world document for the
serial/Lua prototype.  This module deliberately does not use it.  Instead,
every tick reads only the requested player's current Pokemon from
``BadgeStore``, writes movement back through ownership-checked store methods,
and appends a player-scoped event.

Every Habitat tick requires Backboard/Jev to choose a small, validated
interaction label. Movement, persistence, and displayed dialogue remain
server-controlled. A missing key, unavailable SDK, network failure, or invalid
model response leaves the world unchanged and reports a recoverable error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from typing import Any, Literal

try:
    from backboard import BackboardClient
except ImportError:  # Convert a missing runtime dependency into a clear tick error.
    BackboardClient = None  # type: ignore[assignment,misc]

from badge_store import (
    BadgeStore,
    NotFoundError,
    OwnershipError,
    PokemonRecord,
    PokemonWorldState,
    SimulationDialogueLine,
    SimulationEventRecord,
    simulation_event_to_dict,
    world_state_to_dict,
)


Interaction = Literal["observe", "greet", "play", "challenge", "rest"]
Director = Literal["jev"]
INTERACTIONS: frozenset[str] = frozenset(
    {"observe", "greet", "play", "challenge", "rest"}
)


class PlayerSimulationError(RuntimeError):
    """A non-secret, user-facing failure in the player Habitat service."""


class EmptyHabitatError(PlayerSimulationError):
    """Raised when a player asks to advance a Habitat with no Pokemon."""


class WorldChangedError(PlayerSimulationError):
    """Raised if a trade changes the collection while a tick is being committed."""


class JevUnavailableError(PlayerSimulationError):
    """Raised when a required Backboard/Jev decision cannot be obtained."""


@dataclass(frozen=True, slots=True)
class Personality:
    """Small, bounded traits supplied to the Jev decision context."""

    curiosity: int
    sociability: int
    bravery: int
    competitiveness: int

    def as_dict(self) -> dict[str, int]:
        return {
            "curiosity": self.curiosity,
            "sociability": self.sociability,
            "bravery": self.bravery,
            "competitiveness": self.competitiveness,
        }


@dataclass(frozen=True, slots=True)
class SimulationDecision:
    """A constrained interaction choice, never arbitrary model text."""

    kind: Interaction
    source: Director
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerSimulationResult:
    """The safe, complete result of one committed player Habitat tick.

    It intentionally contains no credential, prompt, Backboard response, or
    thread ID.  ``world_states`` is the current owner's renderable state after
    the tick; it is safe to pass to the badge UI layer.
    """

    player_id: str
    revision: int
    event: SimulationEventRecord
    world_states: tuple[PokemonWorldState, ...]
    director_used: Director
    director_note: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return an API-safe serialization without model/provider internals."""

        return {
            "player_id": self.player_id,
            "revision": self.revision,
            "event": simulation_event_to_dict(self.event),
            "world_states": [world_state_to_dict(state) for state in self.world_states],
            "director_used": self.director_used,
            "director_note": self.director_note,
        }


class PlayerSimulationService:
    """Advance isolated Habitat worlds stored in :class:`BadgeStore`.

    A service instance owns a lock per player, not one global simulation lock.
    Two different players can therefore advance independently, while duplicate
    button presses for one badge cannot select the same actor/revision.  The
    Wi-Fi runtime should own exactly one instance per FastAPI process.

    ``BadgeStore`` separately verifies ownership for every state/event write.
    Thus a trade racing a tick cannot cause this service to write Player A's
    state or event onto a Pokemon now owned by Player B.
    """

    def __init__(
        self,
        store: BadgeStore,
        *,
        backboard_api_key: str | None = None,
    ) -> None:
        self.store = store
        configured_key = backboard_api_key
        if configured_key is None:
            configured_key = os.environ.get("BACKBOARD_API_KEY")
        self.backboard_api_key = configured_key.strip() if configured_key else None
        self._player_locks: dict[str, asyncio.Lock] = {}
        self._lock_index_lock = asyncio.Lock()

    async def tick(self, player_id: str) -> PlayerSimulationResult:
        """Commit one isolated Habitat update for ``player_id``.

        Each successful event has a Jev-derived interaction. If Jev cannot be
        reached or returns an unsupported answer, no movement or event is
        committed; :class:`JevUnavailableError` is raised instead.
        """

        if not isinstance(player_id, str) or not player_id.strip():
            raise ValueError("player_id must be a non-empty string.")
        lock = await self._lock_for(player_id)
        async with lock:
            return await self._tick_locked(player_id)

    async def _tick_locked(
        self,
        player_id: str,
    ) -> PlayerSimulationResult:
        """Read-plan-commit one tick while the caller holds the player lock."""

        # ``require_player`` makes a nonexistent player an explicit error
        # rather than quietly creating state for an untrusted identifier.
        await asyncio.to_thread(self.store.require_player, player_id)
        pokemon = await asyncio.to_thread(self.store.list_pokemon_for_player, player_id)
        if not pokemon:
            raise EmptyHabitatError("Capture a Pokemon before advancing this Habitat.")

        current_states = {
            state.pokemon_id: state
            for state in await asyncio.to_thread(
                self.store.list_world_states_for_player, player_id
            )
        }
        recent_events = await asyncio.to_thread(
            self.store.list_simulation_events_for_player, player_id, limit=3
        )
        revision = max((event.revision for event in recent_events), default=0) + 1

        states: dict[str, PokemonWorldState] = {}
        new_state_ids: set[str] = set()
        for index, creature in enumerate(pokemon):
            state = current_states.get(creature.pokemon_id)
            if state is None:
                state = self._initial_state(player_id, creature, index)
                new_state_ids.add(creature.pokemon_id)
            states[creature.pokemon_id] = state

        actor_index = (revision - 1) % len(pokemon)
        actor = pokemon[actor_index]
        target = pokemon[(actor_index + 1) % len(pokemon)] if len(pokemon) > 1 else None
        actor_state = states[actor.pokemon_id]
        target_state = states[target.pokemon_id] if target is not None else None
        actor_traits = self._personality_for(actor)
        target_traits = self._personality_for(target) if target is not None else None

        decision = await self._jev_decision(
            player_id=player_id,
            revision=revision,
            actor=actor,
            actor_state=actor_state,
            actor_traits=actor_traits,
            target=target,
            target_state=target_state,
            target_traits=target_traits,
            recent_events=recent_events,
        )

        # Do not apply a response chosen for a Pokemon that was traded while
        # Backboard was thinking. Make the caller retry rather than applying
        # a Jev answer to a changed collection.
        live_pokemon = await asyncio.to_thread(self.store.list_pokemon_for_player, player_id)
        planned_ids = tuple(creature.pokemon_id for creature in pokemon)
        live_ids = tuple(creature.pokemon_id for creature in live_pokemon)
        if live_ids != planned_ids:
            raise WorldChangedError("Collection changed during the Habitat tick; please retry.")

        now = datetime.now(timezone.utc)
        updates = {
            pokemon_id: states[pokemon_id]
            for pokemon_id in new_state_ids
        }
        next_actor_state, next_target_state = self._next_states(
            player_id=player_id,
            revision=revision,
            kind=decision.kind,
            actor=actor,
            actor_state=actor_state,
            target=target,
            target_state=target_state,
            updated_at=now,
        )
        states[actor.pokemon_id] = next_actor_state
        updates[actor.pokemon_id] = next_actor_state
        if target is not None and next_target_state is not None:
            states[target.pokemon_id] = next_target_state
            updates[target.pokemon_id] = next_target_state

        summary, dialogue = self._narrative(
            player_id=player_id,
            revision=revision,
            kind=decision.kind,
            actor=actor,
            target=target,
        )
        event = SimulationEventRecord(
            player_id=player_id,
            revision=revision,
            actor_pokemon_id=actor.pokemon_id,
            target_pokemon_id=target.pokemon_id if target is not None else None,
            kind=decision.kind,
            summary=summary,
            dialogue=dialogue,
            created_at=now,
        )

        try:
            # State changes and their event commit together.  The store checks
            # ownership for every referenced Pokemon in that exact transaction,
            # so a concurrent trade cannot leave a partial Habitat beat.
            event = await asyncio.to_thread(
                self.store.commit_simulation_tick,
                player_id,
                tuple(updates.values()),
                event,
            )
        except OwnershipError as exc:
            raise WorldChangedError(
                "Collection changed while the Habitat tick was committing; please retry."
            ) from exc

        return PlayerSimulationResult(
            player_id=player_id,
            revision=revision,
            event=event,
            world_states=tuple(states[creature.pokemon_id] for creature in pokemon),
            director_used=decision.source,
            director_note=decision.note,
        )

    async def _lock_for(self, player_id: str) -> asyncio.Lock:
        """Return the process-local serialisation lock for one player."""

        async with self._lock_index_lock:
            lock = self._player_locks.get(player_id)
            if lock is None:
                lock = asyncio.Lock()
                self._player_locks[player_id] = lock
            return lock

    @staticmethod
    def _initial_state(
        player_id: str, pokemon: PokemonRecord, index: int
    ) -> PokemonWorldState:
        """Give a new capture a stable, visibly separated starting position."""

        x = 10 + PlayerSimulationService._stable_int(
            f"{player_id}|{pokemon.pokemon_id}|spawn-x", 81
        )
        y = 14 + PlayerSimulationService._stable_int(
            f"{player_id}|{pokemon.pokemon_id}|spawn-y", 73
        )
        # Tiny offset makes identical generated IDs/traits less likely to
        # overlap in the first frame, while remaining inside the safe field.
        x = PlayerSimulationService._clamp(x + (index * 7) % 13, 6, 94)
        y = PlayerSimulationService._clamp(y + (index * 5) % 11, 10, 90)
        return PokemonWorldState(
            pokemon_id=pokemon.pokemon_id,
            x=x,
            y=y,
            mood="curious",
            energy=75,
            activity="exploring",
        )

    @staticmethod
    def _personality_for(pokemon: PokemonRecord | None) -> Personality | None:
        if pokemon is None:
            return None
        raw_traits: Mapping[str, Any] = {}
        possible_traits = pokemon.metadata.get("personality")
        if isinstance(possible_traits, Mapping):
            raw_traits = possible_traits

        def trait(name: str) -> int:
            value = raw_traits.get(name)
            if isinstance(value, bool):
                value = None
            if isinstance(value, (int, float)):
                return PlayerSimulationService._clamp(int(round(value)), 0, 100)
            # Generated captures normally do not yet carry traits.  Derive
            # them from an immutable ID, rather than Python's salted hash, so
            # a restart never changes a creature's personality.
            return 20 + PlayerSimulationService._stable_int(
                f"{pokemon.pokemon_id}|{name}", 61
            )

        return Personality(
            curiosity=trait("curiosity"),
            sociability=trait("sociability"),
            bravery=trait("bravery"),
            competitiveness=trait("competitiveness"),
        )

    async def _jev_decision(
        self,
        *,
        player_id: str,
        revision: int,
        actor: PokemonRecord,
        actor_state: PokemonWorldState,
        actor_traits: Personality | None,
        target: PokemonRecord | None,
        target_state: PokemonWorldState | None,
        target_traits: Personality | None,
        recent_events: Sequence[SimulationEventRecord],
    ) -> SimulationDecision:
        """Ask Jev for one validated interaction, with no shared player thread.

        We intentionally do not save or pass a ``thread_id``.  Each request
        gets fresh, explicitly supplied state, so one player's Backboard
        conversation can never become another player's simulation context.
        """

        if not self.backboard_api_key:
            raise JevUnavailableError(
                "BACKBOARD_API_KEY is required before advancing a Habitat."
            )
        if BackboardClient is None:
            raise JevUnavailableError(
                "The Backboard SDK is required before advancing a Habitat."
            )

        assert actor_traits is not None
        state = {
            "player_id": player_id,
            "revision": revision,
            "actor": self._director_profile(actor, actor_state, actor_traits),
            "target": (
                self._director_profile(target, target_state, target_traits)
                if target is not None and target_state is not None and target_traits is not None
                else None
            ),
            "recent_events": [
                {
                    "kind": event.kind,
                    "summary": event.summary,
                    "actor_pokemon_id": event.actor_pokemon_id,
                    "target_pokemon_id": event.target_pokemon_id,
                }
                for event in recent_events[-3:]
            ],
        }
        criteria = self._interaction_criteria(recent_events)
        questions = {
            "next_interaction": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one low-stakes next interaction from the "
                    "listed choices. Reflect the profiles, personalities, "
                    "energy, and recent events; vary the activity from the "
                    "most recent interaction whenever another choice fits."
                ),
                "criteria": criteria,
            }
        }

        try:
            async with BackboardClient(api_key=self.backboard_api_key) as client:
                response = await client.send_message(
                    "Select only the next structured Habitat interaction.",
                    llm_provider="typesafe",
                    model_name="jev-latest",
                    stream=False,
                    system_one={"state": state, "questions": questions},
                )
            interaction = self._jev_interaction(response)
            if interaction not in criteria:
                raise ValueError("Jev repeated or did not return an offered interaction.")
            return SimulationDecision(interaction, "jev")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not return raw SDK/network/model details through the badge or
            # API. The caller can retry without a partial world commit.
            raise JevUnavailableError(
                "Jev did not provide a valid Habitat decision; try again."
            ) from exc

    @staticmethod
    def _interaction_criteria(
        recent_events: Sequence[SimulationEventRecord],
    ) -> dict[str, str]:
        """Offer Jev a varied valid choice set without inventing free-form text."""

        criteria = {
            "observe": "Investigate or quietly study the surroundings or another Pokemon.",
            "greet": "Start a cautious or friendly conversation.",
            "play": "Invite a cooperative, playful shared activity.",
            "challenge": "Begin a non-destructive competition or territorial disagreement.",
            "rest": "Pause to recover energy or avoid social pressure.",
        }
        if recent_events:
            # Avoid a monotonous single-Pokemon loop while leaving Jev in
            # charge of every actual choice. The most recent record comes
            # first from BadgeStore.
            criteria.pop(recent_events[0].kind, None)
        return criteria or {"observe": "Investigate the surroundings."}

    @staticmethod
    def _director_profile(
        pokemon: PokemonRecord,
        state: PokemonWorldState,
        traits: Personality,
    ) -> dict[str, Any]:
        return {
            "pokemon_id": pokemon.pokemon_id,
            "name": pokemon.name,
            "species": pokemon.species,
            "types": list(pokemon.types),
            "moves": list(pokemon.moves),
            "flavour": pokemon.flavour,
            "rarity": pokemon.rarity,
            "state": {
                "x": state.x,
                "y": state.y,
                "mood": state.mood,
                "energy": state.energy,
                "activity": state.activity,
            },
            "personality": traits.as_dict(),
        }

    @staticmethod
    def _jev_interaction(response: Any) -> Interaction | None:
        """Read only the constrained System One answer from a SDK response."""

        system_one = getattr(response, "system_one", None)
        if isinstance(system_one, Mapping):
            answers = system_one.get("answers")
        else:
            answers = getattr(system_one, "answers", None)
        if not isinstance(answers, Mapping):
            return None
        answer = answers.get("next_interaction")
        if isinstance(answer, Mapping):
            choice = answer.get("choice")
        else:
            choice = getattr(answer, "choice", None)
        if not isinstance(choice, str) or choice not in INTERACTIONS:
            return None
        return choice  # Type narrowed by the finite validation above.

    @staticmethod
    def _next_states(
        *,
        player_id: str,
        revision: int,
        kind: Interaction,
        actor: PokemonRecord,
        actor_state: PokemonWorldState,
        target: PokemonRecord | None,
        target_state: PokemonWorldState | None,
        updated_at: datetime,
    ) -> tuple[PokemonWorldState, PokemonWorldState | None]:
        """Produce one visible, bounded movement beat for the chosen action."""

        actor_delta_x = PlayerSimulationService._signed_delta(
            f"{player_id}|{actor.pokemon_id}|{revision}|actor-x", 10, 17
        )
        actor_delta_y = PlayerSimulationService._signed_delta(
            f"{player_id}|{actor.pokemon_id}|{revision}|actor-y", 7, 13
        )
        if kind == "rest":
            actor_delta_x //= 3
            actor_delta_y //= 3
        elif kind == "challenge":
            actor_delta_x = PlayerSimulationService._signed_delta(
                f"{player_id}|{actor.pokemon_id}|{revision}|rush-x", 15, 22
            )
            actor_delta_y = PlayerSimulationService._signed_delta(
                f"{player_id}|{actor.pokemon_id}|{revision}|rush-y", 10, 17
            )

        actor_energy_delta = {
            "observe": -1,
            "greet": -2,
            "play": -7,
            "challenge": -6,
            "rest": 12,
        }[kind]
        actor_next = PokemonWorldState(
            pokemon_id=actor.pokemon_id,
            x=PlayerSimulationService._advance_coordinate(actor_state.x, actor_delta_x, 6, 94),
            y=PlayerSimulationService._advance_coordinate(actor_state.y, actor_delta_y, 10, 90),
            mood={
                "observe": "curious",
                "greet": "open",
                "play": "happy",
                "challenge": "focused",
                "rest": "calm",
            }[kind],
            energy=PlayerSimulationService._clamp(actor_state.energy + actor_energy_delta, 0, 100),
            activity={
                "observe": "watching closely",
                "greet": "starting a conversation",
                "play": "playing together",
                "challenge": "testing a rival",
                "rest": "taking a breather",
            }[kind],
            updated_at=updated_at,
        )

        if target is None or target_state is None:
            return actor_next, None

        if kind in {"greet", "play", "challenge"}:
            # Move the companion partway toward the actor's *new* location.
            # This reads as an interaction, not the one-pixel jitter produced
            # by the legacy serial snapshot loop.
            target_delta_x = PlayerSimulationService._toward(
                target_state.x, actor_next.x, maximum=11
            )
            target_delta_y = PlayerSimulationService._toward(
                target_state.y, actor_next.y, maximum=9
            )
        else:
            target_delta_x = PlayerSimulationService._signed_delta(
                f"{player_id}|{target.pokemon_id}|{revision}|target-x", 5, 10
            )
            target_delta_y = PlayerSimulationService._signed_delta(
                f"{player_id}|{target.pokemon_id}|{revision}|target-y", 4, 8
            )

        target_energy_delta = -2 if kind == "play" else (-1 if kind == "challenge" else 0)
        target_next = PokemonWorldState(
            pokemon_id=target.pokemon_id,
            x=PlayerSimulationService._advance_coordinate(target_state.x, target_delta_x, 6, 94),
            y=PlayerSimulationService._advance_coordinate(target_state.y, target_delta_y, 10, 90),
            mood=(
                "engaged"
                if kind in {"greet", "play"}
                else ("focused" if kind == "challenge" else target_state.mood)
            ),
            energy=PlayerSimulationService._clamp(target_state.energy + target_energy_delta, 0, 100),
            activity=(
                f"talking with {actor.name}"
                if kind == "greet"
                else (
                    f"playing with {actor.name}"
                    if kind == "play"
                    else (
                        f"facing {actor.name}"
                        if kind == "challenge"
                        else target_state.activity
                    )
                )
            ),
            updated_at=updated_at,
        )
        return actor_next, target_next

    @staticmethod
    def _narrative(
        *,
        player_id: str,
        revision: int,
        kind: Interaction,
        actor: PokemonRecord,
        target: PokemonRecord | None,
    ) -> tuple[str, tuple[SimulationDialogueLine, ...]]:
        """Create bounded, deterministic dialogue the badge can render safely."""

        if target is None:
            summaries: dict[Interaction, tuple[str, ...]] = {
                "observe": ("{actor} studies the Habitat from a quiet corner.",),
                "greet": ("{actor} practises a greeting for a future friend.",),
                "play": ("{actor} invents a tiny solo game.",),
                "challenge": ("{actor} sets a personal training challenge.",),
                "rest": ("{actor} finds a peaceful spot to recharge.",),
            }
            lines: dict[Interaction, tuple[str, ...]] = {
                "observe": (
                    "That ripple moved before the wind did. Interesting.",
                    "The quiet parts of this place have the best clues.",
                    "I wonder what lives under that patch of moss.",
                    "There is always something new to notice.",
                ),
                "greet": (
                    "Hello, future friend. I am practising my best first impression.",
                    "If someone arrives, I hope they like electric light shows.",
                    "I should prepare a greeting that sounds confident.",
                ),
                "play": (
                    "A leaf, a puddle, and a little spark: perfect game rules.",
                    "I can make my own fun, especially with dramatic sound effects.",
                    "That pebble almost made it to the river. Rematch.",
                ),
                "challenge": (
                    "One more try. I can do this better than the last time.",
                    "My record is mine to beat today.",
                    "A real challenge starts with one careful step.",
                ),
                "rest": (
                    "A small pause will help my next idea shine brighter.",
                    "I will listen to the water until my charge settles.",
                    "Even explorers need a calm spot to recharge.",
                ),
            }
            summary = PlayerSimulationService._choose(
                summaries[kind], f"{player_id}|{actor.pokemon_id}|{revision}|summary"
            ).format(actor=actor.name)
            line = PlayerSimulationService._choose(
                lines[kind], f"{player_id}|{actor.pokemon_id}|{revision}|line"
            )
            return summary, (SimulationDialogueLine(actor.pokemon_id, line),)

        summaries = {
            "observe": (
                "{actor} studies {target}'s habits from a careful distance.",
                "{actor} pauses to watch how {target} explores the Habitat.",
            ),
            "greet": (
                "{actor} opens a cautious conversation with {target}.",
                "{actor} and {target} trade a friendly first hello.",
            ),
            "play": (
                "{actor} invites {target} into a small shared game.",
                "{actor} and {target} turn the Habitat into a playground.",
            ),
            "challenge": (
                "{actor} tests {target} with a spirited challenge.",
                "{actor} and {target} begin a harmless contest.",
            ),
            "rest": (
                "{actor} steps back from {target} to recover some energy.",
                "{actor} asks {target} for a little quiet time.",
            ),
        }
        dialogue = {
            "observe": (
                ("There is more to you than I expected.", "I noticed you watching."),
                ("How do you decide where to go next?", "Mostly by curiosity."),
            ),
            "greet": (
                ("Your energy is interesting.", "Then let us see where this goes."),
                ("Hello. Want to explore together?", "I thought you would never ask."),
            ),
            "play": (
                ("First one to the leaf pile wins.", "You are on."),
                ("I found a game. Join me.", "Only if you promise a rematch."),
            ),
            "challenge": (
                ("Show me you can keep up.", "I have been waiting for that."),
                ("A friendly contest?", "Friendly, but serious."),
            ),
            "rest": (
                ("I will be back when my steam settles.", "Take your time."),
                ("I need a quiet minute.", "I will keep the path clear."),
            ),
        }
        choice_key = f"{player_id}|{actor.pokemon_id}|{target.pokemon_id}|{revision}|narrative"
        summary = PlayerSimulationService._choose(summaries[kind], choice_key).format(
            actor=actor.name, target=target.name
        )
        first, second = PlayerSimulationService._choose(dialogue[kind], choice_key)
        return summary, (
            SimulationDialogueLine(actor.pokemon_id, first),
            SimulationDialogueLine(target.pokemon_id, second),
        )

    @staticmethod
    def _stable_int(value: str, modulus: int) -> int:
        if modulus <= 0:
            raise ValueError("modulus must be positive.")
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % modulus

    @staticmethod
    def _choose(values: Sequence[Any], seed: str) -> Any:
        if not values:
            raise ValueError("Cannot choose from an empty sequence.")
        return values[PlayerSimulationService._stable_int(seed, len(values))]

    @staticmethod
    def _clamp(value: int, low: int, high: int) -> int:
        return max(low, min(high, value))

    @staticmethod
    def _signed_delta(seed: str, minimum: int, maximum: int) -> int:
        if minimum <= 0 or maximum < minimum:
            raise ValueError("movement range must be positive and ordered.")
        magnitude = minimum + PlayerSimulationService._stable_int(
            f"{seed}|magnitude", maximum - minimum + 1
        )
        return -magnitude if PlayerSimulationService._stable_int(f"{seed}|sign", 2) else magnitude

    @staticmethod
    def _advance_coordinate(value: int, delta: int, low: int, high: int) -> int:
        """Move through a range, reflecting at edges instead of sticking there."""

        candidate = value + delta
        while candidate < low or candidate > high:
            if candidate > high:
                candidate = high - (candidate - high)
            if candidate < low:
                candidate = low + (low - candidate)
        return PlayerSimulationService._clamp(candidate, low, high)

    @staticmethod
    def _toward(current: int, destination: int, *, maximum: int) -> int:
        difference = destination - current
        if difference == 0:
            return 0
        magnitude = min(maximum, max(2, abs(difference) // 2))
        return magnitude if difference > 0 else -magnitude


__all__ = [
    "Director",
    "EmptyHabitatError",
    "Interaction",
    "JevUnavailableError",
    "Personality",
    "PlayerSimulationError",
    "PlayerSimulationResult",
    "PlayerSimulationService",
    "SimulationDecision",
    "WorldChangedError",
]
