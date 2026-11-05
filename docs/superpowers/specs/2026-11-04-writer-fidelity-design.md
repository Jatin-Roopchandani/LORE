# Writer fidelity: keep every fact, keep readings labelled

**Status:** agreed in chat on 2026-11-04, after round 7.

## Why

Rounds 6 and 7 were blind and graded against pre-registered bars. Both show the same thing about the prose writer (`lore/query/write.py`) on ordinary factual questions:

- **It drops verified facts.** In round 7, dev key facts were 131 with the defaults on against 140 with them off, and held-out was 52 against 58. One question fell from 6 facts to 1 (D3).
- **Interpretation leaks into factual sentences.** 7 of the 9 false claims on the defaults side were readings stated as fact, for example "Holmes withheld assistance", "RACHE signified self-appointed justice" and "Small came only to confront, not to kill".
- **No readability gain.** Graders preferred neither arm, in either round.

As a result, `PROSE_FACTUAL` is now off: plain factual answers show their checked claims. Interpretive, deep, lens and recap answers still go through the writer. This spec fixes the writer so prose can earn its way back on for every answer.

## What changes (general, for any book)

### 1. Coverage is enforced by code, not asked for

- After the writer returns and `verify_prose` runs, code computes which kept claims no shown sentence cites.
- **One repair round:** the writer gets the draft plus the uncited claims, with a rule to work each one into the paragraph where it belongs, in story order, without changing the other sentences. The result is verified again.
- **Then a deterministic fallback.** Any claim still uncited is appended as its own sentence (the claim text, with its citation) at the end of the paragraph whose claims share the most passages with it, or of the section's last paragraph.
- **Result:** no verified claim is ever lost between the claims step and the shown answer. Assert this as an invariant: `cited(shown) ⊇ kept` minus claims the faithfulness check cut.
- Duplicates stay controlled. A claim that the check judged to restate another sentence counts as covered by that sentence. The prompt and the repair say "add only what is new".

### 2. Readings only in labelled sentences

- A `fact` sentence may say only what its cited claims say: no motive, judgement, intent or evaluation words that its claims don't contain.
- The faithfulness check (`check`) already judges sentences. Extend its per-sentence verdict with `inference_in_fact`: the sentence is kind `fact` but states an interpretation not in its claims.
  - A sentence with this verdict is either rewritten as kind `reading`, when READINGS is on and the answer allows readings, which brings the "LORE's reading" label and the checks readings already get;
  - or reduced to its claims' wording, in the repair round above.
- In factual-only answers (no interpretive part), no readings are allowed at all.
- The writer prompt says the same, with one short contrasting example that uses a made-up story, never a real book.

### 3. Measuring it

- Add a cheap deterministic metric, used in tests and in `trace_report`: a per-answer `coverage = cited kept claims / kept claims`. Log it on every prose answer.
- **Eval:** a NEW factual set of 24 questions, both Holmes books, written fresh and not drawn from dev, held-out, arc, interpretive, deep or recap. Arms: `prose_factual=true` with this fix, against `prose_factual=false`.
- **Bar, fixed before the run:**
  - false claims no higher than control;
  - key facts no worse than −2 in total;
  - readability preferred, sign test p < 0.05;
  - fallback at most 25%.
- If it passes, `PROSE_FACTUAL` goes back on.

## Codex identity fix (same branch, separate commit)

The rebuilt codex's identity line for Holmes is "A medical student working at the chemical laboratory". That is Stamford's first impression, which the book overturns one chapter later ("I'm a consulting detective"). "Quote the earliest passage that establishes it" picked the earliest description.

The rule becomes:
- Identity states what the story **establishes** the entity to be: their settled occupation, status or role, and their place among the main figures.
- A first impression or mistaken belief that the text later corrects is not identity.
- Among passages that establish the same settled fact, quote the earliest.
- Give 2 identity items when the text supports two distinct facts, for example occupation plus relation to the protagonist.

The spoiler-safe intro is unchanged: it uses only visible statements, so an identity anchored later than the reader's position simply doesn't show. The intro then falls back to the earlier visible lines.

Changing the prompt changes the prompt hash, so a codex rebuild picks it up. That costs about 72 Gemini requests for the Holmes world, and the controller runs it.

## Out of scope

Changing the claims step, retrieval, or anything about interpretive, deep or recap answers beyond the shared writer rules above.
