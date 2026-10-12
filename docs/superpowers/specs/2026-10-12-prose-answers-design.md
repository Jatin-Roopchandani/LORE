# Prose answers: a written, cited answer on top of checked claims

**Status:** approved in chat 2026-10-12, section by section.

**Goal:** answers read as a coherent response, not a stack of quoted lines. "Who is
Sherlock Holmes?" gets a short profile. LORE's promise holds: no sentence reaches the
user unless it traces through a checked claim to a word-for-word quote.

**Done when:**
- the paired eval (§6) passes the pre-registered bar;
- the web UI shows the written answer with clickable citations and the claims under a
  collapsed "Evidence" section;
- the MCP `lore_search` returns the same prose.

---

## 1. Today, and the problem

`lore/query/synthesize.py`:
1. **Claims:** the model proposes claims per part. Each claim is one sentence in its own
   words, plus a passage number and a verbatim quote.
2. **Quote check:** code verifies each quote (`verify_claims`).
3. **Collapse:** near-duplicates collapse (`_collapse`).
4. **Faithfulness check:** one batched Gemini call judges each claim against its passage
   (`check_claims`).
5. **Compose:** the survivors are joined into `answer` (`_compose`).

Nothing writes an answer. Broad questions come back as a list of facts whose wording
stays close to the quotes.

## 2. Decisions (taken in chat)

1. **Everywhere (option A).** The written answer becomes `answer` in both the web UI and
   MCP `lore_search`. The claims stay in the result as structured evidence.
2. **Adaptive length (option B).**
   - A focused question gets 2–4 sentences.
   - A broad question gets up to two short paragraphs, about 200 words at most.
   - Plain neutral prose, with every sentence cited.
3. **Writer over checked claims, one combined check (option 1).**
   - The writer sees only the claims that passed the quote check.
   - The existing faithfulness call is extended to judge the sentences too, in the same
     request.
   - Cost: +1 Gemini request per question.

Rejected:
- **Two separate checks:** +2 requests per question.
- **Code-only joining:** doesn't meet the goal.

## 3. Pipeline

1. **Claims.** Per part: write claims, verify quotes, collapse near-duplicates.
   Unchanged.
2. **Writer.** One call per question, not per part, and only when there are ≥2 claims.
   - Input: the original question, the sub-questions, and the numbered claims. Each
     claim carries its text, quote and book/chapter.
   - Output: `Prose{sentences: [Sentence{text, claims: [int]}]}`.
3. **Code check of the prose (`verify_prose`).** Drops any sentence with no citation, or
   with a citation outside 1..len(claims).
4. **One combined faithfulness call.**
   - The prompt keeps today's claims block and adds a "Sentences" block.
   - It returns claim verdicts as today, plus sentence verdicts.
   - Sentence rule: a sentence is supported only if the claims it cites *together* state
     everything in it. Connecting words that add a cause, order or consequence
     ("because", "later", "so", "which led to") are content: if no cited claim states
     that link, the sentence is unsupported.
   - Validation works like `validated_verdicts`: any count or numbering mismatch means
     the check did not run.
5. **Assembly (`assemble`).**
   - Drop the claims judged unsupported, as today.
   - Drop every sentence that cites a dropped claim.
   - Then count the sentences judged unsupported on their own:
     - 0 or 1: remove it;
     - more than 1: fall back.
   - Also fall back if fewer than half of the writer's sentences survive.
   - Map each sentence's claim numbers to the passage numbers of those claims.
6. **Result.**
   - `answer` is the surviving prose, sentences joined with their citations.
   - On fallback, `answer` is today's claim list.

**When the writer is skipped or falls back:**
- **Refusal or empty answer:** no writer call.
- **Exactly one claim:** no writer call; the claim is the answer.
- **Writer raises or returns something malformed:** fall back to claims, and add a note.
- **The check did not run:** fall back to claims. Unchecked prose is riskier than
  unchecked claims, so the prose is not shown.
- **Check drops all claims:** refusal, as today.

**Switch:** `PROSE_ANSWERS = True` in `lore/query/write.py`, with the same pattern as
the other synthesis flags. With it off, behaviour is byte-identical to today.

## 4. Code

**`lore/query/write.py` (new)**
- `WRITER_PROMPT`: general rules only, with no eval text; the leak test covers it.
  1. Answer the question directly in the first sentence.
  2. Use only the numbered facts; never add anything they don't say, including what
     you know about the story.
  3. Cite every sentence with the fact numbers it uses.
  4. Don't state a fact twice.
  5. When the facts come from more than one book, say which book.
  6. Length: 2–4 sentences for a narrow question, up to two short paragraphs (about 200
     words at most) for a broad one.
  7. Keep each fact's certainty: "seemed" or "was suspected" stays as it is.
- Pydantic schemas `Sentence{text: str, claims: list[int], new_paragraph: bool = False}` and
  `Prose{sentences: list[Sentence]}`. The writer sets `new_paragraph` on the first sentence
  of a second paragraph.
- `async write_answer(question, sub_questions, claims, passages, generate, *,
  book_titles) -> Prose | None`: returns None on any failure.
- `verify_prose(prose, n_claims) -> tuple[list[Sentence], list[tuple[Sentence, str]]]`.
- `assemble(sentences, claim_ok: list[bool], sentence_ok: list[bool],
  passage_of: list[int]) -> list[ProseSentence] | None`: None means fall back.

**`lore/query/synthesize.py`**
- The faithfulness prompt and `validated_verdicts` take an optional sentence list.
  `check_claims` returns sentence verdicts too.
- `synthesize` and `synthesize_parts` call the writer after the collapse and before the
  check, then assemble.

**`lore/query/models.py` and `lore/mcp/shapes.py`**
- `Answer` gains `prose: tuple[ProseSentence, ...] = ()` and
  `answer_style: str = "claims"`.
- `ProseSentence` is `{text, claims: tuple[int, ...], passages: tuple[int, ...],
  new_paragraph: bool}`.
- `to_search_result` emits `prose` (a list) and `answer_style`. These are additive: old
  callers are unaffected.

**`lore/query/agent.py`**
- The event hook gains a `compose` step (start/done) between `write` and `check`.
- Detail: `N sentences`, or `fell back to facts`.

**Web.**
- `web/src/types.ts`:
  - `StepName` gains `"compose"` between `write` and `check` in `STEP_ORDER`;
  - `SearchResult` gains `prose` and `answer_style`.
- `messages.ts`:
  - the step label "Writing the answer in prose";
  - the fallback note "Showing the checked facts; the written answer didn't pass
    verification.";
  - an Evidence label.

## 5. UI

**`answer_style === "prose"`:**
- **The answer:** the written answer comes first, in the book font, as one or two
  paragraphs, split where a sentence has `new_paragraph`.
- **Citation chips:** each sentence ends with chips (`[n]`, one per passage). Clicking a
  chip opens that passage with the cited claim's quote highlighted.
- **Hover:** hovering a sentence lights the graph edges behind every claim it cites,
  through the same hover-chunk mechanism as claims today.
- **Evidence:** a collapsed "Evidence (N claims)" `<details>` holds today's claim list,
  unchanged.

**`answer_style === "claims"`:** today's view.
- On fallback, add the quiet note.
- History recorded before this change replays as claims.

**Steps list:** gains the "Writing the answer in prose" row.

**Graph:** unchanged.

**Wording:** all new strings live in `messages.ts`. No model names appear unless
show_internals is on.

## 6. Testing and evaluation

**Code tests (no Gemini).**
- `write.py`:
  - `verify_prose` drops sentences with no citation or an out-of-range citation.
  - `assemble`:
    - a failed claim takes its sentences with it;
    - one failing sentence is removed;
    - two or more fall back;
    - fewer than half surviving falls back;
    - claim numbers map to passage numbers.
  - `write_answer` returns None on garbage or an exception.
- **Combined check:** the prompt numbers the claims and the sentences, and a verdict
  mismatch is treated as "did not run", which falls back.
- `synthesize`:
  - the writer is skipped for refusals and single-claim answers;
  - `PROSE_ANSWERS=False` is byte-identical to today, on the prompt and on the calls;
  - the leak test covers `WRITER_PROMPT`.
- **Agent:** the `compose` step events. **MCP:** `prose` and `answer_style` in the result.
- **Frontend (Vitest):**
  - the prose view renders its chips, and a chip opens the right passage;
  - hovering a sentence sets the hover chunks;
  - Evidence is collapsed by default;
  - fallback and old history items render the claim view.

**Evaluation.** A paired run (`run_pair.py`) of `prose_answers` on vs off, on the 56
dev and 16 held-out questions. Graders are blind and judge the `answer` text each arm
shows.
- **Key facts:** a fact counts only if the answer text states it.
- **False claims:** judged sentence by sentence.
- **Duplicates.**
- **A new readability preference per pair:** A, B or tie.

**Adoption bar (pre-registered; it does not move after the run):**
1. False claims no higher than control, on both dev and held-out.
2. Key facts no worse than −4 on dev, and no loss on held-out.
3. Readability: prose preferred on dev with a two-sided sign test p < 0.05 over the
   non-tie pairs.
4. The fallback rate is reported. Above 25% means the writer needs work before adoption.

**Cost:** the run takes about 1,000 Gemini requests, a full day's allowance, so it runs
after a reset.

## 7. Risks

- **The writer invents links between true facts.** The sentence rule in §3.4 treats
  connectives as content, and two failing sentences mean falling back.
- **Coverage drops because the writer omits facts.** The claims stay visible under
  Evidence, and the eval's key-fact bar catches it.
- **Budget.** One more request per question: about 7 instead of 6.
- **Merge overlap with `ui-overview`.** This work is built on `ui-overview`, which
  already contains main. Merge `ui-overview` into main first, or merge both together.
