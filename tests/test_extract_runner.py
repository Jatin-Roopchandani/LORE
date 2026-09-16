import pytest

from lore.extract.runner import ChunkInput, extract_chunks
from lore.extract.schemas import ChunkExtraction
from lore.inference.base import ProviderOutputError

VALID = ChunkExtraction.model_validate(
    {
        "entities": [{"name": "Darcy", "kind": "Character"}],
        "triplets": [
            {
                "subject": "Darcy",
                "predicate": "LOVES",
                "object": "Elizabeth",
                "evidence": "he ardently admired her",
                "confidence": 0.9,
            }
        ],
    }
)


class ScriptedProvider:
    """Returns/raises whatever the script says, per call."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    async def generate(self, prompt: str, schema):
        self.calls.append(prompt)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_happy_path_collects_results() -> None:
    provider = ScriptedProvider([VALID, VALID])
    result = await extract_chunks(
        [ChunkInput("b:1:0", "text one"), ChunkInput("b:1:1", "text two")], provider
    )
    assert [r.chunk_id for r in result.results] == ["b:1:0", "b:1:1"]
    assert result.dead_letters == []
    assert "text one" in provider.calls[0]  # the chunk text reached the prompt


@pytest.mark.asyncio
async def test_retries_once_then_succeeds() -> None:
    provider = ScriptedProvider([ValueError("bad json"), VALID])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert len(result.results) == 1
    assert result.dead_letters == []
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_dead_letters_after_repeated_failure() -> None:
    provider = ScriptedProvider([ValueError("bad json"), ValueError("still bad")])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert result.results == []
    assert len(result.dead_letters) == 1
    dead = result.dead_letters[0]
    assert dead.chunk_id == "b:1:0"
    assert "still bad" in dead.error


@pytest.mark.asyncio
async def test_one_bad_chunk_does_not_abort_the_batch() -> None:
    provider = ScriptedProvider(
        [ValueError("x"), ValueError("y"), VALID]  # chunk 1 dies, chunk 2 succeeds
    )
    result = await extract_chunks(
        [ChunkInput("b:1:0", "a"), ChunkInput("b:1:1", "b")], provider
    )
    assert [r.chunk_id for r in result.results] == ["b:1:1"]
    assert [d.chunk_id for d in result.dead_letters] == ["b:1:0"]


@pytest.mark.asyncio
async def test_max_attempts_below_one_raises_value_error() -> None:
    provider = ScriptedProvider([VALID])
    with pytest.raises(ValueError):
        await extract_chunks(
            [ChunkInput("b:1:0", "text")], provider, max_attempts=0
        )


@pytest.mark.asyncio
async def test_provider_output_error_populates_the_dead_letter_raw_output() -> None:
    # FIX 4: providers that raise ProviderOutputError give the dead letter
    # a populated raw_output instead of an empty string.
    provider = ScriptedProvider([
        ProviderOutputError("bad json", raw_output="{not valid"),
        ProviderOutputError("still bad", raw_output="{also not valid"),
    ])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert result.results == []
    assert len(result.dead_letters) == 1
    dead = result.dead_letters[0]
    assert dead.chunk_id == "b:1:0"
    assert dead.raw_output == "{also not valid"


@pytest.mark.asyncio
async def test_progress_callback_reports_each_chunk() -> None:
    seen: list[tuple[int, int]] = []
    provider = ScriptedProvider([VALID, VALID])
    await extract_chunks(
        [ChunkInput("b:1:0", "a"), ChunkInput("b:1:1", "b")],
        provider,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(1, 2), (2, 2)]


# --- Task R1: a provider that cannot serve ANY chunk halts the unit ---------

from lore.extract.runner import ExtractionHalted  # noqa: E402


@pytest.mark.asyncio
async def test_a_halted_provider_propagates_and_dead_letters_nothing() -> None:
    # "Daily quota used up" is not a property of the chunk. Dead-lettering
    # it would silently mark hundreds of good chunks dead; the unit must
    # fail instead so `--resume` picks it up when the budget resets.
    provider = ScriptedProvider([VALID, ExtractionHalted("quota gone")])
    with pytest.raises(ExtractionHalted, match="quota gone"):
        await extract_chunks(
            [ChunkInput("b:1:0", "a"), ChunkInput("b:1:1", "b"),
             ChunkInput("b:1:2", "c")],
            provider,
        )
    # No retry either: a second attempt against an exhausted budget only
    # spends another request.
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_ordinary_errors_still_retry_then_dead_letter_unchanged() -> None:
    # The Ollama path's behaviour is untouched: anything that is not an
    # ExtractionHalted is a per-chunk failure, exactly as before.
    provider = ScriptedProvider([RuntimeError("HTTP 500"), RuntimeError("HTTP 500")])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert [d.chunk_id for d in result.dead_letters] == ["b:1:0"]
