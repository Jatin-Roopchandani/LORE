# Interpretive answers: argued from evidence, in LORE's own voice

**Status:** draft, 2026-10-30. Decisions taken in chat; awaiting the user's review.

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
