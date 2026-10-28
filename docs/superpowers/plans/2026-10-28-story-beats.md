# Story beats: implementation plan

**Spec:** `docs/superpowers/specs/2026-10-28-story-beats-design.md`. Read it first.

The resumable-pipeline API this builds on lives in:
- `lore/orchestrator/jobs.py`
- `lore/orchestrator/pipeline.py`
- `lore/orchestrator/stages.py`

The reports are in `.superpowers/sdd/2026-10-14-prose-answers/resume-*-report.md`.

**Constraints for every task:**
- Work in `/home/jatin/LORE-prose` on branch `prose-answers`, using `.venv/bin/python`.
- **Generality.** The code must work for any book. No library names or genre words go in prompts or code. Test fixtures use invented books and names. Extend `test_prompt_rules_name_no_character_book_or_genre` and the leak test to every new prompt text.
- **TDD.** Write failing tests first. Make no real Gemini or network calls; `tests/conftest.py` blocks them.
- **Commits.** Use git email jatinroopchandani3007@gmail.com. Commit messages carry NO trailers. Stage files by explicit path. Do not push.
- **Processes.** Never use `pkill -f` or `pgrep -f`. Do not stop `lore ui` on :8766. Do not touch `~/LORE-r6`.
- **Style.** Match the surrounding code style.

## Task 1: storage (foundation)

- Add a migration for `beats` and `beat_entities` (spec §4).
- Add `lore/beats/store.py` with these functions:
  - `save_beats(conn, book_id, chapter, part, beats)`, which is idempotent per (book, chapter, part);
  - `beats_for_entities(conn, entity_ids) -> list[Beat]`;
  - `relink(conn, kuzu)`, which maps names to the current entity ids, aliases included;
  - `count(conn, book_id)`.
- Add a `Beat` dataclass matching spec §2.
- Add a LanceDB `beats` table helper with `upsert` and `search(vector, entity_ids, k)`, following `lore/db/lance_store.py`.
- Tests:
  - save and load round-trip;
  - idempotent re-save;
  - relink after a simulated merge (a fake kuzu, or a tiny real one in tmp, following the existing test patterns);
  - Lance upsert and search.

## Task 2: building beats, the `beats` stage (needs Task 1)

- Add `lore/beats/build.py`, containing:
  - `BEATS_PROMPT`, general rules only (spec §3);
  - the pydantic schema;
  - `chapter_entities(kuzu, book_id, chapter, limit=12)`, which returns Character, Faction and Object entities ranked by MENTIONS in that chapter's chunks;
  - `chapter_parts(chunks, max_chars=60_000)`;
  - `verify_beats(raw, chapter_chunks, entities)`. It reuses the claims quote matcher from `synthesize.verify_claims` and its normaliser, pins each beat to a chunk, drops unknown entities, and dedups.
- Cache each chapter part at `data/beats/<book>/<chapter>-<part>.json`, keyed by a prompt hash.
- Replace the no-op `BeatsStage` in `lore/orchestrator/stages.py` with the real one:
  - items are chapter parts;
  - call `advance(already_done)` first;
  - call `spend_check()` before each request;
  - when every model is exhausted, raise `QuotaStop`;
  - use the existing Gemini cascade or client factory pattern (see `DisambiguateStage`);
  - write rows plus the Lance table, then `relink`.
- Run `relink` after every graph load as well, in `LoadStage` or the load path.
- Add config `[lore.beats] enabled = true`. When it is false, the stage is a no-op.
- Tests use a fake generate:
  - verification;
  - cache hit after a quota stop, then resume (nothing re-requested);
  - only the changed-hash file reruns;
  - long-chapter split;
  - the stage contract.

## Task 3: the query timeline (needs Task 1; parallel with Task 2)

- Add `TIMELINE = True` in a new `lore/query/timeline.py`, plus `TIMELINE_SHARE = 0.30`.
- Add `timeline_entities(seeds, kinds)`, capped at 3 per part.
- Add `select_beats(beats, part_vector, order) -> list[Beat]`: spread 12 plus relevance 12, always including the earliest and the latest, in story order (`position.order_passages` semantics).
- Wire it into `lore/query/agent.py` for each part:
  - the chunks of the selected beats join the candidates, de-duplicated;
  - the evidence assembly reserves a `TIMELINE_SHARE` slice of the budget for them;
  - pass the beats down to `synthesize._build_prompt`, which adds the "Story timeline" block plus its rule (spec §5).
- When `TIMELINE` is off, or there are no beats or no timeline entities, the claims prompt stays byte-identical. Pin this with a golden test against HEAD's prompt.
- Add the `run_pair` arm `timeline`.
- Tests:
  - selection;
  - budget slice;
  - the block appears only with beats;
  - golden-off;
  - banned-word and leak coverage.

## Task 4: the arc question set (independent; can run any time)

- Write `evals/answer_quality/arc.json` with 16 questions, in the same format as `heldout.json`.
- The questions cover change, relationships, arcs, loyalty and allegiance across a book or across the two graphed books.
- Every expected fact must be verified against the book text in `lore.db` chunks: record a `verified` entry with chapter references, as heldout does. Verify locally with grep and read; use no Gemini.
- Make no tuning use of it: write it before any timeline results exist. Add it to `run_eval.SETS` / `run_pair` `--set arc`.
- Do not add any of its text to prompts. The leak test must include `arc.json`.

## Review and finish

- Controller checks on each task.
- One Sonnet whole-feature review.
- Full suite.
- Then build the beats for the two graphed books from the Library panel (Continue), or with `lore process <book> --skip-extract`: about 33 requests.
