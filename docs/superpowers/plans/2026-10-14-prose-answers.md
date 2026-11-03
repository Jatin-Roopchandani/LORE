# Prose Answers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** turn the checked claims into a short, coherent, cited answer, and fall back to
the claim list whenever the prose doesn't pass verification.

**Architecture:**
- A new `lore/query/write.py` holds the writer prompt, its schema, the code-level
  citation check and the assembly and fallback rules.
- The faithfulness check in `lore/query/synthesize.py` is extended to judge sentences
  in the same call.
- `synthesize` / `synthesize_parts` run the writer between the near-duplicate collapse
  and the check.
- `Answer`, the MCP result and the live event stream gain additive fields and steps.
- The web chat pane renders prose with citation chips over a collapsed Evidence list.
- The eval scripts learn to grade the answer text and a readability preference.

**Tech Stack:** Python 3.12, pydantic, pytest (pytest-asyncio auto mode); React + TypeScript + Vitest.

**Spec:** `docs/superpowers/specs/2026-10-12-prose-answers-design.md`

## Global Constraints

- Work in `/home/jatin/LORE-prose` (branch `prose-answers`). Never edit `/home/jatin/LORE`, `/home/jatin/LORE-ui` or other worktrees.
- Commits use user.email `jatinroopchandani3007@gmail.com`. **Commit messages carry no trailer lines at all**: no `Co-Authored-By`, no `Claude-Session`.
- Python tests: `.venv/bin/python -m pytest -q <files>` from the worktree root. Create the venv with `uv sync` if it's missing. Frontend: `cd web && npm test && npx tsc --noEmit && npm run build`. If `web/node_modules` is missing, run `npm install`.
- No Gemini or Ollama calls in tests. Everything is scripted.
- With `PROSE_ANSWERS = False` the synthesis prompts and model calls are byte-identical to today.
- The writer runs only when there are ≥ 2 claims. It's one call per question, never per part.
- Fallback rules, exactly:
  - sentences citing a claim the check dropped are removed;
  - if more than 1 sentence is judged unsupported on its own, fall back;
  - if fewer than half of the writer's sentences survive (`2 * survivors < written`), fall back;
  - if no sentences survive, fall back;
  - if the check didn't run, or the sentence verdicts don't line up, fall back.
- `ProseSentence.claims` are 1-based indices into the final `Answer.claims` (the survivors). `ProseSentence.passages` are the sorted, unique passage numbers of those claims.
- The prose answer text is sentences joined by spaces, each followed by its citations like `[3][5]`; paragraphs are joined by a blank line.
- `answer_style` is `"prose"` or `"claims"`.
- All new user-facing frontend strings live in `web/src/messages.ts`.
- The writer prompt contains no eval-set text. The leak test must cover it.

## Review Focus

1. **The writer cites a claim number that doesn't exist or is 0.** The sentence is dropped by code, never shown. → Task 1 `verify_prose` test.
2. **The check model returns claim verdicts but garbled sentence verdicts.** The claim verdicts still apply (no safety lost), the prose falls back, and the answer shows claims. → Task 2 and Task 3 tests.
3. **A sentence cites a claim that the check then drops.** That sentence goes too, and the remaining citations renumber to the survivors. → Task 1 `assemble` test.
4. **History or old results with no `prose` / `answer_style`.** The UI renders the claim view without crashing. → Task 5 test.
5. **A sentence citing claims from several passages.** Hover lights all of their edges, and every chip opens its own passage. → Task 5 tests.

---

## File Structure

- Create `lore/query/write.py`: the writer (prompt, schemas, `write_answer`), `verify_prose`, `assemble`, `compose_text`, and the `PROSE_ANSWERS` flag.
- Modify `lore/query/models.py`: add `ProseSentence`; `Answer` gains `prose` and `answer_style`.
- Modify `lore/query/synthesize.py`: a sentence block in the faithfulness prompt, `validated_sentence_verdicts`, `check_answer`, and the shared `_finish` used by `synthesize` / `synthesize_parts`.
- Modify `lore/query/agent.py`: `QueryModels.write`, the `compose` step events, and `_synthesize` passing the writer.
- Modify `lore/mcp/shapes.py`: `ProseOut`; `SearchResult` gains `prose` and `answer_style`.
- Modify `evals/answer_quality/run_pair.py`, `evals/answer_quality/grade_pair.py` and `evals/answer_quality/GRADING.md`.
- Frontend: `web/src/types.ts`, `web/src/messages.ts`, a new `web/src/components/Prose.tsx`, `web/src/components/ChatPane.tsx`, `web/src/components/Claims.tsx`, `web/src/App.tsx`, `web/src/components/GraphPane.tsx`, `web/src/graphView.ts`, `web/src/theme.css`.
- Tests: `tests/test_query_write.py` (new), `tests/test_query_synthesize.py`, `tests/test_query_agent.py`, `tests/test_mcp_tools.py`, `tests/test_grade_pair.py` (new), `web/src/components/Prose.test.tsx` (new), `web/src/components/ChatPane.test.tsx`, `web/src/graphView.test.ts`.

---

### Task 1: The writer module

**Files:**
- Create: `lore/query/write.py`
- Modify: `lore/query/models.py` (add `ProseSentence`)
- Test: `tests/test_query_write.py`

**Interfaces:**
- Produces:
  - `PROSE_ANSWERS: bool = True`
  - `class Sentence(BaseModel): text: str; claims: list[int]; new_paragraph: bool = False`
  - `class Prose(BaseModel): sentences: list[Sentence]`
  - `WRITER_PROMPT: str`
  - `writer_prompt(question: str, sub_questions: Sequence[str], claims: Sequence[Claim], passages: Iterable[Passage], book_titles: dict[str, str] | None) -> str`
  - `async write_answer(question, sub_questions, claims, passages, generate, *, book_titles=None) -> Prose | None`
  - `verify_prose(prose: Prose, n_claims: int) -> list[Sentence]`
  - `assemble(sentences: Sequence[Sentence], written: int, claim_ok: Sequence[bool], sentence_ok: Sequence[bool], passage_of: Sequence[int]) -> tuple[ProseSentence, ...] | None`
  - `compose_text(prose: Sequence[ProseSentence]) -> str`
  - In `lore/query/models.py`: `@dataclass(frozen=True) class ProseSentence: text: str; claims: tuple[int, ...]; passages: tuple[int, ...]; new_paragraph: bool = False`.

- [ ] **Step 1: Write the failing tests**

`tests/test_query_write.py`:

```python
"""The writer: prompt, citation check, assembly and fallback rules."""
from lore.query.models import Claim, Passage, ProseSentence
from lore.query.write import (
    Prose, Sentence, assemble, compose_text, verify_prose, write_answer, writer_prompt,
)

P = (Passage(n=3, chunk_id="b1:3:0", book_id="b1", chapter_seq=3, text="Hope loved Lucy Ferrier."),
     Passage(n=5, chunk_id="b1:5:0", book_id="b1", chapter_seq=5, text="Lucy was forced to marry Drebber."))
C = [Claim(text="Hope loved Lucy.", passage=3, quote="Hope loved Lucy Ferrier"),
     Claim(text="Lucy was forced to marry Drebber.", passage=5, quote="Lucy was forced to marry Drebber")]


def S(text, claims, new_paragraph=False):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph)


def test_writer_prompt_numbers_the_facts_and_shows_book_and_chapter():
    prompt = writer_prompt("Why did Hope hate Drebber?", ["Why did Hope hate Drebber?"], C, P, {"b1": "A Study in Scarlet"})
    assert "1. Hope loved Lucy." in prompt and "2. Lucy was forced to marry Drebber." in prompt
    assert "A Study in Scarlet, chapter 3" in prompt
    assert "Why did Hope hate Drebber?" in prompt


def test_verify_prose_drops_uncited_and_out_of_range_sentences():
    prose = Prose(sentences=[S("Good.", [1]), S("No cite.", []), S("Zero.", [0]), S("Too high.", [3]), S("Both.", [1, 2])])
    kept = verify_prose(prose, n_claims=2)
    assert [s.text for s in kept] == ["Good.", "Both."]


def test_assemble_maps_claims_to_passages_and_survivor_indices():
    out = assemble([S("A.", [1]), S("B.", [1, 2], True)], written=2,
                   claim_ok=[True, True], sentence_ok=[True, True], passage_of=[3, 5])
    assert out == (ProseSentence("A.", (1,), (3,), False), ProseSentence("B.", (1, 2), (3, 5), True))


def test_a_dropped_claim_takes_its_sentences_and_citations_renumber():
    # claim 1 fails the check; the survivor (old claim 2) becomes claim 1.
    out = assemble([S("A.", [1]), S("B.", [2]), S("C.", [2])], written=3,
                   claim_ok=[False, True], sentence_ok=[True, True, True], passage_of=[3, 5])
    assert out == (ProseSentence("B.", (1,), (5,)), ProseSentence("C.", (1,), (5,)))


def test_one_unsupported_sentence_is_removed():
    out = assemble([S("A.", [1]), S("B.", [2]), S("C.", [1, 2])], written=3,
                   claim_ok=[True, True], sentence_ok=[True, False, True], passage_of=[3, 5])
    assert [s.text for s in out] == ["A.", "C."]


def test_two_unsupported_sentences_fall_back():
    assert assemble([S("A.", [1]), S("B.", [2]), S("C.", [1])], written=3,
                    claim_ok=[True, True], sentence_ok=[False, False, True], passage_of=[3, 5]) is None


def test_fewer_than_half_surviving_falls_back():
    # 5 written, 3 lost to verify_prose earlier, 1 removed now -> 1 of 5 survives.
    assert assemble([S("A.", [1]), S("B.", [2])], written=5,
                    claim_ok=[True, True], sentence_ok=[True, False], passage_of=[3, 5]) is None


def test_nothing_surviving_falls_back():
    assert assemble([S("A.", [1])], written=1, claim_ok=[False, True], sentence_ok=[True], passage_of=[3, 5]) is None


def test_compose_text_cites_and_splits_paragraphs():
    text = compose_text((ProseSentence("A.", (1,), (3,)), ProseSentence("B.", (1, 2), (3, 5)),
                         ProseSentence("C.", (2,), (5,), True)))
    assert text == "A. [3] B. [3][5]\n\nC. [5]"


async def test_write_answer_returns_none_on_garbage_or_exception():
    async def raises(prompt, schema):
        raise RuntimeError("quota")
    async def garbage(prompt, schema):
        return "not a model"
    assert await write_answer("q", ["q"], C, P, raises) is None
    assert await write_answer("q", ["q"], C, P, garbage) is None


async def test_write_answer_returns_the_prose():
    async def ok(prompt, schema):
        assert schema is Prose
        return Prose(sentences=[S("Hope loved Lucy.", [1])])
    out = await write_answer("q", ["q"], C, P, ok)
    assert out.sentences[0].claims == [1]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_query_write.py`
Expected: FAIL (`ModuleNotFoundError: lore.query.write`).

- [ ] **Step 3: Add `ProseSentence` to `lore/query/models.py`** (after `Passage`):

```python
@dataclass(frozen=True)
class ProseSentence:
    """One sentence of a written answer. `claims` are 1-based indices into
    Answer.claims (the checked survivors); `passages` are those claims'
    passage numbers, sorted and unique, for the UI's citation chips."""

    text: str
    claims: tuple[int, ...]
    passages: tuple[int, ...]
    new_paragraph: bool = False
```

- [ ] **Step 4: Implement `lore/query/write.py`**

```python
"""The writer: turns checked claims into a short, coherent, cited answer.

Claims are proven facts (each quote found in its passage by code). Stacked
in order they read as a list, not an answer. The writer composes a
paragraph or two from those claims only, every sentence citing the claims
it rests on. The faithfulness check then judges each sentence against its
claims (synthesize.check_answer), and `assemble` applies the fallback
rules: prose that does not pass is never shown -- the claim list is.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel

from lore.query.models import Claim, Passage, ProseSentence

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

# On by default (spec 2026-10-12). Off gives today's claim-list answers,
# byte-identical prompts and calls.
PROSE_ANSWERS = True


class Sentence(BaseModel):
    text: str
    claims: list[int]
    new_paragraph: bool = False


class Prose(BaseModel):
    sentences: list[Sentence]


WRITER_PROMPT = """Write the answer to the question from the numbered facts below.

Each fact was checked against the book it cites. Use only what the facts
say; never add anything they do not say, including what you know about the
story. Every sentence lists the numbers of the facts it uses.

- Answer the question directly in your first sentence.
- Do not state the same fact twice.
- When the facts come from more than one book, say which book.
- Keep each fact's certainty: if a fact says someone seemed, was suspected
  or claimed, say so too.
- Length: a narrow question gets two to four sentences; a broad one gets
  up to two short paragraphs, about 200 words at most. Mark the first
  sentence of a second paragraph with new_paragraph.
- Do not use connecting words that add a cause, an order or a consequence
  ("because", "so", "later", "which led to") unless a cited fact states it.

Question: {question}
{parts}
Facts:
{facts}
"""


def _title(book_id: str, book_titles: dict[str, str] | None) -> str:
    return (book_titles or {}).get(book_id) or book_id


def writer_prompt(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                  passages: Iterable[Passage], book_titles: dict[str, str] | None) -> str:
    by_number = {p.n: p for p in passages}
    lines = []
    for number, claim in enumerate(claims, start=1):
        p = by_number.get(claim.passage)
        where = f" ({_title(p.book_id, book_titles)}, chapter {p.chapter_seq})" if p else ""
        lines.append(f"{number}. {claim.text.strip()}{where}\n   quote: {claim.quote.strip()}")
    parts = ("It has these parts:\n" + "\n".join(f"- {s}" for s in sub_questions) + "\n"
             if len(sub_questions) > 1 else "")
    return WRITER_PROMPT.format(question=question, parts=parts, facts="\n".join(lines))


async def write_answer(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                       passages: Iterable[Passage], generate: Generate, *,
                       book_titles: dict[str, str] | None = None) -> Prose | None:
    """The writer's prose, or None on any failure: a failed extra step must
    never cost the user the checked claims (they are the fallback)."""
    try:
        reply = await generate(writer_prompt(question, sub_questions, claims, passages, book_titles), Prose)
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    return reply if isinstance(reply, Prose) and reply.sentences else None


def verify_prose(prose: Prose, n_claims: int) -> list[Sentence]:
    """Sentences that cite at least one claim, all within 1..n_claims."""
    return [s for s in prose.sentences
            if s.text.strip() and s.claims and all(1 <= k <= n_claims for k in s.claims)]


def assemble(sentences: Sequence[Sentence], written: int, claim_ok: Sequence[bool],
             sentence_ok: Sequence[bool], passage_of: Sequence[int]) -> tuple[ProseSentence, ...] | None:
    """The prose to show, or None to fall back to the claim list.

    `written`: how many sentences the writer produced, before verify_prose.
    `claim_ok`/`passage_of`: per claim given to the writer (1-based order).
    `sentence_ok`: per sentence in `sentences`.
    """
    survivors_index = {}
    for k, ok in enumerate(claim_ok, start=1):
        if ok:
            survivors_index[k] = len(survivors_index) + 1
    standing = [(s, ok) for s, ok in zip(sentences, sentence_ok)
                if all(k in survivors_index for k in s.claims)]
    if sum(not ok for _, ok in standing) > 1:
        return None
    kept = [s for s, ok in standing if ok]
    if not kept or 2 * len(kept) < written:
        return None
    return tuple(
        ProseSentence(text=s.text.strip(),
                      claims=tuple(survivors_index[k] for k in s.claims),
                      passages=tuple(sorted({passage_of[k - 1] for k in s.claims})),
                      new_paragraph=s.new_paragraph and i > 0)
        for i, s in enumerate(kept))


def compose_text(prose: Sequence[ProseSentence]) -> str:
    paragraphs: list[list[str]] = [[]]
    for s in prose:
        if s.new_paragraph and paragraphs[-1]:
            paragraphs.append([])
        paragraphs[-1].append(s.text + " " + "".join(f"[{n}]" for n in s.passages))
    return "\n\n".join(" ".join(p) for p in paragraphs)
```

`assemble` returns `new_paragraph` false for the first kept sentence. In the test `test_assemble_maps_claims_to_passages_and_survivor_indices`, the second sentence keeps `True`. That's consistent.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_query_write.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git config user.email   # must print jatinroopchandani3007@gmail.com
git add lore/query/write.py lore/query/models.py tests/test_query_write.py
git commit -m "feat: writer module: cited prose from checked claims, with fallback rules"
```

---

### Task 2: The combined faithfulness check

**Files:**
- Modify: `lore/query/synthesize.py` (`Faithfulness`, `_CHECK_PROMPT`, `faithfulness_prompt`, a new `validated_sentence_verdicts`, a new `check_answer`; `check_claims` delegates to it)
- Test: `tests/test_query_synthesize.py`

**Interfaces:**
- Consumes: `Sentence` from Task 1.
- Produces:
  - `class Faithfulness(BaseModel): judgements: list[Judgement]; sentence_judgements: list[Judgement] = []`. A sentence judgement's `claim` field holds the sentence number.
  - `faithfulness_prompt(claims, passages, book_titles=None, sentences: Sequence[Sentence] | None = None) -> str`
  - `validated_sentence_verdicts(result, expected: int) -> list[bool] | None`
  - `@dataclass class CheckOutcome: kept: list[Claim]; dropped: list[tuple[Claim, str]]; ran: bool; claim_ok: list[bool]; sentence_ok: list[bool] | None`
  - `async check_answer(claims, passages, check, *, book_titles=None, sentences=None) -> CheckOutcome`
  - `check_claims(...)` is unchanged in signature and return; it calls `check_answer` with `sentences=None`.

Rules:
- Claim verdicts are validated exactly as today. If they don't line up: `ran=False`, keep every claim, `claim_ok` all True, and `sentence_ok=None`.
- Sentence verdicts are validated separately. If they don't line up: `sentence_ok=None` (prose falls back), while the claim verdicts still apply.
- With `sentences=None` the prompt is byte-identical to today's.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_query_synthesize.py`):

```python
# --- prose answers: the combined check -----------------------------------------

from lore.query.synthesize import (  # noqa: E402
    CheckOutcome, Faithfulness, Judgement, check_answer, faithfulness_prompt,
    validated_sentence_verdicts,
)
from lore.query.write import Sentence  # noqa: E402

_PS = (Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Holmes played the violin."),
       Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2, text="Watson kept a bull pup."))
_CS = [Claim(text="Holmes played the violin.", passage=1, quote="Holmes played the violin"),
       Claim(text="Watson kept a bull pup.", passage=2, quote="Watson kept a bull pup")]


def test_without_sentences_the_check_prompt_is_unchanged():
    assert "Sentences:" not in faithfulness_prompt(_CS, _PS)


def test_with_sentences_the_prompt_numbers_them_and_names_their_facts():
    p = faithfulness_prompt(_CS, _PS, sentences=[Sentence(text="Holmes played, and Watson kept a pup.", claims=[1, 2])])
    assert "Sentences:" in p and "1. sentence: Holmes played, and Watson kept a pup." in p
    assert "uses claims 1, 2" in p
    assert "sentence_judgements" in p


def _j(n, v):
    return Judgement(claim=n, verdict=v, reason="r")


async def test_check_answer_applies_both_verdict_lists():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "not_supported")],
                            sentence_judgements=[_j(1, "supported"), _j(2, "not_supported")])
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1]), Sentence(text="b", claims=[1])])
    assert out.ran and out.claim_ok == [True, False] and out.sentence_ok == [True, False]
    assert out.kept == [_CS[0]] and out.dropped[0][0] == _CS[1]


async def test_garbled_sentence_verdicts_keep_the_claim_verdicts():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "not_supported")],
                            sentence_judgements=[_j(2, "supported")])   # misnumbered
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert out.ran and out.claim_ok == [True, False] and out.sentence_ok is None


async def test_garbled_claim_verdicts_keep_every_claim_and_drop_the_prose():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(2, "supported")], sentence_judgements=[_j(1, "supported")])
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert not out.ran and out.kept == _CS and out.claim_ok == [True, True] and out.sentence_ok is None


async def test_a_raising_check_keeps_everything():
    async def check(prompt, schema):
        raise RuntimeError("503")
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert not out.ran and out.kept == _CS and out.sentence_ok is None


def test_validated_sentence_verdicts():
    assert validated_sentence_verdicts(Faithfulness(judgements=[], sentence_judgements=[_j(1, "supported"), _j(2, "Not Supported")]), 2) == [True, False]
    assert validated_sentence_verdicts(Faithfulness(judgements=[], sentence_judgements=[_j(1, "maybe")]), 1) is None
    assert validated_sentence_verdicts(Faithfulness(judgements=[]), 1) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_query_synthesize.py -k "check_answer or sentence or prompt_is_unchanged or numbers_them"`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

In `lore/query/synthesize.py`:

1. `class Faithfulness(BaseModel)`: add `sentence_judgements: list[Judgement] = []`.

2. Add the sentence block (a module constant) and extend `faithfulness_prompt`:

```python
_SENTENCES_BLOCK = """
Sentences:
{sentences}

Then check each numbered sentence of the written answer against the claims
it uses (their text and quotes above). "{supported}": those claims,
together, state everything the sentence says. "{not_supported}": the
sentence says more than they do. Connecting words count: a "because",
"so", "later" or "which led to" that no cited claim states makes the
sentence "{not_supported}". Return these in sentence_judgements, one per
sentence, numbered 1 to {count} in the claim field, with a verdict and a
reason of at most {max_words} words.
"""
```

In `faithfulness_prompt`, add the parameter `sentences: Sequence["Sentence"] | None = None` (import `Sentence` under `TYPE_CHECKING`, or just annotate loosely to avoid a circular import, since write.py doesn't import synthesize). After building the base prompt string:

```python
    prompt = _CHECK_PROMPT.format(...)  # unchanged
    if sentences:
        items = "\n".join(
            f"{i}. sentence: {s.text.strip()}\n   uses claims {', '.join(str(k) for k in s.claims)}"
            for i, s in enumerate(sentences, start=1))
        prompt += _SENTENCES_BLOCK.format(
            sentences=items, count=len(sentences), supported=SUPPORTED,
            not_supported=NOT_SUPPORTED, max_words=MAX_REASON_WORDS)
    return prompt
```

3. Add `validated_sentence_verdicts`:

```python
def validated_sentence_verdicts(result, expected: int) -> list[bool] | None:
    """One supported-flag per sentence, or None if the answer does not line
    up -- the same verifiable-mapping rule as validated_verdicts."""
    items = getattr(result, "sentence_judgements", None)
    if not isinstance(items, list) or not items:
        return None
    if [getattr(item, "claim", None) for item in items] != list(range(1, expected + 1)):
        return None
    flags = []
    for item in items:
        word = _SPACE.sub("_", str(item.verdict).strip().lower().strip(".")).strip("_")
        if word not in (SUPPORTED, NOT_SUPPORTED):
            return None
        flags.append(word == SUPPORTED)
    return flags
```

4. Add `CheckOutcome` and `check_answer`, and make `check_claims` delegate:

```python
@dataclass
class CheckOutcome:
    kept: list[Claim]
    dropped: list[tuple[Claim, str]]
    ran: bool
    claim_ok: list[bool]
    sentence_ok: list[bool] | None


async def check_answer(claims, passages, check, *, book_titles=None, sentences=None) -> CheckOutcome:
    """check_claims, plus a verdict per prose sentence in the same call.
    Claim verdicts and sentence verdicts are validated separately: garbled
    sentence verdicts cost only the prose, never the claim check."""
    claims = list(claims)
    if not claims:
        return CheckOutcome([], [], True, [], None)
    passages = list(passages)
    try:
        result = await check(faithfulness_prompt(claims, passages, book_titles, sentences), Faithfulness)
        verdicts = validated_verdicts(result, len(claims))
        sentence_ok = (validated_sentence_verdicts(result, len(sentences))
                       if sentences and verdicts is not None else None)
    except Exception:  # noqa: BLE001 -- see check_claims
        verdicts, sentence_ok = None, None
    if verdicts is None:
        return CheckOutcome(claims, [], False, [True] * len(claims), None)
    kept, dropped = [], []
    for claim, (supported, reason) in zip(claims, verdicts):
        (kept.append(claim) if supported else dropped.append((claim, reason)))
    return CheckOutcome(kept, dropped, True, [ok for ok, _ in verdicts], sentence_ok)
```

Rewrite `check_claims` to keep its docstring and return `(outcome.kept, outcome.dropped, outcome.ran)` from `await check_answer(claims, passages, check, book_titles=book_titles)`. Add `from dataclasses import dataclass` if it isn't imported.

- [ ] **Step 4: Run the synthesis and agent tests**

Run: `.venv/bin/python -m pytest -q tests/test_query_synthesize.py tests/test_query_agent.py`
Expected: all PASS (existing tests unchanged, new ones pass).

- [ ] **Step 5: Commit**

```bash
git config user.email
git add lore/query/synthesize.py tests/test_query_synthesize.py
git commit -m "feat: one faithfulness call judges claims and prose sentences"
```

---

### Task 3: Prose in synthesis and in Answer

**Files:**
- Modify: `lore/query/models.py` (`Answer` gains `prose`, `answer_style`)
- Modify: `lore/query/synthesize.py` (`_finish`; `synthesize` and `synthesize_parts` take `write: Generate | None = None` and use `_finish`)
- Test: `tests/test_query_synthesize.py`

**Interfaces:**
- Consumes: Task 1 (`write_answer`, `verify_prose`, `assemble`, `compose_text`, `PROSE_ANSWERS`) and Task 2 (`check_answer`, `CheckOutcome`).
- Produces:
  - `Answer.prose: tuple[ProseSentence, ...] = ()` and `Answer.answer_style: str = "claims"`
  - `synthesize(..., check=None, write=None)` and `synthesize_parts(..., check=None, write=None)`
  - `PROSE_FALLBACK_NOTE = "the written answer did not pass verification; showing the checked facts"`

Rules for `_finish(question, sub_questions, kept, evidence, check, write, book_titles) -> tuple[list[Claim], list[Claim], tuple[ProseSentence, ...], str | None, str | None]`, which returns `(kept, cut, prose, skipped_note, prose_note)`:
- Try prose only when `PROSE_ANSWERS` is true, `write is not None`, `check is not None` and `len(kept) >= 2`.
- When trying: call `write_answer(...)`. If it returns None, set `prose_note = PROSE_FALLBACK_NOTE` and run the check exactly as today, without sentences. Otherwise `sentences = verify_prose(prose, len(kept))` and `written = len(prose.sentences)`.
- Run `check_answer(kept, evidence.passages, check, book_titles=..., sentences=sentences or None)` when check is not None. Otherwise keep everything, as today.
- If sentences were attempted, `outcome.ran` is true and `outcome.sentence_ok` is not None: `prose = assemble(sentences, written, outcome.claim_ok, outcome.sentence_ok, [c.passage for c in kept_before_check])`. If the result is None, or the check didn't run, or `sentence_ok` is None: no prose, and `prose_note = PROSE_FALLBACK_NOTE`.
- `skipped_note = CHECK_SKIPPED_NOTE` when the check didn't run, as today.

`synthesize` / `synthesize_parts`, after `kept = _collapse(...)`, call `_finish` in place of the inline `check_claims` block. `synthesize_parts` uses `outcome.dropped` claims as `cut`, as today. The answer:
- with prose: `text=compose_text(prose)`, `prose=prose`, `answer_style="prose"`;
- otherwise: `text=_compose(kept)`, `answer_style="claims"`, and `prose_note` joined into the notes.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_query_synthesize.py`; reuse `_PS` and `_CS` from Task 2):

```python
from lore.query import write as write_module  # noqa: E402
from lore.query.synthesize import PROSE_FALLBACK_NOTE  # noqa: E402
from lore.query.write import Prose  # noqa: E402

_EV = Evidence(edges=(), passages=_PS, truncated=False)
_TWO = Synthesis(claims=_CS, can_answer=True)


def _gen(reply):
    calls = []
    async def generate(prompt, schema):
        calls.append(prompt)
        return reply
    return generate, calls


def _check(claim_v, sent_v):
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(i + 1, v) for i, v in enumerate(claim_v)],
                            sentence_judgements=[_j(i + 1, v) for i, v in enumerate(sent_v)])
    return check


async def test_prose_becomes_the_answer():
    gen, _ = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[Sentence(text="Holmes played the violin while Watson kept a bull pup.", claims=[1, 2])]))
    a = await synthesize("Habits?", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["supported"]), write=write)
    assert a.answer_style == "prose" and a.prose[0].passages == (1, 2)
    assert a.text == "Holmes played the violin while Watson kept a bull pup. [1][2]"
    assert len(a.claims) == 2 and len(wcalls) == 1


async def test_failing_prose_falls_back_to_claims_with_a_note():
    gen, _ = _gen(_TWO)
    write, _ = _gen(Prose(sentences=[Sentence(text="x", claims=[1]), Sentence(text="y", claims=[2])]))
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["not_supported"] * 2), write=write)
    assert a.answer_style == "claims" and a.prose == () and PROSE_FALLBACK_NOTE in a.note


async def test_no_writer_call_for_a_single_claim():
    gen, _ = _gen(Synthesis(claims=[_CS[0]], can_answer=True))
    write, wcalls = _gen(Prose(sentences=[]))
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported"], []), write=write)
    assert wcalls == [] and a.answer_style == "claims"


async def test_no_writer_call_without_a_check():
    gen, _ = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[]))
    a = await synthesize("q", _EV, gen, mode="graph", check=None, write=write)
    assert wcalls == [] and a.answer_style == "claims"


async def test_prose_off_is_todays_behaviour(monkeypatch):
    monkeypatch.setattr(write_module, "PROSE_ANSWERS", False)
    gen, calls = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[Sentence(text="x", claims=[1])]))
    check_prompts = []
    async def check(prompt, schema):
        check_prompts.append(prompt)
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "supported")])
    a = await synthesize("q", _EV, gen, mode="graph", check=check, write=write)
    assert wcalls == [] and a.answer_style == "claims"
    assert "Sentences:" not in check_prompts[0]


async def test_a_writer_failure_keeps_the_claim_check():
    gen, _ = _gen(_TWO)
    async def write(prompt, schema):
        raise RuntimeError("quota")
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported", "not_supported"], []), write=write)
    assert a.answer_style == "claims" and len(a.claims) == 1 and PROSE_FALLBACK_NOTE in a.note
```

If the synthesis tests already use a `Synthesis` helper or `Evidence`, reuse the existing imports. Add `Evidence` and `Synthesis` to imports if they aren't already imported at module level in that file.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_query_synthesize.py -k "prose or writer or single_claim or without_a_check"`
Expected: FAIL.

- [ ] **Step 3: Implement**

1. In `lore/query/models.py`, `Answer`: add `prose: tuple[ProseSentence, ...] = ()` and `answer_style: str = "claims"` after `seeds`.

2. In `lore/query/synthesize.py`:
   - Import `from lore.query import write as write_module` and `from lore.query.write import assemble, compose_text, verify_prose, write_answer`.
   - Add `PROSE_FALLBACK_NOTE`.
   - Add `_finish` per the rules above. Read `write_module.PROSE_ANSWERS` at call time, so tests can monkeypatch it.
   - In `synthesize`: add the parameter `write: Generate | None = None`. Replace the `if check is not None and kept:` block with:

```python
    kept, _cut, prose, skipped, prose_note = await _finish(
        question, list(checklist) or [question], kept, evidence, check, write, book_titles)
    if not kept:
        return answer(REFUSAL)
    partial = None if result.can_answer else PARTIAL_NOTE
    if prose:
        return replace(answer(compose_text(prose), partial, skipped, claims=tuple(kept)),
                       prose=prose, answer_style="prose")
    return answer(_compose(kept), partial, skipped, prose_note, claims=tuple(kept))
```

   - In `synthesize_parts`: add `write` and pass it in the early `synthesize(...)` delegation. Replace its check block with `kept, cut, prose, skipped, prose_note = await _finish(question, sub_questions, kept, evidence, check, write, book_titles)`, keep the `missing` computation using `cut`, and return prose or claims the same way.
   - `answer(...)` builds an `Answer` dataclass. Use `dataclasses.replace` to set `prose` and `answer_style` (import `replace`).

   Write `_finish`:

```python
async def _finish(question, sub_questions, kept, evidence, check, write, book_titles):
    """Writer (when it applies) and the faithfulness check, in one place for
    both synthesize paths. Returns (kept, cut, prose, skipped_note, prose_note)."""
    try_prose = (write_module.PROSE_ANSWERS and write is not None and check is not None
                 and len(kept) >= 2)
    sentences, written, prose_note = None, 0, None
    if try_prose:
        drafted = await write_answer(question, sub_questions, kept, evidence.passages, write,
                                     book_titles=book_titles)
        if drafted is None:
            prose_note = PROSE_FALLBACK_NOTE
        else:
            sentences, written = verify_prose(drafted, len(kept)), len(drafted.sentences)
    if check is None or not kept:
        return kept, [], (), None, prose_note
    passage_of = [c.passage for c in kept]
    outcome = await check_answer(kept, evidence.passages, check, book_titles=book_titles,
                                 sentences=sentences or None)
    skipped = None if outcome.ran else CHECK_SKIPPED_NOTE
    prose = ()
    if sentences is not None:
        built = (assemble(sentences, written, outcome.claim_ok, outcome.sentence_ok, passage_of)
                 if sentences and outcome.ran and outcome.sentence_ok is not None else None)
        if built:
            prose = built
        else:
            prose_note = PROSE_FALLBACK_NOTE
    return outcome.kept, [c for c, _ in outcome.dropped], prose, skipped, prose_note
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_query_synthesize.py tests/test_query_write.py tests/test_query_agent.py`
Expected: all PASS. If an existing test asserts a note string exactly and now sees `PROSE_FALLBACK_NOTE`, it doesn't pass `write` (callers that don't pass `write` never try prose), so that shouldn't happen. If it does, the test isn't passing `write=None`. Fix `_finish`, not the test.

- [ ] **Step 5: Commit**

```bash
git config user.email
git add lore/query/models.py lore/query/synthesize.py tests/test_query_synthesize.py
git commit -m "feat: synthesis writes a cited prose answer and falls back to claims"
```

---

### Task 4: Agent wiring, the compose step, the MCP shape, and the leak test

**Files:**
- Modify: `lore/query/agent.py` (`QueryModels.write`; `make_query_models` sets it; `_synthesize` passes it; the compose step)
- Modify: `lore/mcp/shapes.py` (`ProseOut`; `SearchResult.prose` and `.answer_style`; `to_search_result`)
- Modify: `tests/test_query_synthesize.py` (the leak test covers `WRITER_PROMPT`)
- Test: `tests/test_query_agent.py`, `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `QueryModels.write: Generate | None = None`
  - the event stream: `step compose start/done` between `write` and `check`, with detail `"N sentences"`;
  - `SearchResult.prose: list[ProseOut]` where `ProseOut{text, claims: list[int], passages: list[int], new_paragraph: bool}`, and `SearchResult.answer_style: str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_agent.py`, reusing the Phase 5a helpers already there (`_conn`, `VectorLance`, `EntityKuzu`, `_edge_row`, `_row`, `_scripted`, `Q`, `QUERY_PREFIX`, `TemplateChoice`):

```python
# --- prose answers: the compose step ------------------------------------------

async def test_the_compose_step_sits_between_write_and_check(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace
    from lore.query.models import Claim
    from lore.query.synthesize import Faithfulness, Judgement, Synthesis
    from lore.query.write import Prose, Sentence

    conn = _conn(tmp_path)
    _chunk(conn, "b1:2:0", "Watson kept a bull pup in the rooms.", 2)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1), _row("b1:2:0", [], 0.25, 2)]},
                        {"b1:1:0": Q, "b1:2:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    two = Synthesis(claims=[Claim(text="Holmes talked the case over with Watson.", passage=1, quote=QUOTE),
                            Claim(text="Watson kept a bull pup.", passage=2, quote="Watson kept a bull pup")],
                    can_answer=True)
    models, _ = _scripted({QUERY_PREFIX + "who?": Q}, ["who?"],
                          [TemplateChoice(template="neighbours", entities=[1])], two)

    async def write(prompt, schema):
        return Prose(sentences=[Sentence(text="Holmes discussed the case with Watson, who kept a bull pup.", claims=[1, 2])])

    async def check(prompt, schema):
        return Faithfulness(judgements=[Judgement(claim=1, verdict="supported"), Judgement(claim=2, verdict="supported")],
                            sentence_judgements=[Judgement(claim=1, verdict="supported")])

    models = dc_replace(models, write=write, check=check)
    events = []
    answer = await answer_question(conn, kuzu, lance, "who?", models, on_event=events.append)
    conn.close()
    steps = [(e["name"], e["state"]) for e in events if e["type"] == "step"]
    assert steps[-6:] == [("write", "start"), ("write", "done"), ("compose", "start"),
                          ("compose", "done"), ("check", "start"), ("check", "done")]
    assert answer.answer_style == "prose"
```

Check `QUOTE` exists in the test module and is a substring of `TEXT` (the `b1:1:0` chunk). Adapt if the file uses different constants.

Append to `tests/test_mcp_tools.py`:

```python
def test_search_result_carries_prose():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, Claim, ProseSentence
    answer = Answer(text="A. [1]", passages=(), mode="graph", template=None, note=None,
                    claims=(Claim(text="A", passage=1, quote="a b c d"),),
                    prose=(ProseSentence("A.", (1,), (1,)),), answer_style="prose")
    out = to_search_result(answer, {}).model_dump()
    assert out["answer_style"] == "prose"
    assert out["prose"] == [{"text": "A.", "claims": [1], "passages": [1], "new_paragraph": False}]
```

In `tests/test_query_synthesize.py`, extend `test_the_prompt_leaks_nothing_from_the_eval_set`: add `write_module.WRITER_PROMPT` (from `from lore.query import write as write_module`) to the text it scans.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_query_agent.py -k compose tests/test_mcp_tools.py -k prose`
Expected: FAIL.

- [ ] **Step 3: Implement**

1. `QueryModels`: add `write: Generate | None = field(default=None)` after `check`. In `make_query_models`, pass `write=synthesize_with` next to `check=synthesize_with`.

2. `_synthesize`: pass `write=models.write` to both `synthesize(...)` and `synthesize_parts(...)`.

3. The compose step. Extend `_CheckSteps` (rename it `_AnswerSteps`, keeping the name `_CheckSteps` as an alias if other code references it). `wrap(models)` also wraps `models.write`, when it isn't None:

```python
        if models.write is not None:
            write = models.write

            async def composing(prompt, schema):
                self.composed = True
                emit(self.on_event, "step", name="write", state="done", part=None, detail=None)
                emit(self.on_event, "step", name="compose", state="start", part=None, detail=None)
                reply = None
                try:
                    reply = await write(prompt, schema)
                    return reply
                finally:
                    n = len(getattr(reply, "sentences", None) or [])
                    emit(self.on_event, "step", name="compose", state="done", part=None,
                         detail=f"{n} sentences" if n else "no prose")
            models = replace(models, write=composing)
```

   In the check announcer, emit `write done` only `if not self.composed`. Initialise `self.composed = False`. Where `answer_question` closes the write step after synthesis (`if checks is None or not checks.ran:` emits `write done`), also skip it when `checks.composed` is true, so write is never closed twice. Read the current `answer_question` tail and adapt it to this rule: `write done` is emitted exactly once — by the compose wrapper, the check announcer or the tail, whichever runs first.

4. `lore/mcp/shapes.py`:

```python
class ProseOut(BaseModel):
    text: str
    claims: list[int]
    passages: list[int]
    new_paragraph: bool = False
```

   `SearchResult`: add `prose: list[ProseOut] = []` and `answer_style: str = "claims"`. In `to_search_result`, map `prose=[ProseOut(text=s.text, claims=list(s.claims), passages=list(s.passages), new_paragraph=s.new_paragraph) for s in answer.prose]` and `answer_style=answer.answer_style`.

- [ ] **Step 4: Run the tests and the full suite**

Run: `.venv/bin/python -m pytest -q tests/test_query_agent.py tests/test_mcp_tools.py tests/test_query_synthesize.py tests/test_web_ask.py`, then `.venv/bin/python -m pytest -q tests` (~15 min; long timeout or background).
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git config user.email
git add lore/query/agent.py lore/mcp/shapes.py tests/
git commit -m "feat: agent and MCP carry prose answers; compose step in the live stream"
```

---

### Task 5: The web UI: prose with citation chips over collapsed evidence

**Files:**
- Modify: `web/src/types.ts`, `web/src/messages.ts`, `web/src/components/ChatPane.tsx`, `web/src/components/Claims.tsx`, `web/src/App.tsx`, `web/src/components/GraphPane.tsx`, `web/src/graphView.ts`, `web/src/theme.css`
- Create: `web/src/components/Prose.tsx`
- Test: `web/src/components/Prose.test.tsx` (new), `web/src/components/ChatPane.test.tsx`, `web/src/graphView.test.ts`

**Interfaces:**
- Consumes: the MCP/SSE `answer` result with `prose?: ProseSentence[]` and `answer_style?: "prose" | "claims"`.
- Produces:
  - `type StepName` gains `"compose"`, and `STEP_ORDER` = `["split","seeds","search","rank","write","compose","check"]`;
  - `interface ProseSentence { text: string; claims: number[]; passages: number[]; new_paragraph: boolean }`;
  - `SearchResult` gains `prose?: ProseSentence[]; answer_style?: "prose" | "claims"`;
  - `<Prose result onHoverChunks={(chunks: string[]) => void} />`;
  - hover becomes multi-chunk: `ViewOptions.hoverChunks: string[]` replaces `hoverChunk`, and `onHoverChunks(chunks: string[])` replaces `onHoverChunk` in ChatPane, Claims and App. An empty array means nothing is hovered.

Messages: add `STEP_LABEL.compose = "Writing the answer in prose"`, `UI.evidence = (n: number) => `Evidence (${n} ${n === 1 ? "claim" : "claims"})``, and `UI.proseFallback = "Showing the checked facts; the written answer didn't pass verification."`.

- [ ] **Step 1: Write the failing tests**

`web/src/components/Prose.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { Prose } from "./Prose";
import type { SearchResult } from "../types";

const result = {
  answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: ["q"],
  claims: [{ text: "Hope loved Lucy.", passage: 3, quote: "Hope loved Lucy Ferrier" },
           { text: "Lucy married Drebber.", passage: 5, quote: "forced to marry Drebber" }],
  passages: [{ n: 3, chunk_id: "c3", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 13, text: "Hope loved Lucy Ferrier dearly." },
             { n: 5, chunk_id: "c5", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 14, text: "She was forced to marry Drebber." }],
  edges: [], seeds: [], answer_style: "prose",
  prose: [{ text: "Hope loved Lucy, who was forced to marry Drebber.", claims: [1, 2], passages: [3, 5], new_paragraph: false },
          { text: "He swore revenge.", claims: [1], passages: [3], new_paragraph: true }],
} as SearchResult;

test("renders paragraphs with a chip per passage", () => {
  const { container } = render(<Prose result={result} onHoverChunks={() => {}} />);
  expect(container.querySelectorAll("p.prose")).toHaveLength(2);
  expect(screen.getAllByRole("button", { name: /\[3\]/ })).toHaveLength(2);
  expect(screen.getByRole("button", { name: /\[5\]/ })).toBeInTheDocument();
});

test("a chip opens its passage with the cited quote marked", () => {
  render(<Prose result={result} onHoverChunks={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: /\[5\]/ }));
  expect(screen.getByText("forced to marry Drebber").tagName).toBe("MARK");
});

test("hovering a sentence reports every chunk it cites", () => {
  const onHover = vi.fn();
  render(<Prose result={result} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText(/who was forced to marry Drebber/));
  expect(onHover).toHaveBeenCalledWith(["c3", "c5"]);
  fireEvent.mouseLeave(screen.getByText(/who was forced to marry Drebber/));
  expect(onHover).toHaveBeenLastCalledWith([]);
});
```

Append to `web/src/components/ChatPane.test.tsx`, using the file's existing `askWith` helper and imports:

```tsx
test("a prose answer shows the prose and a collapsed evidence list", () => {
  const events = (single as AskEvent[]).map((e) => e.type === "answer" ? { ...e, result: { ...e.result, answer_style: "prose",
    prose: [{ text: "A written sentence.", claims: [1], passages: [e.result.claims[0].passage], new_paragraph: false }] } } : e);
  render(<ChatPane ask={askWith(events as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText("A written sentence.")).toBeInTheDocument();
  const details = screen.getByText(/^Evidence \(/).closest("details")!;
  expect(details).not.toHaveAttribute("open");
});

test("an old result without prose renders the claim view", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByText(/^Evidence \(/)).toBeNull();
  expect(document.querySelectorAll("ul.claims li").length).toBeGreaterThan(0);
});

test("a prose fallback shows the note", () => {
  const events = (single as AskEvent[]).map((e) => e.type === "answer" ? { ...e, result: { ...e.result, answer_style: "claims",
    note: "the written answer did not pass verification; showing the checked facts" } } : e);
  render(<ChatPane ask={askWith(events as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(/didn't pass verification/)).toBeInTheDocument();
});
```

Update the existing ChatPane, Claims, App and graphView tests for the rename `onHoverChunk` → `onHoverChunks` (array) and `hoverChunk` → `hoverChunks`. In `graphView.test.ts`, a test passing `hoverChunk: "c2"` becomes `hoverChunks: ["c2"]`. Add one test where `hoverChunks: ["c1", "c2"]` marks links of both chunks `hot`.

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/components/Prose.test.tsx src/components/ChatPane.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`web/src/components/Prose.tsx`:

```tsx
import { useState } from "react";
import { highlightQuote } from "../highlight";
import type { SearchResult } from "../types";

export function Prose({ result, onHoverChunks }: { result: SearchResult; onHoverChunks: (chunks: string[]) => void }) {
  const [open, setOpen] = useState<string | null>(null);   // "<sentence>:<passage>"
  const byN = new Map(result.passages.map((p) => [p.n, p]));
  const prose = result.prose ?? [];
  const paragraphs: { s: (typeof prose)[number]; i: number }[][] = [[]];
  prose.forEach((s, i) => {
    if (s.new_paragraph && paragraphs[paragraphs.length - 1].length) paragraphs.push([]);
    paragraphs[paragraphs.length - 1].push({ s, i });
  });
  return (
    <div className="prose-answer">
      {paragraphs.map((para, pi) => (
        <div key={pi}>
          <p className="prose">
            {para.map(({ s, i }) => {
              const chunks = s.passages.map((n) => byN.get(n)?.chunk_id).filter(Boolean) as string[];
              return (
                <span key={i} className="sentence" onMouseEnter={() => onHoverChunks(chunks)} onMouseLeave={() => onHoverChunks([])}>
                  {s.text}{" "}
                  {s.passages.map((n) => (
                    <button key={n} className="cite" type="button" aria-expanded={open === `${i}:${n}`}
                      onClick={() => setOpen(open === `${i}:${n}` ? null : `${i}:${n}`)}>{`[${n}]`}</button>
                  ))}{" "}
                </span>
              );
            })}
          </p>
          {para.map(({ s, i }) => s.passages.map((n) => {
            if (open !== `${i}:${n}`) return null;
            const p = byN.get(n);
            if (!p) return null;
            const quote = s.claims.map((k) => result.claims[k - 1]).find((c) => c && c.passage === n)?.quote ?? "";
            return (
              <blockquote key={`${i}:${n}`} className="passage">
                <span className="src">{`${p.book_title ?? p.book_id} · chapter ${p.chapter_seq} · ${p.chunk_id}`}</span>
                {highlightQuote(p.text, quote).map((x, j) => x.mark ? <mark key={j}>{x.text}</mark> : <span key={j}>{x.text}</span>)}
              </blockquote>
            );
          }))}
        </div>
      ))}
    </div>
  );
}
```

`ChatPane.tsx`: the prop is renamed `onHoverChunks`. In the answer block:

```tsx
{r && r.claims.length === 0 && <p className="note">{UI.refusal}</p>}
{r && r.claims.length > 0 && r.answer_style === "prose" && (r.prose?.length ?? 0) > 0 ? (
  <>
    <Prose key={ask.run} result={r} onHoverChunks={onHoverChunks} />
    <details className="evidence">
      <summary>{UI.evidence(r.claims.length)}</summary>
      <Claims key={ask.run} result={r} onHoverChunks={onHoverChunks} />
    </details>
  </>
) : r && r.claims.length > 0 && (
  <>
    {(r.note ?? "").includes("written answer did not pass verification") && <p className="note">{UI.proseFallback}</p>}
    <Claims key={ask.run} result={r} onHoverChunks={onHoverChunks} />
  </>
)}
```

`Claims.tsx`: the prop becomes `onHoverChunks: (chunks: string[]) => void`. Enter calls `onHoverChunks(p ? [p.chunk_id] : [])`; leave calls `onHoverChunks([])`.

`App.tsx`: `const [hoverChunks, setHoverChunks] = useState<string[]>([])`. Pass `onHoverChunks={setHoverChunks}` to ChatPane and `hoverChunks={hoverChunks}` to GraphPane.

`GraphPane.tsx` / `graphView.ts`: rename to `hoverChunks: string[]`, with `hot: chunkIds.some((c) => options.hoverChunks.includes(c))`. Update the `useMemo` deps.

`theme.css`: add `.prose{font:17px/1.6 var(--f-book);margin:0 0 .6em} .prose .sentence:hover{background:var(--glow-soft)} details.evidence{margin-top:8px;border-top:1px solid var(--line);padding-top:6px} details.evidence summary{cursor:pointer;color:var(--muted);font-size:13px}`.

`types.ts` / `messages.ts`: as in Interfaces.

- [ ] **Step 4: Run the frontend checks**

Run: `cd web && npm test && npx tsc --noEmit && npm run build`
Expected: all PASS, build OK.

- [ ] **Step 5: Commit**

```bash
git config user.email
git add web/src/
git commit -m "feat: chat pane shows prose answers with citation chips over collapsed evidence"
```

---

### Task 6: Eval tooling for the prose comparison

**Files:**
- Modify: `evals/answer_quality/run_pair.py` (arm setting `prose_answers`)
- Modify: `evals/answer_quality/grade_pair.py` (`packet --answer-only`; readability preference in `score`; a `styles` command)
- Modify: `evals/answer_quality/GRADING.md` (a section on grading prose and the pre-registered bar)
- Test: `tests/test_grade_pair.py` (new)

**Interfaces:**
- Produces:
  - run_pair arm spec `prose_answers=false`;
  - `grade_pair.py packet ... --answer-only`: each A/B is `{"answer": text}` only, so graders judge the text the user sees;
  - `score`: when grades carry `"preferred": "A" | "B" | "tie"`, it prints preference wins and losses for arm 2 vs arm 1 with a sign test;
  - `grade_pair.py styles --run <label> --arms <a1> <a2>`: prints the `answer_style` counts and the prose fallback rate per arm.

- [ ] **Step 1: Write the failing tests**

`tests/test_grade_pair.py`:

```python
"""grade_pair: answer-only packets, readability preference, styles."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "answer_quality"))

import grade_pair  # noqa: E402


def _runs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    rec = lambda i, style: {"id": i, "answer": f"answer {i} {style}", "answer_style": style,
                            "claims": [{"text": "c", "quote": "q q q q", "passage": 1}]}
    (runs / "r.a.jsonl").write_text("\n".join(json.dumps(rec(i, "claims")) for i in ("Q1", "Q2")) + "\n")
    (runs / "r.b.jsonl").write_text("\n".join(json.dumps(rec(i, s)) for i, s in (("Q1", "prose"), ("Q2", "claims"))) + "\n")
    qs = tmp_path / "q.json"
    qs.write_text(json.dumps([{"id": "Q1", "question": "one?"}, {"id": "Q2", "question": "two?"}]))
    monkeypatch.setattr(grade_pair, "HERE", tmp_path)
    monkeypatch.setitem(grade_pair.SETS, "dev", qs)
    return tmp_path


def test_answer_only_packets_carry_just_the_answer(tmp_path, monkeypatch):
    _runs(tmp_path, monkeypatch)
    grade_pair.packet("r", ["a", "b"], "dev", tmp_path / "out", seed=1, answer_only=True)
    item = json.loads((tmp_path / "out" / "packet.json").read_text())[0]
    assert set(item["A"]) == {"answer"} and set(item["B"]) == {"answer"}


def test_styles_reports_the_fallback_rate(tmp_path, monkeypatch, capsys):
    _runs(tmp_path, monkeypatch)
    grade_pair.styles("r", ["a", "b"])
    out = capsys.readouterr().out
    assert "b: prose 1, claims 1" in out and "fallback 50%" in out


def test_score_counts_the_readability_preference(tmp_path, capsys):
    out = tmp_path / "g"
    (out / "sealed").mkdir(parents=True)
    (out / "sealed" / "key.json").write_text(json.dumps({"Q1": {"A": "a", "B": "b"}, "Q2": {"A": "b", "B": "a"}}))
    g = lambda f: {"facts_covered": f, "facts_total": 3, "false_claims": 0, "duplicates": 0, "verdict": "partly"}
    (out / "grades.json").write_text(json.dumps([
        {"id": "Q1", "A": g(1), "B": g(2), "preferred": "B"},
        {"id": "Q2", "A": g(2), "B": g(1), "preferred": "A"}]))
    grade_pair.score(out)
    text = capsys.readouterr().out
    assert "readability: b preferred 2, a preferred 0, tie 0" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_grade_pair.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

`run_pair.py` `ARM_SETTINGS`: add `"prose_answers": ("lore.query.write", "PROSE_ANSWERS", lambda v: v.lower() in ("1", "true", "yes", "on")),`.

`grade_pair.py`:
- `packet(run, arms, qset, out, seed, answer_only=False)`: when `answer_only`, the item for each label is `{"answer": r.get("answer")}`. Add the CLI flag `--answer-only`.
- In `score`, after the facts lines:

```python
    prefs = Counter()
    for g in grades:
        p = g.get("preferred")
        if p in ("A", "B"):
            prefs[key[g["id"]][p]] += 1
        elif p == "tie":
            prefs["tie"] += 1
    if prefs:
        w, l = prefs[arms[1]], prefs[arms[0]]
        print(f"readability: {arms[1]} preferred {w}, {arms[0]} preferred {l}, tie {prefs['tie']};"
              f" sign test p = {sign_test(w, l):.3f}")
```

- Add:

```python
def styles(run: str, arms: list[str]) -> None:
    for a in arms:
        recs = list(_answers(HERE / "runs" / f"{run}.{a}.jsonl").values())
        c = Counter(r.get("answer_style", "claims") for r in recs)
        eligible = sum(1 for r in recs if len(r.get("claims", [])) >= 2)
        fell = sum(1 for r in recs if len(r.get("claims", [])) >= 2 and r.get("answer_style") != "prose")
        rate = f"{100 * fell // eligible}%" if eligible else "n/a"
        print(f"{a}: prose {c['prose']}, claims {c['claims']}; fallback {rate} of {eligible} eligible")
```

  with a CLI subcommand `styles --run --arms`.

`GRADING.md`: add a section "Grading prose answers":
- graders judge only the answer text in an `--answer-only` packet;
- a key fact counts only if the answer text states it;
- false claims are judged sentence by sentence;
- duplicates are counted the same way as today;
- each pair gets `"preferred": "A" | "B" | "tie"`, meaning which reads better as an answer to the question (clear, direct, well-organised), judged independently of correctness;
- the pre-registered bar, copied from the spec §6.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_grade_pair.py`
Expected: PASS.

- [ ] **Step 5: Final verification and commit**

Run: `.venv/bin/python -m pytest -q tests` (full) and `cd web && npm test && npm run build`.

```bash
git config user.email
git add evals/answer_quality/ tests/test_grade_pair.py
git commit -m "test: eval tooling for prose answers: answer-only packets, readability, styles"
git push -q origin prose-answers
```

---

## After the plan: the measured run (needs a Gemini reset; ~1,000 requests)

```bash
P=evals/answer_quality/run_pair.py
.venv/bin/python $P --label r6 --arm prose_answers=false --arm control --trace
.venv/bin/python $P --label h6 --set heldout --arm prose_answers=false --arm control
.venv/bin/python evals/answer_quality/grade_pair.py styles --run r6 --arms prose_answersfalse control
.venv/bin/python evals/answer_quality/grade_pair.py packet --run r6 --arms prose_answersfalse control --set dev --out <dir> --answer-only
```

Grade blind (with `preferred`), then `score`, and apply the bar from spec §6.

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3.1–3.2 claims, then the writer (≥2 claims, once per question) | 1, 3 |
| §3.3 code citation check | 1 |
| §3.4 combined check, with connectives as content | 2 |
| §3.5 assembly and fallbacks | 1, 3 |
| §3.6 result fields | 3, 4 |
| `PROSE_ANSWERS` | 1, 3 |
| §4 modules | 1–4 |
| §5 UI | 5 |
| §6 tests | every task |
| §6 eval and bar | 6, plus the run section |
| Leak test | 4 |
| Compose step | 4, 5 |

**Placeholder scan:** none. Two steps tell the implementer to check existing test constants (`QUOTE`, the `askWith` helper) and the current tail of `answer_question` before adapting. Those live in code this plan doesn't restate.

**Type consistency:**
- `ProseSentence` (Python dataclass, TypeScript interface, `ProseOut`) uses the same fields: `text`, `claims`, `passages`, `new_paragraph`.
- `answer_style` is `"prose" | "claims"` everywhere.
- `hoverChunks: string[]` is used consistently across App, ChatPane, Claims, Prose, GraphPane and graphView.
