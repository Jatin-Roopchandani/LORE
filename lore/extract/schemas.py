"""The JSON contract for one chunk's extraction.

This module is the single source of truth: both tiers validate against
it, and the local tier hands its JSON schema to Ollama's structured output
so the model cannot emit anything else.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "2a.1"

# Shared with both inference tiers (SPEC §3 rule 1: prompt, schema,
# validation and retry logic live once so the tiers cannot drift).
#
# 4096 is measured, not guessed: Task 1a raised the median chunk from
# 170 to 1518 characters, which raised triplets-per-chunk in proportion
# and pushed output past the old MAX_OUTPUT_TOKENS=2048 (itself already
# raised once from 1024 for the same reason). Measured truncations sit
# at 6367-8329 raw characters (~1800-2400 tokens), so 4096 is ~70%
# headroom over the worst case seen. Do not raise further without new
# measurement.
MAX_OUTPUT_TOKENS = 4096

EntityKind = Literal["Character", "Place", "Faction", "Object", "Concept", "Event"]


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1)
    kind: EntityKind

    @field_validator("name")
    @classmethod
    def _strip_and_require_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("entity name must contain text")
        return cleaned


class ExtractedTriplet(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    evidence: str = Field(description="verbatim quote from the chunk")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("subject", "predicate", "object")
    @classmethod
    def _strip_and_require_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("triplet fields must contain text")
        return cleaned


class ChunkExtraction(BaseModel):
    # No defaults: guided decoding needs both keys in the schema's
    # `required` list so the model always emits them, even when empty.
    entities: list[ExtractedEntity]
    triplets: list[ExtractedTriplet]
