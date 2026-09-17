# Phase 3 close — Hybrid Query Agent

**Closed 2026-09-17.** `lore ask` answers questions over the loaded books with quote-checked
citations, through graph traversal plus relevance-ranked evidence, with a vector-only
`--baseline` for comparison. Two question sets were run through the real CLI: the frozen
acceptance set (spec §8) and a held-out set written from the book text alone and committed
before any run (`docs/superpowers/specs/2026-09-17-phase-3-heldout-questions.md`, `aacffe3`).
Every answer was judged against the verified answer from the novels, and every non-obvious
citation was checked against the chunk text.

## What changed from the plan, and why

Measured, not assumed, and each approved by the user:

- **Gemini for every LLM step** (extraction, merge adjudication, template selection, synthesis).
  The local 3B was measured nondeterministic at temperature 0 and misread 8 of 11 quoted claims;
  the Kaggle 7B extracted relationships at 56–67% precision with 56 wrong entity kinds, versus
  Gemini Flash Lite's 78–84% and 1. User direction: *"We just want good quality."* Embeddings
  stay local (nomic-embed-text on Ollama, CPU-only — the GPU path reboots WSL).
- **Numbered-choice selection** (the model never copies an entity id) and **quote-checked claims**
  (code drops any claim whose quote is not in its passage).
- **Template correctness:** 5 of 7 Cypher templates returned rows that were not real edges
  (wrong subject, reversed direction). Fixed with a real-Kuzu test that every row is a true edge.
- **Relevance-ranked evidence:** graph evidence was ordered by chapter and truncated, so the
  answering passages fell past the cap. Now a 250-fact candidate pool is ranked by similarity to
  the question with book-fair selection before the 12k-char budget.
- **Graph rebuilt with Gemini** (2026-09-16): entities 356→560, relationships 553→959, cross-book
  entities 8→25, dead-lettered chunks 40→1, the known false `KILLS` edges gone.

## Results

### Frozen acceptance set (spec §8)

| | correct | partly | refused | wrong |
|---|---|---|---|---|
| Agent, first run | 2 | 4 | 2 | 0 |
| **Agent, after relevance ranking** | **5** | **3** | **0** | **0** |
| Baseline (vector only) | 3 | 3 | 2 | 0 |

Agent succeeds where the baseline fails: **Q1** (both novels vs one), **Q4** (correct, including
Morstan's death passage, vs refused), **Q5** (half the answer vs refused) — 2 clear + 1 partial.

### Held-out set (written blind, frozen before running)

| | correct | partly | off-target | refused | wrong |
|---|---|---|---|---|---|
| Agent | 2 (H3, H7) | 2 (H1, H4) | 1 (H2) | 3 (H5, H6, H8) | 0 |
| Baseline | 1 (H7) | 3 (H2, H3, H8) | 0 | 4 (H1, H4, H5, H6) | 0 |

Agent better: H1, H3, H4 (the three cross-book or multi-hop questions where the baseline had one
book or nothing). Baseline better: H2, H8. Tied: H5, H6 (both refused), H7 (both correct).

## Exit criteria (spec §9)

1. Both books ingested, extracted, loaded — **met** (1 of 407 chunks fails schema validation
   deterministically: `05ab674df048d13f:8:2`).
2. Cross-book merge through the gate — **met** (25 cross-book entities).
3. Every §8 question answered with resolving citations — **met** after relevance ranking.
4. Baseline fails on ≥3 where the agent succeeds — **2 clear + 1 partial** on §8; on the held-out
   set the agent was better on 3 and worse on 2. Recorded as borderline, not rounded up.
5. No answer cites evidence absent from the subgraph — **met**, with one misattribution (Q1 cites
   a passage naming Athelney Jones for a claim that he is "of Scotland Yard"; true, but not stated
   in that passage).
6. Every §7 error state correct — **met** (nonsense → `empty`; blank → exit 2; Ollama down →
   clear message, exit 1).
7. Suite green, personal account, pushed — **met** (858 passed, 1 deselected).

**SPEC "done when"** — *a genuine multi-hop question over 2+ books gets a correct, cited answer
that a pure vector baseline demonstrably fails*: met by §8 Q1 and held-out H3.

## The honest read

- **Precision is high.** Across 32 answers from both sets, neither the agent nor the baseline
  stated a false claim; every checked citation holds.
- **The graph earns its place on cross-book and multi-hop questions** (Q1, Q4, H1, H3, H4), which
  is the phase's premise.
- **Recall is the weakness.** On unseen questions the agent fully answered 2 of 8 and refused 3.
  It is roughly on par with vector search overall, not dominant.

## Carried forward

- **Recall:** refusals on H5, H6, H8 and the missing *Sign of the Four* half of Q5. Evidence is
  ranked in a flat 0.68–0.74 similarity band, so passages that mention a subject compete with
  passages that answer the question. H8's selection picked a search that returned one passage
  from the wrong book.
- **Graph quality (Phase 7 eval set):** `Narrator` exists as an entity with 64 edges; Watson is
  split into `Doctor Watson` and `John H. Watson, M.D.`; description-shaped entities remain;
  1 chunk dead-lettered.
- **Citation exactness:** the quote check verifies the quote, not that the claim says no more
  than the quote (the Q1 misattribution).
- **Operations:** Gemini free-tier quota is tight on rebuild days (a synthesis cascaded to
  3.5-flash-lite); Ollama must run CPU-only; background runs must be detached and liveness-checked
  (sessions and WSL restarts killed several).
