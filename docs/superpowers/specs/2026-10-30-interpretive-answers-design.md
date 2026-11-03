# Deep and interpretive answers: detailed, argued from evidence, in LORE's own voice

**Status:** draft, 2026-10-30. Decisions taken in chat; awaiting the user's review. §§1–8 cover interpretation; §§9–13 (added the same day) cover depth: an outline planner, sectioned answers and a codex built at ingest.

**Why depth matters.** Shallow answers are available from any general chatbot. People come to LORE to understand what they are reading in detail. A broad question ("tell me about the world of <series>") should get an encyclopedic, sourced chapter that covers every major aspect the books establish: its cosmology, systems of power, peoples, places, institutions and history. It should not get a paragraph.

**Goal.** Many questions are not answered by any sentence in a book:
- evaluation ("Was X a villain or a victim?");
- judgement ("Who is the most complex character?");
- significance ("Why does the treasure matter?");
- theme ("What does the story say about justice?");
- comparison ("How are A and B alike?");
- character analysis ("What kind of person is X?");
- counterfactuals ("What if X had kept it?");
- questions beyond the books ("What did the author intend?").

LORE should answer these well. It gathers evidence from every side, takes a position where the evidence supports one, gives the strongest counter-case, and says what the books cannot settle. Facts stay checked facts, and interpretation is always labelled as interpretation.

**Done when:**
- interpretive questions of every type above get an argued answer (evidence, a reading, a counter-case, limits) instead of a stack of surface facts or a fallback;
- LORE's viewpoint is a written editorial policy that both the writer and the check follow;
- READINGS and JUDGEMENTS are replaced by this one general mechanism;
- the blind interpretive eval set meets the bar in §7.

**Hard requirement:** everything stays general. There are no per-question-type detectors and no regexes over question wording, and nothing refers to the current library. The banned-word and leak tests cover every new prompt.

---

## 1. Decisions (taken in chat)

1. **Classify by how an answer is reached, not by topic.** Each part of a question is one of:
   - `factual`: the books state it;
   - `interpretive`: it needs judgement from evidence;
   - `beyond`: the books cannot settle it (author intent, real-world facts, counterfactuals beyond what the text implies).

   The model decides this inside the existing decomposition call, at no extra request.
2. **Lenses.** For an interpretive part, the decomposer also names up to 4 **lenses**: the aspects of the story that would decide the question, each written as a self-contained evidence question. For example, "Was X a villain or a victim?" might give the lenses "What harm did X do to others?", "What was done to X?", "How does X explain his reasons?" and "How is X treated at the end?". Each lens becomes a retrieval-and-claims part, reusing the existing parts machinery, the story timeline and story order.
3. **Viewpoint A, the careful critic.** LORE takes a position when the evidence leans one way, says how strongly, always gives the best case for the other side, and never passes off an inference as something the book says.

## 2. Decomposition (lore/query/decompose.py)

The `SubQuestions` schema gains per-part fields:
- `kind`: `factual` | `interpretive` | `beyond`;
- `lenses`: list[str], only when `kind` is `interpretive`, at most 4.

The prompt adds general rules:
- what each kind means;
- lenses must cover opposing sides of the question;
- each lens is concrete and answerable from the text;
- no lens may presuppose the answer.

Defaults for a reply that is missing or malformed:
- `kind` falls back to `factual`, today's behaviour;
- lenses are capped at 4, with duplicates dropped.

**Budget.** The total number of retrieval parts across a question stays capped, at 6 by default. That is the existing parts cost model, so a question with lenses costs about what a multi-part question costs today.

## 3. Retrieval and claims

- **Lens parts.** Each lens is a normal part with its own seeds, timeline, evidence share and claims step. It is book-scoped and world-scoped as today. The claims step is told that this part gathers evidence for one side of a larger interpretive question, and that it should include what characters do, choose, hide and change, not only how they are described.
- **The interpretive part.** The part itself gets no separate retrieval. Its evidence is the union of its lenses' evidence.
- **Beyond parts.** These get one retrieval for whatever the text *does* say about the question, so that the answer can state the books' limits honestly.

## 4. The answer's shape

For an interpretive part, the writer produces these blocks, each sentence carrying a `kind`:

1. **Evidence** (`fact`): cited sentences grouped by lens, in story order. This is the existing fact machinery, unchanged.
2. **Reading** (`reading`): one to three sentences giving LORE's position. They are worded as interpretation ("The evidence suggests…", "On balance…", "A strong case can be made that…") and cite the facts they rest on, from at least two lenses.
3. **Counter-case** (`counter`): one or two sentences giving the strongest opposing reading, citing its facts. This is required whenever the evidence supports one; if it does not, the answer omits it and says that it is omitting it.
4. **Limits** (`limit`): an optional sentence saying what the books do not settle. It is written by code from the `beyond` parts and the writer's gaps, in the same way the existing "don't answer" line is.

A factual part keeps today's prose.

**When the evidence is too thin for a position,** the reading says so plainly, and the answer still shows the evidence: for example, "The passages found don't settle this; here is what they show…". It never falls back silently.

## 5. LORE's viewpoint: an editorial policy

The policy lives in `lore/query/viewpoint.md`, packaged with the code. `lore.toml` `[lore.answers] viewpoint = "<path>"` can point at a replacement file. The writer prompt includes it verbatim, and the check prompt quotes the parts that apply to it.

Content of policy A, the careful critic, all general:
- Take a position when the evidence leans one way, and say how strongly: "clearly", "on balance", or "the evidence is split".
- Keep what the text says separate from what we read into it. Interpretation is always worded as interpretation.
- Give the strongest case for the other side when one exists.
- Never present an inference as a statement made by the book.
- Judge characters within the story's own world and values first. When a modern reading differs, it may be noted as a separate, labelled observation.
- For questions beyond the books, say what the books can and cannot tell you. Never fill the gap from outside knowledge.
- Prefer what characters do over what is said about them when the two differ.

The banned-word test covers this file too.

## 6. Checking

- **Facts:** unchanged.
- **Reading:** supported when a careful reader would agree that the cited facts, taken together, fairly support this position at the stated strength. It must not add events, names or details, and it must be worded as interpretation. Overstated certainty ("clearly" where the evidence is split) is not supported.
- **Counter-case:** supported when it is a fair reading of its cited facts.
- **Fallback rules:**
  - a failed reading or counter-case is dropped. If the reading is dropped, the answer says the evidence did not support a clear position, rather than going silent;
  - only facts count toward the one-in-five fallback limit, as today;
  - prose made only of interpretation, with no facts, falls back.
- **Code guards:**
  - a reading needs interpretive wording and citations from at least 2 lenses;
  - a counter-case needs at least 1 citation;
  - at most 3 reading sentences and 2 counter sentences.

**Replacing READINGS and JUDGEMENTS.** The `reading` kind is reused. `judgement` folds into `reading`. `lore/query/judge.py`'s regex detection is removed, but its candidate gathering (exclusions, ranking) is kept as a lens helper. When a lens is about a set of people and names none, candidates are supplied ranked by **beat variety**: distinct beat kinds and chapters, so characters with an arc come before characters who are merely present.

**Switch:** `INTERPRETIVE = True`. Off gives today's behaviour, pinned by a golden test. There is a `run_pair` arm `interpretive`.

## 7. Evaluation

**A new blind set, `evals/answer_quality/interpretive.json`:** 16 questions covering every type in the Goal, across the graphed books. It is written before any results are seen and never used for tuning. Each question records:
- the evidence a good answer must draw on, with verbatim quotes as in the arc set;
- the defensible positions;
- the main counter-case.

**Grading, per answer, blind:**
- **Grounded:** every fact is supported (false facts counted as today).
- **Position:** does it take one where the evidence supports one, at a fair strength?
- **Counter-case:** present and fair when one exists.
- **Labelling:** interpretation is never stated as a book fact.
- **Readability preference:** compared against the control arm.

**Adoption bar** (pre-registered):
1. False facts no higher than control on dev, held-out and the interpretive set.
2. Key facts on dev and held-out no worse than −4, with no loss on held-out.
3. On the interpretive set, a position is taken where warranted in at least 12 of 16 answers, and the counter-case is present where expected in at least 10 of 16.
4. Zero answers that present an inference as a book statement.
5. Readers prefer the interpretive arm on the interpretive set (sign test p < 0.05).

## 8. Risks

- **Lenses presuppose an answer.** Mitigations: the rule that lenses must cover opposing sides, plus the "position" and "counter-case" grades.
- **Over-hedging.** Policy A asks for a stated strength, and grading checks for a position where one is warranted.
- **Cost.** Lens parts reuse the existing per-part budget, capped at 6 parts.
- **Viewpoint drift between writer and check.** Both read the same policy file.


---

## 9. Depth: when an answer goes deep (decision C: automatic, plus a toggle)

- **The decomposer decides depth.** It gains `depth: "focused" | "deep"`. A question is `deep` when it asks about a whole world, system, institution, people, character's life, theme across a series, or anything else whose full answer spans many entities or much of the story.
- **The user can override it.** The UI gets a **Deep dive** toggle next to the question box with three states: auto (the default), on and off. The API and MCP take `depth: "auto" | "deep" | "focused"`.
- **A deep question can be interpretive too.** "What does the series say about power?" is both deep and interpretive. Each section then follows the interpretive shape in §4.

## 10. The outline planner, built from the graph

For a deep answer, the planner builds a table of contents from what the world's graph and codex actually hold. It does not use the model's general knowledge.

1. **Candidates.** The most significant entities in the question's scope (its world, books or named entity's neighbourhood). Significance is ranked by story span and beat or codex richness, and the candidates are grouped by kind: Concept, Faction, Place, Object, Character and Event. For a question about one entity, the candidates are that entity's facets: its codex entry's aspects, its relationships and its timeline periods.
2. **One planner call.** Given the question and the candidate list (names, kinds, one-line codex summaries), the model groups the candidates into **sections**, between 3 and `DEEP_MAX_SECTIONS` (default 12). Each section has a heading, an order and the entities and aspects it covers, plus a one-sentence overview of the whole answer's scope. The rules are general: cover every major aspect present, put foundations before details, group related things, and add nothing that is not in the candidates.
3. **Code checks.** Every entity the plan names must be in the candidate list, and any other entity is dropped. Sections with no entities are dropped. The plan is capped at the maximum number of sections.

## 11. Sectioned answers

- **One part per section.** Each section is answered as a part with its own scope: the section's entities as seeds, their timelines, their codex entries and its own evidence share. It has its own claims step, its own check and its own writer, which produces that section's prose with **no word cap**. Length follows the evidence, and each section gets a heading.
- **Assembly.** The answer is a short overview (cited facts only), a table of contents, then the sections in order. Where the question is interpretive, a closing section gives LORE's reading, the counter-case and the limits, as in §4.
- **Streaming.** Sections are written in order and stream to the UI as each one passes its check, so the reader starts on section 1 while later sections are still being written. A section whose prose fails falls back to its claim list without affecting the others.
- **Faithfulness is unchanged.** Every sentence cites checked claims, and every claim quotes a passage.
- **Cost.** With a codex, a section usually needs its claims call and its writer and check. That is about 2 requests per section; codex entries cut retrieval misses rather than calls. Including the planner call, a 10-section deep answer costs about 20–25 requests. Deep answers show their request cost in the UI's "How this was answered" panel and respect the answer reserve.

## 12. The codex: entity profiles built at ingest

**What it is.** A **codex entry** is a quote-verified profile of one significant entity. It covers what the books establish about the entity, organised by aspect (nature, history, abilities or rules, relationships, role in events), and how the entity changes across the story or series. Each aspect is a short list of statements. Every statement carries a verbatim quote that code verifies, exactly as story beats do. An entry stores no interpretation.

**How it is built.** A new resumable pipeline stage, `codex`, runs after `beats`:
- It picks the significant entities: those of kind Concept, Faction, Place, Object or Character above a significance threshold, at most `CODEX_MAX_PER_BOOK` (default 150).
- It makes one Gemini call per entity. The input is that entity's beats and its top passages within the world, in story order.
- It verifies every quote, pins each statement to its chunk and caches the result per entity. The cache key is the prompt hash plus the hash of the passages used.
- The stage stops on quota and continues with the Library panel's Continue button, like beats.
- Entries are rebuilt when a book is added to or moved out of the world, because their evidence changes. The cache makes unchanged entries free.

**Storage.** A migration adds the tables `codex_entries` (entity id, world, aspect, statement, quote, chunk id, position) and `codex_summaries` (a one-line summary per entity, for the planner). Statements also go into a LanceDB `codex` table for vector search. Entity links are refreshed with the existing relink logic.

**How it is used.**
- The planner uses the summaries to build the outline.
- In a deep or interpretive section, the section's entities' codex statements are added to its evidence the way timeline beats are: their chunks join the evidence through a capped slice, and a compact "Codex" block is added to the claims prompt.
- The codex block is only a map. A claim must quote a numbered passage, as with the timeline.

**Cost.** One request per significant entity, once. For example, two short novels have about 40–60 significant entities. A long multi-book series can have a few hundred, spread over days using Continue.

## 13. UI

- **Deep dive toggle.** It sits next to the question box, with three states: auto, on and off. It shows a short note about the request cost when it is on.
- **Sectioned answer.** The answer has a contents list with jump links and a heading per section. Sections appear as they stream in, and a section still being written shows its heading with a "writing…" state.
- **Steps list.** It shows the planner step and one row per section.
- **Library panel.** It shows the codex stage's progress and Continue, like beats.
- **Strings.** All strings go in messages.ts.

## Evaluation additions

A **deep set**, `evals/answer_quality/deep.json`, holds about 10 broad questions across the graphed books, for example "the world", a whole character's life, a whole institution or a theme across both books. For each question it lists, ahead of time, the aspects a complete answer must cover, with verbatim evidence. It is written before any results are seen.

Grading criteria:
- **Coverage:** the share of listed aspects the answer covers.
- **False facts:** sentence by sentence.
- **Organisation and readability:** a preference judgement against control.

Bar, pre-registered:
- false facts no higher than control;
- coverage at least 70% on average and higher than control on at least 8 of 10 questions;
- readers prefer the deep answer (sign test p < 0.05).
