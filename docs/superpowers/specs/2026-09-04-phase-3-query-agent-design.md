# Phase 3 — Hybrid Query Agent

**Status:** approved in chat 2026-09-04. Implements SPEC.md §9 Phase 3.

**Goal:** `lore ask "..."` answers a multi-hop, cross-book question with a cited
answer derived from the knowledge graph, and demonstrably beats a pure
vector-search baseline on questions it was not tuned against.

---

## 1. What exists already

Phase 2B closed with a working loader and these stores:

**Kuzu** (`lore/db/kuzu_store.py`)
```
Book(id, title, author)
Chapter(id, book_id, seq, title)
Chunk(id, book_id, chapter_seq)              -- NOTE: no text
Entity(id, name, kind, aliases[], books[])
HAS_CHAPTER(Book -> Chapter)   HAS_CHUNK(Chapter -> Chunk)
MENTIONS(Chunk -> Entity)      ALIAS_OF(Entity -> Entity)
REL(Entity -> Entity, predicate, predicate_raw, evidence,
    confidence, chunk_id, book_id, chapter_seq, run_id)
```

**LanceDB** (`lore/db/lance_store.py`) — `chunks` (vector, text, `entity_ids[]`)
and `entities` (centroid vector, name, kind). `search_chunks(vector, limit)`
is the vector entry point.

**SQLite** — `chunks(id, book_id, chapter_seq, scene_seq, seq, text)` is the
canonical home of chunk text. Kuzu's `Chunk` node deliberately carries no text,
so evidence assembly reads text from SQLite by `chunk_id`.

**Embedding** — `nomic-embed-text`, 768-dim. `lore/extract/embed.py` defines
`DOCUMENT_PREFIX = "search_document: "` (applied at index time) and
`QUERY_PREFIX = "search_query: "` (applied here, at query time). Using the
wrong prefix silently degrades retrieval rather than erroring, so the query
path must apply `QUERY_PREFIX` and a test must pin it.

**Inference** — `OllamaProvider.generate(prompt, schema)` returns a
schema-validated Pydantic model; `embed(texts, model)`; `healthy()`; `aclose()`.
`generate` is the only text path and it is schema-constrained.

## 2. Inherited constraints from Phase 2B

Three findings from Phase 2B bind this phase. They are requirements, not advice.

**2.1 Never match entities by name substring.** `REL` edges attach only to
canonical entity nodes, and `display_name` picks the longest raw surface form.
A character introduced as "Captain Vane" but canonicalised to "Governor Hale"
is unreachable by `WHERE e.name CONTAINS 'Vane'` — this is exactly why Phase
2B's own multi-hop demo returned zero rows. Entity lookup MUST match
`Entity.aliases` or traverse `ALIAS_OF`, and a regression test must cover an
entity whose display name differs from the queried surface form.

**2.2 Every Cypher slot is a bound parameter.** Phase 2B deferred a finding
that `loader._entities_where` interpolates a predicate via f-string. No query
in this phase may interpolate a value into Cypher; all slots bind as `$name`.
The deferred loader finding is fixed in this phase.

**2.3 A re-load is not LLM-free and neither is a query.** Resolver-internal
adjudication runs on every load; likewise every `ask` makes at least two LLM
calls (template selection, synthesis). Nothing in the CLI or docs may describe
querying as free or instant.

## 3. Staging — data before code

Phase 3 carries a data dependency in its middle. The order is fixed:

| Stage | Deliverable |
|---|---|
| **0. Data** | Two Sherlock Holmes books ingested, extracted on Kaggle, loaded |
| **1. Freeze** | The acceptance question set, appended to this spec |
| **2. Build** | The query agent, TDD against fixture graphs |
| **3. Accept** | The frozen set run agent-vs-baseline |

**Corpus:** *A Study in Scarlet* and *The Sign of the Four*. Holmes, Watson and
Lestrade appear in both, so this is the first real-data exercise of the
cross-book adjudication gate Phase 2B built and proved only by unit test —
where the same character across two books *should* merge. Both are short
novels, so the run is roughly half the cost of Pride and Prejudice alone.

**Pride and Prejudice is not used.** Its existing artifacts are 5 units of ~74
and predate the current schema: they carry `entity_names` but no
`entity_kinds`, and `loader._named_kinds` defaults a missing kind to
`"Concept"`. Loading them would silently produce a graph in which every entity
is a Concept, with no error. Re-extracting it is a larger run for a corpus that
barely exercises cross-book merging.

**The question set is frozen, not discovered.** Stage 1 inspects the real
loaded graph and writes 5–8 questions into §8 of this spec, each with a
human-verified answer from the text and the vector-only baseline result. Once
appended, **no later task may modify a question**. This is the safeguard
against Phase 2B's ending, where an optimistically pre-written demo query
returned nothing; choosing questions from the real graph makes them
answerable, and freezing them before implementation stops them being retuned
to whatever the agent happens to do well. A question the agent cannot answer
is a finding, not a reason to edit §8.

## 4. Architecture

```
lore ask "How does Watson's role differ between the two books?"
   │
   ├─1 SEED     embed(QUERY_PREFIX + question) -> LanceDB search_chunks(k)
   │            -> union of entity_ids on those chunks
   │            -> canonicalise (hop ALIAS_OF) -> seed entities
   │
   ├─2 SELECT   LLM picks ONE template + fills slots  [schema-constrained]
   │
   ├─3 TRAVERSE run the parameterized template in Kuzu (read-only, LIMITed)
   │            -> bounded subgraph of REL rows
   │
   ├─4 ASSEMBLE per row: evidence string + chunk text (SQLite, by chunk_id)
   │            dedupe, order by (book_id, chapter_seq), cap at 12000 chars
   │
   └─5 SYNTH    Ollama -> answer with inline numbered citations, or refusal
```

`--baseline` runs **stages 1 and 5 only** — retrieved chunks straight to
synthesis, same model, same prompt, no graph. It is a real flag rather than an
acceptance-only script so the comparison can be re-run after any prompt or
model change.

### Why the LLM never writes Cypher

The one LLM decision before synthesis is *which template, with which slots* —
a constrained classification that `generate(prompt, schema)` already enforces
structurally. Phase 2B's evidence is that the local 3B model produced three
self-loops and two false entity merges on a six-sentence passage; asking it to
emit correct Cypher would add a syntax-validation layer, a read-only
enforcement layer, and a retry loop, to buy expressiveness this phase does not
need. Templates are hand-written constants, so read-only holds by construction.

The seam is left clean — `select_template` returns a `TemplateCall`, and
`run_template` consumes one — so a generator could be added behind that
interface later. Nothing is built for it now.

## 5. Modules

All new code lives in `lore/query/`. Each file has one job and is testable
alone.

| File | Responsibility |
|---|---|
| `seed.py` | `seed_entities(question, lance, kuzu, embed, *, k) -> list[SeedEntity]` — embed, vector search, entity bridge, canonicalisation |
| `templates.py` | `TEMPLATES: dict[str, Template]` — the Cypher library; `Template(name, cypher, slots, description)` |
| `select.py` | `select_template(question, seeds, provider) -> TemplateCall` — the one pre-synthesis LLM call |
| `subgraph.py` | `run_template(kuzu, call) -> list[EdgeRow]`; `assemble(conn, rows, *, max_chars) -> Evidence` |
| `synthesize.py` | `synthesize(question, evidence, provider) -> Answer` — cited answer or refusal |
| `agent.py` | `answer(question, *, baseline=False) -> Answer` — orchestration only |

`lore/cli.py` gains `ask`. `lore/ingest/registry.py` gains
`chunks_by_id(conn, ids) -> dict[str, str]` for evidence assembly.

`books.status` finally advances past `chunked` — `lore process` sets it to
`graphed` on a successful load, and `ask` reports which books are queryable.
This was deferred from Phase 2B with Phase 3 named as the consumer.

## 6. The template library

Six templates. Every one returns the identical row shape, so assembly never
branches on which template ran:

```
(subject, predicate, predicate_raw, object, evidence,
 confidence, book_id, chapter_seq, chunk_id)
```

| Template | Slots | Question it answers |
|---|---|---|
| `neighbours` | `entity_id`, `depth` (1–2) | "Who does Holmes deal with?" |
| `path_between` | `a_id`, `b_id`, `max_hops` (≤3) | "How are Watson and Lestrade connected?" |
| `by_predicate` | `entity_id`, `predicate` | "Who opposes Holmes?" |
| `shared_neighbours` | `a_id`, `b_id` | "What do these two have in common?" |
| `across_books` | `entity_id` | "How does Watson's role differ between the books?" |
| `chapter_window` | `entity_id`, `lo`, `hi` | "How does X change over the story?" |

`across_books` is the one that makes the phase's premise concrete: it returns a
single entity's edges partitioned by `book_id`, which is only meaningful
because the cross-book merge gate put one node behind both books.

### Guards

- **Read-only by construction.** Templates are module constants. A test asserts
  every template matches a read-only whitelist and contains no `CREATE`,
  `MERGE`, `SET`, `DELETE` or `DROP`.
- **Bound parameters only.** Slot values pass as Cypher parameters, never
  interpolated (§2.2). A test asserts no template string contains an f-string
  brace or `%s`.
- **Bounded results.** `LIMIT` is baked into every template; `depth ≤ 2`,
  `max_hops ≤ 3`; assembled evidence capped at 12000 characters before synthesis
  (roughly 3k tokens, leaving the 3B model's context to the prompt and
  answer); the cap is a module constant, not a magic number at the call site.
- **Slot validation.** `select_template` output is schema-constrained, but the
  model can still name a template that does not exist or an entity id that is
  not among the seeds. Both are rejected before execution and fall back to §7's
  empty-result path.

## 7. Error states — kept distinct

Phase 2B's recurring defect was a success line printed over incomplete data.
These four outcomes are deliberately different and each has a test:

| Condition | Behaviour | Exit |
|---|---|---|
| No seed entities found | Plain "nothing in the library matches" — a legitimate empty answer, not an error | 0 |
| Template ran, returned no rows | Fall back to vector-only synthesis **with an explicit note that the graph contributed nothing** — never presented as graph-derived | 0 |
| Synthesis cannot ground a claim | Refuse that claim rather than fill it in | 0 |
| Store unreachable, model down, invalid book | Clear one-line message, no traceback | non-zero |

The second is the important one: silently degrading to vector search and
presenting the result as a graph answer would make the phase's central claim
unfalsifiable.

## 8. Acceptance question set

> **Frozen 2026-09-09 by Task 2.** Chosen from the loaded graph so every
> question is answerable, then frozen so none can be retuned to the agent's
> behaviour. No later task may add, remove, or reword an entry. A question the
> agent cannot answer is a finding, recorded in the acceptance report.

Book ids: `05ab674df048d13f` = *A Study in Scarlet* (SiS),
`0ee225a9a3b5fbc2` = *The Sign of the Four* (SoF). Chapter numbers below are
the loader's `chapter_seq` (EPUB spine order), with the novel's own chapter in
brackets. Every answer was verified against the EPUB text in `data/inbox/`,
never against the graph.

### Q1. Which Scotland Yard detectives work with Sherlock Holmes on the case in each of the two novels?

- **Answer (verified from the text):** In *A Study in Scarlet* they are Tobias
  Gregson and Lestrade — "the well-known Scotland Yard officials, Messrs.
  Lestrade and Gregson" (ch18 [Pt II ch. VII]), "Mr. Lestrade and Mr. Gregson,
  of Scotland Yard, are both engaged upon the case" (ch10 [Pt I ch. VI]), and
  Holmes's own "Gregson is the smartest of the Scotland Yarders; he and
  Lestrade are the pick of a bad lot" (ch7 [Pt I ch. III]). In *The Sign of the
  Four* it is Athelney Jones — "Mr. Athelney Jones, of Scotland Yard" (ch12
  [ch. IX] and ch14 [ch. XI]). Gregson and Lestrade are named once in SoF, but
  only in a list of officials who are "out of their depths"; neither works that
  case.
- **Supporting graph:** the cross-book `Place` node `Scotland Yard`
  (`books` = both). SiS side: `Mr. Gregson -[ALLEGIANCE_TO]-> Scotland Yard`
  (ch7), `-[LOCATED_IN]->` (ch10); `Mr. Lestrade -[LOCATED_IN]->` (ch10),
  `-[ALLEGIANCE_TO]->` (ch11). SoF side: `Mr. Athelney Jones -[LOCATED_IN]->
  Scotland Yard` (ch12), `-[ALLEGIANCE_TO]->` (ch14). Bridged to
  `Mr. Sherlock Holmes` (cross-book) by `INTERACTS_WITH` to each of the three.
  Known noise on this node that a correct answer must not repeat:
  `Mr. Joseph Stangerson -[ALLEGIANCE_TO]-> Scotland Yard` is false, and
  `John Rance` / `Murcher` are constables, not detectives.
- **Hops:** cross-book (2 hops — Holmes → Scotland Yard → officer, partitioned
  by `book_id`)
- **Vector baseline:** all five chunks come from *A Study in Scarlet*.
  `05ab674df048d13f:7:2` and `:10:2` name Gregson and Lestrade as Scotland Yard
  men, so the SiS half is fully answerable from the chunks alone. No chunk from
  *The Sign of the Four* is retrieved at all, so Athelney Jones is absent.
- **Expected verdict:** graph agent should succeed; baseline should fail — it
  can answer one book of the two and has no way to know the other exists.

### Q2. Why did Jefferson Hope kill Enoch Drebber and Joseph Stangerson?

- **Answer (verified from the text):** Revenge. Drebber and Stangerson were
  both in the Danite party that hunted the fleeing Ferriers; Stangerson "had
  shot her father", John Ferrier, and Lucy Ferrier — who was to have married
  Hope — was forced into marriage with Drebber and "pined away and died within
  a month" (ch16 [Pt II ch. V]). Hope took the wedding ring from her dead
  finger and pursued the two men across America and Europe for twenty years
  (ch16, ch17 [Pt II ch. VI]): "they had been the cause of the deaths of two
  human beings—a father and a daughter—and ... I determined that I should be
  judge, jury, and executioner all rolled into one."
- **Supporting graph:** `Mr. Jefferson Hope -[KILLS]-> Enoch J. Drebber`,
  `-[KILLS]-> Mr. Drebber` (ch16, ch17), `-[KILLS]-> Mr. Joseph Stangerson`
  (ch11, ch16, ch18); motive side `Mr. Jefferson Hope -[LOVES]-> Lucy Ferrier`
  (ch13), `-[LOVES]-> Lucy` (ch13, ch15), `-[LOVES]-> John Ferrier` (ch16);
  `Danite Band -[KILLS]-> John Ferrier` (ch14); `Mr. Drebber -[OPPOSES]->
  Mr. Jefferson Hope`, `Mr. Joseph Stangerson -[OPPOSES]-> Mr. Jefferson Hope`
  (ch16). The chain is `Hope → (Lucy | John Ferrier) → (Drebber | Stangerson)`.
- **Hops:** 2
- **Vector baseline:** strong. `05ab674df048d13f:16:9` carries "Stangerson had
  shot her father", the forced marriage and Lucy's death in one chunk;
  `:16:13` and `:16:14` carry the twenty-year pursuit.
- **Expected verdict:** both should succeed. This question is in the set to
  prove the baseline is not broken.

### Q3. How is Jefferson Hope connected to John Ferrier?

- **Answer (verified from the text):** Hope was courting Lucy Ferrier, John
  Ferrier's adopted daughter, after saving her from a cattle stampede in Salt
  Lake City; he became "a favourite with the old farmer" (ch13 [Pt II ch. II]).
  When the Elders threatened Ferrier over Lucy's marriage, Ferrier sent word
  into the Nevada mountains for Hope (ch15 [Pt II ch. IV]), and Hope returned
  and led the two of them out of Utah. Ferrier was killed during the flight;
  Hope found his grave — "JOHN FERRIER, Formerly of Salt Lake City, Died August
  4th, 1860" — and swore the revenge that drives the rest of the book (ch16
  [Pt II ch. V]).
- **Supporting graph:** `Mr. Jefferson Hope -[LOVES]-> Lucy Ferrier` (ch13) and
  `John Ferrier -[LOVES]-> Lucy Ferrier` (ch12) give the two-hop path through
  the daughter; `Mr. Jefferson Hope -[LOVES]-> John Ferrier` (ch16),
  `-[BECOMES]->` / `-[UNCATEGORIZED]-> John Ferrier` (ch13) and
  `Danite Band -[KILLS]-> John Ferrier` (ch14) close it. Note the split node:
  `John Ferrier` exists twice, as `Character` (degree 20) and as `Faction`
  (degree 5); the `Character` node carries the edges above.
- **Hops:** 2
- **Vector baseline:** strong. `05ab674df048d13f:13:9` alone states that Hope
  called on John Ferrier and became a favourite of the old farmer; `:15:0` has
  Ferrier sending his message to Hope, and `:16:6` has the grave.
- **Expected verdict:** both should succeed. Second of the two questions kept
  as a baseline-health check.

### Q4. What connected Major Sholto to Captain Morstan, and how did Morstan die?

- **Answer (verified from the text):** They were brother officers of the 34th
  Bombay Infantry, "bosom friends", both in charge of the convict-guard on Blair
  Island in the Andamans (ch5 [ch. II], ch15 [ch. XII]), and both took Jonathan
  Small's Agra-treasure secret in exchange for a promise of freedom. Sholto
  sailed home with the whole treasure and cheated Morstan. On the night Morstan
  reached London, 3 December 1878, he went straight to Sholto to claim his
  share; they quarrelled, and Morstan — who "had suffered for years from a weak
  heart" — fell dead, striking his head on the corner of the treasure-chest.
  Sholto and his servant Lal Chowdar hid the body (ch7 [ch. IV], chunks
  `0ee225a9a3b5fbc2:7:8` and `:7:9`).
- **Supporting graph:** `Captain Morstan -[ALLEGIANCE_TO]-> Major Sholto`
  (ch15, evidence "They were bosom friends, those two, and never far apart"),
  three `Captain Morstan <-[UNCATEGORIZED]-> Major Sholto` edges (ch7, chunk
  `0ee225a9a3b5fbc2:7:4`), `Major Sholto -[POSSESSES]-> the Agra treasure`
  (ch14), and `Captain Morstan -[KILLS]-> Lal Chowdar` (ch7, chunk
  `0ee225a9a3b5fbc2:7:9`) — a mis-directed predicate whose `evidence` string is
  nevertheless the disposal of Morstan's body, and whose chunk is Thaddeus
  Sholto's account of the quarrel and the death.
- **Hops:** 2
- **Vector baseline:** partial. `0ee225a9a3b5fbc2:6:1` gives Holmes's deduction
  that Sholto is the only man Morstan could have visited, `:15:29` gives "bosom
  friends", `:15:34` gives the cheated treasure. None of the five chunks says
  how Morstan died — the weak heart, the quarrel and the fall are in `:7:8`
  and `:7:9`, which are not retrieved.
- **Expected verdict:** graph agent should succeed; baseline should fail on the
  second half of the question.

### Q5. In each novel the police arrest the wrong person before Holmes finds the real culprit. Who is wrongly arrested in each?

- **Answer (verified from the text):** In *A Study in Scarlet*, Gregson arrests
  "Arthur Charpentier, sub-lieutenant in Her Majesty's navy" for Drebber's
  murder (ch10 [Pt I ch. VI]), while Lestrade is separately chasing Stangerson,
  "who had no more to do with the crime than the babe unborn"; the real
  murderer is Jefferson Hope. In *The Sign of the Four*, Athelney Jones arrests
  Thaddeus Sholto — with the housekeeper Mrs. Bernstone, the Indian butler Lal
  Rao and the gatekeeper McMurdo — for Bartholomew Sholto's murder (ch10
  [ch. VII], ch11 [ch. VIII]); Thaddeus and the housekeeper are released two
  days later (ch12 [ch. IX]) and the real killer is Tonga.
- **Supporting graph:** SiS side, `Lieutenant Charpentier -[KILLS]-> Mr.
  Drebber` (ch10, chunk `05ab674df048d13f:10:11`, evidence "I suppose you are
  arresting me for being concerned in the death of that scoundrel Drebber") —
  the graph has the wrong predicate but the right chunk. SoF side,
  `Mr. Athelney Jones -[UNCATEGORIZED]-> Mr. Thaddeus Sholto` (ch10, evidence
  "He has arrested not only friend Thaddeus, but the gatekeeper, the
  housekeeper, and the Indian servant"; ch11, evidence "the brother, Thaddeus
  Sholto, has already been arrested, together with the housekeeper, Mrs.
  Bernstone, an Indian butler named Lal Rao, and a porter, or gatekeeper, named
  McMurdo"), plus the parallel edges to `housekeeper, Mrs. Bernstone` and
  `McMurdo`. The real culprits are reachable by `Mr. Jefferson Hope -[KILLS]->
  Mr. Drebber` and `Tonga -[KILLS]-> Mr. Bartholomew Sholto`.
- **Hops:** cross-book (2 hops on each side — victim → arrested man, victim →
  real killer)
- **Vector baseline:** weak. The five chunks are Holmes's pre-murder deduction
  in SoF (`0ee225a9a3b5fbc2:6:0`, `:6:1`) and Gregson's self-congratulation in
  SiS (`05ab674df048d13f:10:2`, `:10:5`, `:7:2`). `:10:5` mentions only that
  Lestrade "is after the secretary Stangerson"; the Charpentier arrest is in
  `:10:4`, which is not retrieved, and no chunk mentions Thaddeus Sholto's
  arrest.
- **Expected verdict:** graph agent should succeed; baseline should fail on
  both halves.

### Q6. Who killed Bartholomew Sholto, and how?

- **Answer (verified from the text):** Tonga, the Andaman Islander, killed him
  with a poisoned thorn blown from a blow-pipe, before Jonathan Small could
  climb into the room. "Tonga thought he had done something very clever in
  killing him" (ch15 [ch. XII]); Small "bore no grudge against Bartholomew
  Sholto ... the savage instincts of his companion had broken out, and the
  poison had done its work" (ch10 [ch. VII]). The thorn is found in the scalp
  in ch8 [ch. V].
- **Supporting graph:** `Tonga -[KILLS]-> Mr. Bartholomew Sholto` (ch15, twice,
  chunk `0ee225a9a3b5fbc2:15:39`), with `Jonathan Small -[OPPOSES]->
  Mr. Bartholomew Sholto` (ch10) as the contrast. The graph carries no
  Tonga–Small edge, so the agent must take "his companion" from the evidence
  and chunk text, not from an edge.
- **Hops:** 1
- **Vector baseline:** strong. `0ee225a9a3b5fbc2:15:39` names Tonga as the
  killer and `:10:17` supplies "the poison had done its work".
- **Expected verdict:** both should succeed.

### Q7. Who owned the steam launch Aurora?

- **Answer (verified from the text):** Mordecai Smith, a Thames boatman working
  from Smith's Wharf, who ran her with his eldest son Jim — "a steam launch
  called the Aurora, owner Mordecai Smith" (ch11 [ch. VIII]); "at that moment
  who should come down but Mordecai Smith, the missing owner?" (ch13 [ch. X]).
  Small hired her for the escape: "I engaged with old Smith" (ch15 [ch. XII]).
- **Supporting graph:** `Mordecai Smith -[POSSESSES]-> steam launch Aurora` and
  `Jim -[POSSESSES]-> steam launch Aurora` (both ch12). The `steam launch
  Aurora` node is kind `Place` rather than `Object` and exists as an
  `ALIAS_OF`-linked pair (degree 21 and degree 0), so the answer requires the
  alias hop that §2.1 mandates; there are also separate `Aurora` (`Object`) and
  `Aurora herself` (`Character`) nodes that carry no ownership edge.
- **Hops:** 1
- **Vector baseline:** strong. `0ee225a9a3b5fbc2:11:10` states the owner
  outright, and `:13:5` calls Mordecai Smith "the missing owner".
- **Expected verdict:** both should succeed.

### Q8. At what address do Holmes and Watson live, and is it the same address in both novels?

- **Answer (verified from the text):** 221B Baker Street, in both. *A Study in
  Scarlet*: "we ... inspected the rooms at No. 221b, Baker Street" (ch6 [Pt I
  ch. II]), and Watson's own advertisement is signed "Dr. Watson, 221B, Baker
  Street" (ch9 [Pt I ch. V]). *The Sign of the Four*: Holmes's newspaper
  advertisement directs replies "at 221b Baker Street" (ch12 [ch. IX]).
- **Supporting graph:** the cross-book `Place` node `Baker Street`
  (`books` = both), with `Mr. Sherlock Holmes -[ALLEGIANCE_TO]-> Baker Street`
  (SiS ch5) and `-[LOCATED_IN]-> Baker Street` (SoF ch4, ch12), plus
  `John H. Watson, M.D. -[LOCATED_IN]-> Baker Street` (SoF ch4). The street
  number lives on the book-local nodes `No. 221b, Baker Street` (SiS ch6,
  `Mr. Sherlock Holmes -[LOCATED_IN]->`) and `221b Baker Street` (SoF ch12,
  `Mr. Sherlock Holmes -[BECOMES]->`), which the resolver did not merge; the
  cross-book claim therefore rests on `Baker Street` plus one number node per
  book.
- **Hops:** cross-book (1 hop per book)
- **Vector baseline:** strong. `05ab674df048d13f:6:0` gives "No. 221b, Baker
  Street" for SiS and `0ee225a9a3b5fbc2:12:7` puts Holmes at "The Baker Street
  address" in SoF, though the number itself in SoF sits in `:12:6`, which is
  not retrieved.
- **Expected verdict:** both should succeed; the baseline's SoF half is
  slightly weaker, so a baseline answer that hedges on "the same address" is a
  legitimate partial.

### Summary

| | Q1 | Q2 | Q3 | Q4 | Q5 | Q6 | Q7 | Q8 |
|---|---|---|---|---|---|---|---|---|
| hops | cross-book | 2 | 2 | 2 | cross-book | 1 | 1 | cross-book |
| expected agent | pass | pass | pass | pass | pass | pass | pass | pass |
| expected baseline | **fail** | pass | pass | **fail** | **fail** | pass | pass | pass |

Six of the eight need more than one hop or more than one book; three are
cross-book. Three are expected baseline failures, which is exactly the
threshold exit criterion 4 sets — if the acceptance run shows fewer, that is a
finding about the agent or the baseline, not a licence to edit this section.

## 9. Exit criteria

1. Both Holmes books ingested, extracted and loaded; `lore graph` counts
   cross-checked against Kuzu and LanceDB directly.
2. At least one entity correctly merged across both books through the
   adjudication gate, with `books` holding both ids — the first real-data
   proof of Phase 2B's cross-book guarantee.
3. `lore ask` answers every question in §8 with citations that resolve to real
   chunk ids in the loaded books.
4. `lore ask --baseline` demonstrably fails on at least three of those
   questions where the graph agent succeeds, with both outputs recorded.
5. No answer cites evidence absent from the assembled subgraph.
6. Every §7 error state reachable and correct, verified by invocation.
7. Full suite green and pristine; every commit on the personal account, pushed.

## 10. Out of scope

MCP tools (Phase 4), the web UI (Phase 5), LLM-generated Cypher (§4), book
discovery (Phase 6), and the extraction-quality eval set (Phase 7). The
deferred Phase 2B items not named in §2 — the Ollama provider's error-response
handling, its hardcoded 120 s timeout, and empty dead-letter error strings —
stay deferred unless they block Stage 0.
