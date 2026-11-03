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
    # The cascade for `extraction_provider = "gemini"`. A field here rather
    # than a new section: `extraction_provider` is a top-level string, and
    # this section already holds each tier's model (`model` for Kaggle,
    # `embed_model_ollama` for the local embedder). Lite only: 500
    # requests/day each, and one chunk is one request. Measured on 22 chunks
    # (extraction-eval-report.md), 3.1-flash-lite beat the Kaggle 7B on
    # relationship precision (81% vs 61%) and entity kinds (1 wrong vs 56).
    gemini_models: list[str] = Field(default_factory=lambda: list(LITE_MODELS))


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

    Template selection switches separately, in SelectionConfig. The quote
    check applies whichever model answers.

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


class SelectionConfig(BaseModel):
    """Who picks `lore ask`'s search template. Local by default; Gemini is opt-in.

    The mirror of SynthesisConfig, switched independently of it. Measured on
    the frozen questions (Phase 3, before Task 8b): Gemini Flash Lite
    honoured 24 of 24 choices, identical across three runs; the local 3B
    honoured 21 of 24 and flipped choices between runs at temperature 0 and
    seed 0 with nothing changed. Choosing Gemini sends the question and the
    names of its seed entities to Google.

    `extra="forbid"` for the same reason as DisambiguationConfig: no
    `api_key` line may be silently accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama", "gemini"] = "ollama"
    models: list[str] = Field(default_factory=lambda: list(LITE_MODELS))


class AdjudicationConfig(BaseModel):
    """Who answers the resolver's borderline same-name merges during a load.

    Local by default. The 3B's record on this graph (see
    `lore.graph.loader._reconcile_ids`) is answers anti-correlated with the
    truth, which is why the project's lore.toml moves it to Gemini. The
    prompt is identical either way (`lore.graph.adjudicate._TEMPLATE`), and
    Gemini answers are cached on disk so a reload spends no requests.
    Choosing Gemini sends two entity names, their kinds, aliases and one
    evidence sentence per question to Google.

    `extra="forbid"` for the same reason as DisambiguationConfig: no
    `api_key` line may be silently accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama", "gemini"] = "ollama"
    models: list[str] = Field(default_factory=lambda: list(LITE_MODELS))


class WebConfig(BaseModel):
    """`lore ui` (Phase 5a). show_internals gates provider details (model
    names, the Gemini quota, Ollama commands, the MCP panel): on for a
    local install, off for a future deployed build."""

    show_internals: bool = True
    port: int = 8766


class ProcessingConfig(BaseModel):
    """Book processing (resumable jobs). `answer_reserve` is the number of
    Gemini requests per day that processing leaves untouched, so questions
    in the UI keep working while a book is being processed."""

    answer_reserve: int = Field(default=60, ge=0)


class BeatsConfig(BaseModel):
    """Story beats (the `beats` pipeline stage). On by default; when False the
    stage does nothing and the book simply has no beats."""

    enabled: bool = True


class CodexConfig(BaseModel):
    """The codex (the `codex` pipeline stage): quote-verified entity profiles.
    On by default; when False the stage does nothing and no entry is built."""

    enabled: bool = True


class AnswersConfig(BaseModel):
    """How answers are written. `viewpoint` is the path of an editorial policy
    that replaces the packaged `lore/query/viewpoint.md`; empty uses it."""

    viewpoint: Path | None = None


class LoreConfig(BaseModel):
    data_dir: Path = Path("data")
    # "kaggle": free T4 burst. "ollama": fully local, zero egress.
    # "gemini": each chunk's text goes to Google, one request per chunk;
    # embeddings stay local on Ollama.
    extraction_provider: Literal["kaggle", "ollama", "gemini"] = "kaggle"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    kaggle: KaggleConfig = Field(default_factory=KaggleConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    disambiguation: DisambiguationConfig = Field(
        default_factory=DisambiguationConfig
    )
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    adjudication: AdjudicationConfig = Field(default_factory=AdjudicationConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    beats: BeatsConfig = Field(default_factory=BeatsConfig)
    answers: AnswersConfig = Field(default_factory=AnswersConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)

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

    @property
    def beats_dir(self) -> Path:
        """Cached beat answers, per book. Never created eagerly."""
        return self.data_dir / "beats"

    @property
    def codex_dir(self) -> Path:
        """Cached codex answers, per entity. Never created eagerly."""
        return self.data_dir / "codex"

    @property
    def adjudication_dir(self) -> Path:
        """The Gemini adjudicator's answer cache. Never created eagerly."""
        return self.data_dir / "adjudication"

    @property
    def narrators_dir(self) -> Path:
        """Task 10c's per-book narrator hints. Never created eagerly."""
        return self.data_dir / "narrators"

    @property
    def speakers_dir(self) -> Path:
        """Task 10c's per-chunk first-person speakers. Never created eagerly."""
        return self.data_dir / "speakers"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.kuzu_dir,
            self.lancedb_dir,
            self.inbox_dir,
            self.extractions_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
