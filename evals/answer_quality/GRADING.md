# Grading answer-quality runs

Each run of `run_eval.py` records 26 answers. Grade them against the
verified answers in `questions.json`. The verified answers are copied from
the Phase 3 specs and were checked against the novels. Grade blind: the
grader is not told which run, round or prompt produced an answer. When two
runs are compared, the grader sees both answers to each question in random
order, labelled A and B.

## Per answer

1. **Key facts.** Split the verified answer into its key facts, the
   distinct things a complete answer must say. For Q2 those are: revenge;
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
