"""Typed records for the server-authoritative Pokemon world."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PokemonStats(BaseModel):
    hp: int = Field(ge=1, le=999)
    attack: int = Field(ge=1, le=999)
    defense: int = Field(ge=1, le=999)
    speed: int = Field(ge=1, le=999)


class Pokemon(BaseModel):
    """A stable Pokemon profile plus the metadata needed by the world."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pokemon_id: str = Field(
        default_factory=lambda: f"mon_{uuid4().hex[:10]}",
        pattern=r"^[a-z0-9_-]{3,48}$",
    )
    name: str = Field(min_length=1, max_length=48)
    species: str = Field(min_length=1, max_length=96)
    element: str = Field(alias="type", min_length=1, max_length=32)
    stats: PokemonStats
    moves: list[str] = Field(min_length=1, max_length=4)
    flavour: str = Field(min_length=1, max_length=240)
    sprite_prompt: str = Field(min_length=1, max_length=500)
    rarity: Literal["common", "uncommon", "rare", "legendary"]
    caught_at: datetime = Field(default_factory=utc_now)
    sprite_key: str | None = Field(default=None, max_length=64)
    sprite_status: Literal["pending", "ready"] = "pending"

    @field_validator("name", "species", "element", "flavour", "sprite_prompt")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Text fields cannot be blank.")
        return value

    @field_validator("moves")
    @classmethod
    def normalise_moves(cls, values: list[str]) -> list[str]:
        moves = [value.strip().lower().replace(" ", "_") for value in values if value.strip()]
        if not moves:
            raise ValueError("At least one move is required.")
        return moves


class PokemonPersonality(BaseModel):
    """Durable traits used as structured context for the simulation director."""

    curiosity: int = Field(default=50, ge=0, le=100)
    sociability: int = Field(default=50, ge=0, le=100)
    bravery: int = Field(default=50, ge=0, le=100)
    competitiveness: int = Field(default=50, ge=0, le=100)


class PokemonSimulationState(BaseModel):
    pokemon_id: str
    x: int = Field(default=50, ge=0, le=100)
    y: int = Field(default=50, ge=0, le=100)
    mood: str = Field(default="calm", min_length=1, max_length=32)
    energy: int = Field(default=75, ge=0, le=100)
    activity: str = Field(default="waiting", min_length=1, max_length=48)
    personality: PokemonPersonality = Field(default_factory=PokemonPersonality)
    updated_at: datetime = Field(default_factory=utc_now)


class PokemonRelationship(BaseModel):
    first_id: str
    second_id: str
    friendship: int = Field(default=0, ge=-100, le=100)
    rivalry: int = Field(default=0, ge=0, le=100)


class DialogueLine(BaseModel):
    speaker_id: str
    text: str = Field(min_length=1, max_length=240)


class SimulationEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex[:12]}")
    revision: int = Field(ge=1)
    actor_id: str
    target_id: str = ""
    kind: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=240)
    dialogue: list[DialogueLine] = Field(default_factory=list, max_length=2)
    created_at: datetime = Field(default_factory=utc_now)
    director: Literal["fallback", "jev"] = "fallback"


class WorldSnapshot(BaseModel):
    revision: int = Field(default=0, ge=0)
    pokemon: list[Pokemon] = Field(default_factory=list, max_length=64)
    states: dict[str, PokemonSimulationState] = Field(default_factory=dict)
    relationships: list[PokemonRelationship] = Field(default_factory=list)
    events: list[SimulationEvent] = Field(default_factory=list, max_length=100)


class SimulationTickRequest(BaseModel):
    """The bridge can disable Jev for a deterministic offline test tick."""

    prefer_jev: bool = True


class SimulationTickResult(BaseModel):
    world: WorldSnapshot
    event: SimulationEvent
    director_used: Literal["fallback", "jev"]
    director_note: str | None = None
