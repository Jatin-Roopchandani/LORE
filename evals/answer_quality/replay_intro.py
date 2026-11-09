"""Offline replay of the intro-reliability failure (spec intro-reliability, 2026-11-09).

No model is called. Scripted writer and check replies, shaped like the recorded
deep answer to "explain <a people>" (6 sections, 38 calls; its trace files were
gone when this was written, so the replies here are synthetic, with made-up
names), go through the real code: a section (synthesize), a lens (deep.cited_prose)
and the short answer (deep.short_answer), once with the three arms off and once
with them on. For each unit it prints what would be shown, the openers that
survive with their attached citations, and the calls made.

The replies:
- the gist's writer opens with a sentence summing up three kept claims but citing
  one; the check, judging it on its cited claim alone, rejects it as overreach;
  judged as an opening against every claim, it passes and names its supporters;
- an introduction repair that writes another detail, not an introduction (the
  arms-off path), and a named one (INTRO_PROMPT, arms on);
- the last section: the first draft loses only its opener; the repair loses two
  sentences and leaves an orphan.

    PYTHONPATH=. python evals/answer_quality/replay_intro.py
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from lore.query import deep
from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import AnswerSection, BookInfo, Claim, Evidence, Passage, ProseSentence, SectionSpec
from lore.query.write import Prose, Sentence

FACTS = [
    "The Varn are giants, taller than any man.",
    "The Varn love books and keep great libraries.",
    "The Varn build in stone and are famed as builders.",
    "The Varn must return to their groves or sicken.",
    "Tolo is a young Varn who left his grove to see the world.",
    "Tolo reads books whenever he can.",
]
PS = tuple(Passage(n=i, chunk_id=f"b:{i}:0", book_id="b", chapter_seq=i, text=t)
           for i, t in enumerate(FACTS, start=1))
CS = [Claim(text=t, passage=i, quote=t.rstrip(".")) for i, t in enumerate(FACTS, start=1)]
BOOKS = {"b": BookInfo("The Long Road")}
TITLES = {"b": "The Long Road"}
EVIDENCE = Evidence(edges=(), passages=PS, truncated=False)

# What the gist writer gave: three claims summed up, one cited.
GIST_OPENER = ("The Varn are giant, book-loving builders bound to their groves, and the book "
               "shows them mainly through Tolo.")
DETAIL = "Tolo reads books whenever he can."
NAMED = ("The Varn are a people of giants, lovers of books and builders in stone, who must "
         "return to their groves, and the story shows them through Tolo.")
EMBELLISHED = "The Varn are ancient and wise beyond all other peoples."


def S(text, claims, new_paragraph=False):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph)


class Replay:
    """Scripted writer and check. `drafts`: replies to the writer and repair
    prompts in turn; `intro`: the reply to the introduction prompt. The check
    rejects `rejected` sentences, and rejects GIST_OPENER unless the prompt judges
    it as an opening; an opening judged so is supported by `support`."""

    def __init__(self, drafts, intro=None, rejected=(), support=(1, 2, 3, 4, 5)):
        self.drafts, self.intro = list(drafts), intro
        self.rejected = set(rejected)
        self.not_intro: set[str] = set()  # rejected by the introduction check only
        self.support = list(support)
        self.calls: list[str] = []
        self._writes = 0

    async def write(self, prompt, schema):
        if prompt.startswith("Write the introduction"):
            self.calls.append("intro-write")
            if self.intro is None:
                raise RuntimeError("no introduction scripted")
            return self.intro
        self.calls.append("write")
        self._writes += 1
        got = self.drafts[min(self._writes, len(self.drafts)) - 1]
        if isinstance(got, Exception):
            raise got
        return got

    async def check(self, prompt, schema):
        intro = prompt.startswith("Check an introduction")
        self.calls.append("intro-check" if intro else "check")
        opens = intro or "Sentence 1 opens the text" in prompt
        n = len(re.findall(r"^\d+\. claim: ", prompt, re.M))
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        judged = []
        for i, t in enumerate(texts, start=1):
            first = intro or (i == 1 and opens)
            ok = (t not in self.rejected and (t != GIST_OPENER or first)
                  and not (intro and t in self.not_intro))
            judged.append({"claim": i, "verdict": "supported" if ok else "not_supported",
                           "reason": "overreach" if not ok else "",
                           "supported_by": [k for k in self.support if k <= n] if first and ok else []})
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "restated_by": 0}
                           for i in range(1, n + 1)],
            "sentence_judgements": judged})

    def models(self):
        return SimpleNamespace(write=self.write, check=self.check, spend_ok=None)


ARMS = ("OPENER_CITES", "INTRO_REPAIR", "GENTLE_FALLBACK")


def arms(on: bool) -> None:
    for name in ARMS:
        setattr(w, name, on)


def show(prose) -> str:
    if not prose:
        return "(none)"
    return " | ".join(s.text for s in prose[:2]) + ("" if len(prose) <= 2 else f" | ... ({len(prose)} sentences)")


def cites(prose) -> str:
    return f"opener cites {list(prose[0].claims)}" if prose else ""


async def section(drafts, intro, rejected=()):
    r = Replay(drafts, intro, rejected)
    spec = SectionSpec(part_of="Explain the Varn", max_claims=12, length="section", focus="The Varn")
    a = await synth.synthesize("The Varn", EVIDENCE, lambda p, s: _claims(), mode="graph",
                               check=r.check, write=r.write, book_titles=TITLES, books=BOOKS,
                               section=spec)
    return a, r


async def _claims():
    return synth.Synthesis(can_answer=True, claims=[
        {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CS])


def _section_prose(heading, texts, start):
    prose = tuple(ProseSentence(text=t, claims=(start + i,), passages=(start + i,),
                                new_paragraph=i == 0) for i, t in enumerate(texts))
    return AnswerSection(heading=heading, prose=prose, text=" ".join(texts),
                         claims_start=start, claims_end=start + len(texts) - 1)


async def main() -> None:
    first_section = [S(GIST_OPENER, [1], True), S("The Varn love books and keep great libraries.", [2]),
                     S("The Varn build in stone and are famed as builders.", [3]),
                     S("The Varn must return to their groves or sicken.", [4], True),
                     S("Tolo is a young Varn who left his grove to see the world.", [5], True),
                     S(DETAIL, [6])]
    # the last section: its opener is rejected and the sentence after it leans on it
    last_first = Prose(sentences=[S(EMBELLISHED, [1, 2], True),
                                  S("They love books and keep great libraries.", [2]),
                                  *first_section[2:]])
    last_repair = Prose(sentences=[
        S("The Varn are giants who love books.", [1, 2], True),
        S("The Varn are ancient builders of every city.", [3]),
        S("They build in stone and are famed as builders.", [3]),
        S("The Varn must return to their groves or sicken.", [4], True),
        S("Tolo, the wisest Varn, left his grove.", [5], True),
        S(DETAIL, [6])])
    rejected = {EMBELLISHED, "The Varn are ancient builders of every city.",
                "Tolo, the wisest Varn, left his grove."}
    gist = Prose(sentences=[S(GIST_OPENER, [1]), S(DETAIL, [6])])
    for on in (False, True):
        arms(on)
        print(f"=== arms {'ON' if on else 'OFF'} ({', '.join(ARMS)}) ===")
        a, r = await section([Prose(sentences=first_section)], Prose(sentences=[S(NAMED, [1, 2, 3, 4, 5])]))
        print(f"section (opener under-cited): {a.answer_style}; {cites(a.prose)}; calls {r.calls}")
        print(f"  shows: {show(a.prose)}")
        a, r = await section([last_first, Prose(sentences=last_repair.sentences)],
                             Prose(sentences=[S(NAMED, [1, 2, 3, 4, 5])]), rejected)
        print(f"last section (repair loses 2, orphan): {a.answer_style}; calls {r.calls}")
        print(f"  shows: {show(a.prose) if a.prose else a.text.splitlines()[0]}")
        r = Replay([Prose(sentences=first_section)])
        got = await deep.cited_prose("The Varn", ["The Varn"], CS, PS, r.models(), length="lens",
                                     book_titles=TITLES, books=BOOKS)
        print(f"lens: {cites(got)}; calls {r.calls}\n  shows: {show(got)}")
        sections = [_section_prose("Who the Varn are", FACTS[:4], 1),
                    _section_prose("Tolo", FACTS[4:], 5)]
        # The gist's opening fails even as an opening. The introduction call is
        # replayed twice: as recorded (another detail, which the introduction check
        # is now told to reject as "not an introduction"), and with a named one.
        for label, intro in (("intro repair writes a detail", S(DETAIL, [6])),
                             ("intro repair writes an introduction", S(NAMED, [1, 2, 3, 4, 5]))):
            r = Replay([gist], Prose(sentences=[intro]))
            r.rejected.add(GIST_OPENER)
            if intro.text == DETAIL:
                r.not_intro.add(DETAIL)
            short = await deep.short_answer("Explain the Varn", sections, CS, PS, r.models(),
                                            book_titles=TITLES, books=BOOKS)
            print(f"short answer, opener rejected even as an opening, {label}: calls {r.calls}\n"
                  f"  shows: {show(short)}")
        r = Replay([gist])
        short = await deep.short_answer("Explain the Varn", sections, CS, PS, r.models(),
                                        book_titles=TITLES, books=BOOKS)
        print(f"short answer, as recorded: {cites(short)}; calls {r.calls}\n  shows: {show(short)}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
