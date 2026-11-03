# Story beats: a whole-story timeline for each character, faction and object

**Status:** draft, 2026-10-28. The decisions were taken in chat; this spec is awaiting the user's review.

**Goal.** Questions about a character's arc, a change, a relationship or a theme should draw on the whole story, not only the ~25 passages that best match the question's wording. A story mostly *shows* change through what characters do: following someone, recording events, repeatedly being impressed. Those moments are scattered across many chapters, and they rarely match the question's wording.

**Done when:**
- every graphed book has quote-verified beats for its significant characters, factions and objects, built as the `beats` stage of the resumable pipeline;
- every question about such an entity gets that entity's beats from across the story, in story order, and the passages behind them become evidence;
- the paired eval, including a new set of arc questions, meets the pre-registered bar in §7.

**Hard requirement.** Everything must generalise to any book a user uploads, including long fantasy series:
- no names or genre words from the current library in prompts or code;
- the banned-word test covers every new prompt.

---

## 1. Decisions (taken in chat)

1. **Quote-anchored beats (option 1).** Every beat carries a verbatim quote that code verifies against the chapter text. A beat that fails verification is dropped. Beats never assert anything their quote does not support.
2. **Used automatically for any question about an entity (option A).** There is no classifier and no user toggle. The extra cost is prompt length only, with zero extra Gemini requests per question.
3. **Scope: characters, factions and key objects (option B).** These are graph entities of kind Character, Faction or Object. Places are excluded.
4. **Built through the resumable pipeline.** Beats are the `beats` stage, saved and cached per chapter. They stop on quota and continue with the Library panel's Continue button.

## 2. What a beat is

| Field | Meaning |
|---|---|
| `id` | A stable hash of (book, chapter, entity names, quote). |
| `book_id`, `chapter_seq` | Where the beat is. |
| `chunk_id` | The passage that contains the quote. Code sets this during verification. |
| `entities` | One or more entity names involved, the first being the beat's subject. Each name is linked to its entity id at load. |
| `kind` | One of `action`, `decision`, `feeling`, `treatment`, `change`. `treatment` means how the subject behaves toward someone; `change` means a change of state, status, allegiance or possession. |
| `text` | One short sentence saying what happens, in neutral words. |
| `quote` | A verbatim span of 4 to 40 words from the chapter. |

A beat states one moment. It is not a summary, and it is not an interpretation. Interpretation belongs to the writer's labelled readings (READINGS).

## 3. Building beats: the `beats` stage

**One Gemini call per chapter.** The input is:
- the chapter text;
- the chapter's significant entities, taken from the graph and ranked by how often MENTIONS links them to that chapter's chunks, up to 12, of kind Character, Faction or Object;
- the book title and the chapter position.

**Prompt.** The prompt asks for up to 25 beats per chapter, using general rules only:
- what each listed entity does, decides, feels, how it treats others, and how its situation changes;
- prefer moments that show attitude or change, including those shown through action rather than stated;
- every beat quotes the chapter verbatim;
- no beat about unlisted entities, except as a second entity.

**Verification by code:**
- The quote must be found in the chapter text, using the claims' existing quote matcher with the same normalisation.
- The beat is pinned to the chunk that contains the quote.
- Entity names must match a listed entity or an alias of one; anything else is dropped.
- Beats are de-duplicated by (subject, quote).

**Long chapters.** A chapter longer than 60,000 characters is split at chunk boundaries into parts, with one call per part. Items are counted per part.

**Saved progress.** Results are cached per chapter part in `data/beats/<book>/<chapter>-<part>.json`. Each file carries the prompt hash, so a changed prompt invalidates only its own files.

**Resumable-pipeline contract:**
- The stage's items are the chapter parts.
- It calls `job_ctl.advance(already_done)` first.
- It calls `spend_check()` before each request.
- If every model is out of quota, it raises `QuotaStop`.

**Entity links.** Beats store the entity *names*. At the end of the stage, and after every graph reload, the names are mapped to the current entity ids (through aliases too), so beats follow resolver changes such as merges.

## 4. Storage

A migration adds two tables:
- `beats(id PK, book_id, chapter_seq, part, chunk_id, kind, text, quote, created_at)`;
- `beat_entities(beat_id, position, name, entity_id NULL)`, indexed on `entity_id` and `name`.

Beat text is embedded with the existing local embedder into a LanceDB table `beats` (id, book_id, vector). Old code ignores both the new tables and the new LanceDB table.

## 5. Using beats when answering (`TIMELINE = True`)

For each part of the question, the **timeline entities** are the part's seed entities, after book-title filtering, of kind Character, Faction or Object, capped at 3 per part.

The **timeline** for those entities is all of their beats across the library in story order, using `position.order_passages` semantics. If there are more than 24, the selection is:
- **spread:** 12 beats spaced evenly across the ordered list, always including the earliest and the latest;
- **relevance:** 12 beats with the highest vector similarity to the part, excluding the spread beats;
- the union, de-duplicated and in story order.

**Evidence:**
- The chunks of the selected beats join the part's candidate passages, de-duplicated against passages already present and kept within the existing evidence budget.
- Timeline passages have their own slice, `TIMELINE_SHARE` = 30% of the budget, so ranking cannot crowd them all out and they cannot crowd out ranked evidence.

**Claims prompt.** A compact "Story timeline" block is added:
- one line per selected beat, e.g. `[passage n] (position label) <subject>: <text>`;
- a rule saying the timeline is a map of the whole story; claims must still quote the numbered passages, and the timeline lines themselves are not evidence.

**Faithfulness is unchanged.** Claims cite passages and still pass the quote check and the faithfulness check. Beats widen what the claims step sees; they never become claims directly.

**When there is no timeline.** If the book has no beats, the beats switch is off, or the question has no timeline entities, the behaviour is identical to today.

## 6. Cost

- **Build:** about one request per chapter, once per book. For the library today: A Study in Scarlet has 18 chapters and The Sign of the Four has 15, so 33 requests. Pride and Prejudice has 66, but it is built only once it is graphed. A long fantasy novel needs about 60–120 requests.
- **Reload:** 0, because results are cached.
- **Questions:** 0 extra requests. Prompts get longer by at most 24 short lines plus their passages, which fit inside the existing budget.

## 7. Testing and evaluation

### Code tests (no Gemini)

These use synthetic books with invented names; none of the library's names appear.
- **Verification:** a non-verbatim quote is dropped; a beat is pinned to the right chunk; an unlisted entity is dropped; duplicates collapse.
- **Stage:**
  - resume after a quota stop re-requests nothing that is already cached;
  - a changed prompt hash re-runs only that file;
  - a long chapter splits into parts.
- **Relink:** after an entity merge, beats point to the merged id.
- **Selection:** spread plus relevance, the earliest and latest are always present, and the output is in story order.
- **Budget:** `TIMELINE_SHARE` is respected.
- **Off switch:** with no beats or `TIMELINE` off, the prompts are byte-identical to the previous behaviour (golden file).
- **Banned words:** the banned-word test and the leak test cover the beat prompt and the timeline rule.

### Evaluation

The paired `run_pair` arm is `timeline=off` against on, run over:
- the 56 dev and 16 held-out questions;
- **a new arc set** of 16 questions, written to the same rules as held-out: questions about change, relationships, arcs and allegiance across a book or across the series. The set is written before any results are seen and is never used for tuning.

**Adoption bar** (pre-registered):
1. False claims no higher than control on every set.
2. Key facts: no loss on dev or held-out of more than 4; a gain on the arc set (sign test p < 0.05 over the non-tied pairs).
3. No fallback-rate increase above 5 points on any set.

## 8. Risks

- **Beats biased toward what is said aloud.** The prompt asks for moments shown through action too. The arc set measures whether that works.
- **Prompt growth.** The timeline slice is capped, and the budget is unchanged.
- **Pronoun-heavy chapters** where the subject is never named in the quote. The entity list and the chapter context resolve the subject, and the quote only needs to be verbatim; it does not need to contain the name.
- **Flashbacks.** Story order is reading order (the known limit of the story-order work).
- **Cost of very long series.** One-off, resumable, shown in the Library panel.
