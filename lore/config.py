"""Configuration: lore.toml + LORE_* env overrides, defaults from SPEC.md."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lore.inference.gemini import DEFAULT_MODELS, LITE_MODELS


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:3b-instruct-q4_K_M"


class KaggleConfig(BaseModel):
    username: str = ""
    kernel_slug: str = "lore-hello"
    dataset_slug: str = "lore-hello-data"
    poll_interval_s: int = 30
    poll_timeout_s: int = 1800
    extract_kernel_slug: str = "lore-extract"
    extract_dataset_slug: str = "lore-extract-data"


class ExtractionConfig(BaseModel):
    model: str = "Qwen/Qwen2.5-7B-Instruct-AWQ"
    embed_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embed_model_ollama: str = "nomic-embed-text"
    work_unit_size: int = 10  # 10 verified on a Kaggle T4; 40 crashes vLLM's engine
    vllm_version: str = ""  # empty = install latest on the kernel


class DisambiguationConfig(BaseModel):
    """The optional Gemini disambiguator (Task 1j). Off by default.

    `extra="forbid"` is load-bearing, not tidiness: the API key must come
    from the environment (`GEMINI_API_KEY`, then `GOOGLE_API_KEY`) and
    nowhere else. Pydantic ignores unknown keys by default, so an
    `api_key = "..."` line in lore.toml would be silently accepted, do
    nothing, and end up committed to git. Forbidding extras turns that
    into a loud config error instead.
    """

    model_config = ConfigDict(extra="forbid")

    # Opt-in. With this False, `lore process` records no questions,
    # consults no cache, and behaves exactly as it did before Task 1j.
    enabled: bool = False
    # The whole cascade is configurable, and its ORDER IS MEASURED, not
    # chronological — see `lore.inference.gemini`. A 429/503/404 moves to
    # the next entry, which is what turns five per-model daily quotas into
    # aggregate capacity.
    models: list[str] = Field(default_factory=lambda: list(DEFAULT_MODELS))


class SynthesisConfig(BaseModel):
    """Who writes `lore ask`'s answers. Local by default; Gemini is opt-in.

    Only synthesis switches: template selection stays on the local model
    either way, and the quote check applies to both.

    Measured on identical evidence (Phase 3 Task 7c): Gemini Flash Lite
    scored 2 correct / 5 partial / 0 wrong with 0 of 11 claims misread;
    the local 3B scored 0 / 3 / 1 with 8 of 11 misread. Choosing Gemini
    sends the question and its evidence passages to Google.

    `extra="forbid"` for the same reason as DisambiguationConfig: an
    `api_key = "..."` line here must be a loud error, never a silently
    ignored secret on its way into git. The key comes from the environment
    or `~/.config/lore/gemini.env`, nowhere else.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama", "gemini"] = "ollama"
    # Lite only by default: 500 requests/day each, where the Flash tier's
    # 20/day is shared with `lore disambiguate`. Used only for "gemini".
    models: list[str] = Field(default_factory=lambda: list(LITE_MODELS))


class LoreConfig(BaseModel):
    data_dir: Path = Path("data")
    extraction_provider: str = "kaggle"  # "kaggle" | "ollama"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    kaggle: KaggleConfig = Field(default_factory=KaggleConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    disambiguation: DisambiguationConfig = Field(
        default_factory=DisambiguationConfig
    )
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "LoreConfig":
        candidate = path if path is not None else Path("lore.toml")
        raw: dict = {}
        if candidate.is_file():
            with candidate.open("rb") as f:
                raw = tomllib.load(f).get("lore", {})
        cfg = cls.model_validate(raw)
        if env_dir := os.environ.get("LORE_DATA_DIR"):
            cfg = cfg.model_copy(update={"data_dir": Path(env_dir)})
        return cfg

    @property
    def kuzu_dir(self) -> Path:
        return self.data_dir / "kuzu"

    @property
    def lancedb_dir(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "lore.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def extractions_dir(self) -> Path:
        return self.data_dir / "extractions"

    @property
    def disambiguation_dir(self) -> Path:
        """Pending questions and the answer cache. Never created eagerly.

        Deliberately absent from `ensure_dirs`: a run with the feature off
        must leave no trace of it at all, which is also what makes "a load
        with no key and no config is byte-identical to today" testable.
        """
        return self.data_dir / "disambiguation"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.kuzu_dir,
            self.lancedb_dir,
            self.inbox_dir,
            self.extractions_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
