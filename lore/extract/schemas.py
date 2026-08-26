"""The JSON contract for one chunk's extraction.

This module is the single source of truth: the local tier validates
against it, and the Kaggle kernel hands its JSON schema to vLLM's
guided decoding so the model cannot emit anything else.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "2a.1"

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
