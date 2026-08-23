"""The provider seam (SPEC §3): all LLM calls cross this interface."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class InferenceProvider(Protocol):
    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Run one structured-output completion, validated against `schema`."""
        ...
