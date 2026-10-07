# Grading answer-quality runs

Each run of `run_eval.py` records one answer per question. Grade them
against the verified answers in `questions.json`. The verified answers are copied from
the Phase 3 specs and were checked against the novels. Grade blind: the
grader is not told which run, round or prompt produced an answer. When two
runs are compared, the grader sees both answers to each question in random
order, labelled A and B.

## Per answer

1. **Key facts.** Use the question's `key_facts` list, unchanged: the
   distinct things a complete answer must say. (Before 2026-10-07 the dev
   set had none and each grader split the answer itself, which added
   grading noise; Q1-C10's lists now come from `retrieval/gold.json`.) For Q2 those are: revenge;
   Stangerson shot Lucy's father John Ferrier; Lucy was forced to marry
   Drebber and died within a month; Hope had been going to marry her; he
   pursued them for about twenty years. List each fact and mark it
   **covered** or **missing**. A fact counts as covered only if a claim
   states it. A passage that contains the fact doesn't count unless a
   claim says it.

2. **False claims.** Count claims that state something the verified answer
   or the novels contradict, or that misattribute (for example, the wrong
   person did it). One false claim makes the verdict at best `partly`.

3. **Duplicates.** Count claims that only restate an earlier claim in
   other words.

4. **Verdict**, one of:
   - `correct`: every key fact covered, no false claim.
   - `partly`: some key facts covered, none false, or a minor slip.
   - `refused`: an empty answer, or "the passages do not say", when the
     answer is in the books.
   - `wrong`: the answer's main point is false, or it answers a different
     question.

## Per run

Report:
- verdict counts (correct / partly / refused / wrong)
- total key facts covered out of total
- total false claims
- total duplicates

Plus a one-line note per question saying what was missing or wrong.

**Key-fact coverage is the headline number.** Verdicts are coarse: an
answer that goes from 2 of 5 facts to 4 of 5 is still `partly`, but it is
much better.

## Accepting a change

A prompt or code change is accepted only if, against the run before it:
- key-fact coverage rises,
- there are no new `wrong` verdicts and no new false claims, and
- duplicates don't increase.

If coverage rises but a new false claim appears, the change is rejected,
or fixed and re-run. Correctness comes before fullness.

## Not overfitting

Iterating against the same questions will eventually tune the system to
them instead of making it better. Six rules guard against that.

1. **Two sets.** `questions.json` (56) is the **dev set**. Every change is
   judged on it. `heldout.json` is the **held-out set**. It is never used
   to decide a change, and its per-question answers are not read while
   iterating. It is run only at milestones, to check that a dev gain also
   shows up on questions the changes never saw. A dev gain that does not
   carry over to the held-out set is overfitting, and the change is
   rolled back.
2. **No leakage.** No prompt text may quote or paraphrase an eval question
   or answer. Examples in prompts must come from neither set. The test
   `test_the_prompt_leaks_nothing_from_the_eval_set` catches copied
   phrases but not paraphrases, so every prompt change is also read for
   leakage before it runs.
3. **General changes only.** A change must describe a failure that could
   happen to any question ("stops at one passage", "states a negative
   from excerpts"). It must never describe one particular question. If a
   fix only makes sense for one dev question, it is overfitting.
4. **Noise floor.** Re-running unchanged code gives a slightly different
   score, because Gemini is not fully deterministic even at temperature 0.
   Measure that variance by re-running the baseline. A dev gain smaller
   than the noise floor is not a gain.
5. **Retrieval tuning counts too.** Tuning numbers such as k, caps and
   budgets to the dev set overfits just as prompts do. The same held-out
   check applies.
6. **The grader stays blind and fixed.** It uses the same rubric every
   round, and it is never told which answer is the new one.

## Comparing two arms

Use `run_pair.py`, not two `run_eval.py` runs. Round 3 ran its arms back
to back, and the cascade's daily caps put the dev set on one Gemini model
and the held-out set on another, so a model difference read as a change's
effect. `run_pair.py` asks each question of both arms one after the other
on one pinned model, alternating models between questions, so every
question is a fair pair and the model is balanced across arms.

The dev set grew from 26 to 56 questions (D1-D30, written from the text
on 2026-10-07, clear of every dev and held-out topic) because the ±4-fact
noise floor on 26 questions was as large as the effects being measured.
Re-measure the noise floor on the larger set before relying on it.
