"""The provider seam (SPEC §3): all LLM calls cross this interface."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class InferenceProvider(Protocol):
    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Run one structured-output completion, validated against `schema`."""
        ...


class ProviderOutputError(Exception):
    """Raised when a provider's raw output fails schema validation.

    Carries the model's raw text so dead letters stay diagnosable.
    """

    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output
