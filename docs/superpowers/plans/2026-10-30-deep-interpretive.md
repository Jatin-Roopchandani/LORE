# Deep and interpretive answers: implementation plan

**Spec:** `docs/superpowers/specs/2026-10-30-interpretive-answers-design.md` (§§1–13). Read it first.

**Code to build on:**
- **Query:** `lore/query/` — `decompose.py`, `agent.py`, `synthesize.py`, `write.py`, `timeline.py`, `judge.py`, `scope.py`, `position.py`.
- **Ingest stages:** `lore/beats/` and `lore/orchestrator/stages.py`.
- **UI:** `web/src`.

**Constraints for every task:**
- **Where:** work in `/home/jatin/LORE-prose` (branch `prose-answers`) using `.venv/bin/python`.
- **Generality:** nothing specific to the current library, in code, prompts or tests. Tests use invented books and names. The banned-word test and the leak test must cover every new prompt text and `viewpoint.md`.
- **Tests:** TDD. Make no Gemini or network calls; `tests/conftest.py` blocks them. Switches that are off must be byte-identical to HEAD, pinned by golden files.
- **Commits:** git email jatinroopchandani3007@gmail.com. NO trailers. Stage files by path; other agents share this worktree. Do not push.
- **Processes:** never use `pkill -f` or `pgrep -f`. Do not stop `lore ui` on :8766. Do not touch `~/LORE-r6`. Do not write to `/home/jatin/LORE/data`.
- **Style:** match the surrounding code.

## Task 1: decomposition, depth and viewpoint (foundation)

- **SubQuestions schema:** each part gets `kind` (factual|interpretive|beyond), `lenses` (up to 4, interpretive parts only) and question-level `depth` (focused|deep). Malformed values fall back to today's behaviour.
- **Prompt:** add the general rules from spec §2 and §9.
- **Part budget:** cap total parts at 6. This applies to lens expansion; deep sections are budgeted separately, in Task 4.
- **`depth` parameter:** add `depth: auto|deep|focused` to `answer_question`, MCP `lore_search` and the web ask endpoint. Auto uses the decomposer's value.
- **Viewpoint:** write `lore/query/viewpoint.md` (policy A, spec §5) and add the config `[lore.answers] viewpoint = <path>` with a loader.
- **Switches:** `INTERPRETIVE = True` and `DEEP = True`. When they are off, the decomposition prompt and schema are byte-identical (golden).
- **Tests:** schema parsing and fallbacks, lens capping, depth override, viewpoint loading and override, golden-off.

## Task 2: codex at ingest (independent; parallel with 1)

- **Package:** `lore/codex/`, mirroring `lore/beats/`:
  - the prompt (general rules only);
  - the schema;
  - significant-entity selection (kinds Concept, Faction, Place, Object and Character, a threshold, and the `CODEX_MAX_PER_BOOK` cap);
  - an evidence pack per entity (its beats plus its top passages within its world, in story order);
  - quote verification that reuses the claims and beats matcher;
  - a per-entity cache keyed by the prompt hash plus the evidence hash.
- **Storage:** a migration adding `codex_entries` and `codex_summaries`, a LanceDB `codex` table, a store API, and relinking (reuse the beats relink pattern).
- **Stage:** a `CodexStage` in `stages.py`, placed after `beats` in `STAGES`, following the stage contract: call `advance(already_done)` first, call `spend_check()` before each request, raise `QuotaStop`, and relink at the end.
- **Rebuilds:** the remove and move flows (`lore/graph/remove.py`, the move stages) drop and rebuild codex entries for the affected world.
- **Config:** `[lore.codex] enabled = true`, plus `CODEX_MAX_PER_BOOK`.
- **Tests:** selection, verification, cache-resume with no repeated requests, the stage contract, the move round-trip including the codex, and relinking.

## Task 3: interpretive answering (needs 1)

- **Lens parts:** retrieval and claims per lens, as in spec §3. An interpretive part's evidence is the union of its lens parts' evidence. Beyond parts get one retrieval.
- **Writer:** the answer shape from spec §4, with sentence kinds `fact`, `reading` and `counter`. The `limit` line is rendered by code. The viewpoint text goes into the writer prompt.
- **Check:** the reading and counter-case tests from spec §6, quoting the viewpoint. Guards and fallbacks as in §6: a dropped reading becomes the "evidence did not support a clear position" line.
- **Replace READINGS and JUDGEMENTS:** fold `judgement` into `reading`. Remove the regex detection in `judge.py`, but keep candidate gathering as a lens helper, ranked by beat variety.
- **Switch:** `run_pair` arm `interpretive`.
- **Tests:** synthetic interpretive questions with fake generate. Cover the shape, the guards, the fallbacks, the replaced paths and golden-off.

## Task 4: deep answering (needs 1 and 2; after 3)

- **Planner:** spec §10. Gather candidates from the graph and codex, make one planner call, then validate: unknown entities dropped, empty sections dropped, sections capped at `DEEP_MAX_SECTIONS`.
- **Sections:** one part per section, as in spec §11, using the section's entities as seeds, plus the timeline and the codex slice. Each section has its own claims, check and writer, with no word cap and a heading. Then assemble the overview, the contents list and the sections, plus the interpretive close when relevant.
- **Streaming:** each section is emitted when it is ready, via new agent events (`section` start and done, with its prose). A failed section falls back to claims on its own. Respect the answer reserve.
- **Codex in focused parts:** the claims prompt also gets the codex block when entities have entries (spec §12).
- **Switch:** `run_pair` arm `deep`.
- **Tests:** planner validation, section isolation, streaming events, the per-section fallback, the codex block, the budget, and golden-off.

## Task 5: UI (needs 3 and 4)

- **Deep dive toggle:** auto, on and off, persisted, with a cost note when it is on.
- **Sectioned answer:** a contents list with jump links, sections rendered as they stream in, and a "writing…" state for unfinished sections.
- **Sentence kinds:** distinct, labelled styles for the reading and the counter-case, and a limits line.
- **Steps list and Library panel:** planner and section rows in the steps list; the codex stage in the Library panel.
- **Strings and tests:** strings go in `messages.ts`. Vitest covers everything above.

## Task 6: blind eval sets (independent; can run any time)

- **`evals/answer_quality/interpretive.json`:** 16 questions covering every type in spec §7. Each records the required evidence with verbatim quotes (verified as exact substrings of `lore.db` chunks), the defensible positions and the main counter-case.
- **`evals/answer_quality/deep.json`:** about 10 broad questions. Each lists the aspects a complete answer must cover, with verbatim evidence.
- Add both to `run_eval.SETS` and to the leak test.
- Write them before any results exist. Never use them for tuning.

## Finish

- One Sonnet review of the whole feature, then the full suite and the web build.
- Build the codex for the two graphed books from the Library panel. That is about 40–60 requests, run on a day when quota is free.
