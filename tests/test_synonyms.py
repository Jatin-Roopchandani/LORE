"""Synonym candidates the text never states as "also called", asked, and
applied only on the answer (lore/resolve/synonyms.py). No network: the
adjudicator is a fake."""
import pytest

from lore.resolve import synonyms as syn
from lore.resolve.disambiguation import AnswerMismatchError
from lore.resolve.normalize import entity_id
from lore.resolve.resolver import AliasRecord, CanonicalEntity, ResolutionResult
from lore.resolve.synonyms import Edge, Entity


def _e(name: str, kind: str = "Character", *names: str) -> Entity:
    return Entity(id=f"id:{name}", name=name, kind=kind, names=(name, *names))


def _cue_pairs(cues) -> set[tuple[frozenset, str]]:
    return {(frozenset({c.a, c.b}), c.shape) for c in cues}


# --------------------------------------------------------------------------
# cues in the text

def test_a_glossary_cross_reference_is_a_cue() -> None:
    ents = [_e("Myrddraal", "Faction"), _e("Halfman", "Faction"), _e("Lurk", "Faction")]
    cues = syn.text_cues([("b:9:1", "Halfman: See Myrddraal. Lurk (LUHRK): See Myrddraal.")],
                         ents)
    assert _cue_pairs(cues) == {(frozenset({"id:Halfman", "id:Myrddraal"}), "see"),
                                (frozenset({"id:Lurk", "id:Myrddraal"}), "see")}
    assert all(c.chunk_id == "b:9:1" for c in cues)


def test_a_glossary_entry_lists_the_other_names_of_its_head() -> None:
    ents = [_e("Myrddraal", "Faction"), _e("Halfmen", "Faction"), _e("Eyeless", "Faction"),
            _e("Fade", "Faction"), _e("Dark One"), _e("One Power", "Concept")]
    text = ("Myrddraal (MUHRD-draal): Creatures of the Dark One, commanders of the Trollocs. "
            "In different lands they are known by many names, among them Halfmen, the "
            "Eyeless, and Fade. One Power, the: The power drawn from the True Source.")
    cues = syn.text_cues([("b:9:2", text)], ents)
    assert _cue_pairs(cues) == {(frozenset({"id:Myrddraal", f"id:{n}"}), "entry")
                                for n in ("Halfmen", "Eyeless", "Fade")}


def test_a_narrated_naming_pairs_the_names_before_with_the_name_after() -> None:
    ents = [_e("Father of Lies"), _e("Dark One"), _e("Moiraine")]
    text = "“The Father of Lies is a good name for the Dark One,” Moiraine replied."
    cues = syn.text_cues([("b:3:7", text)], ents)
    assert _cue_pairs(cues) == {(frozenset({"id:Father of Lies", "id:Dark One"}), "naming")}


def test_a_verb_called_is_no_naming() -> None:
    ents = [_e("Rand"), _e("Mat")]
    assert syn.text_cues([("b:1:1", "Rand called to Mat across the Green.")], ents) == []


def test_an_appositive_is_a_weak_cue_and_a_list_is_none() -> None:
    ents = [_e("Bran al'Vere"), _e("Mayor"), _e("Rand"), _e("Mat"), _e("Perrin")]
    cues = syn.text_cues([("b:1:1", "Bran al'Vere, the Mayor, came out. "
                                    "Rand, Mat, and Perrin ran.")], ents)
    assert _cue_pairs(cues) == {(frozenset({"id:Bran al'Vere", "id:Mayor"}), "appositive")}


def test_names_listed_together_are_two_things() -> None:
    ents = [_e("Trollocs", "Faction"), _e("Myrddraal", "Faction"), _e("Raolin"), _e("Yurian"),
            _e("Davian")]
    listed = syn.listed_together([("b:1:1", "Trollocs and Myrddraal attacked. Raolin, "
                                            "Yurian, and Davian were false Dragons.")], ents)
    assert frozenset({"id:Trollocs", "id:Myrddraal"}) in listed
    assert frozenset({"id:Raolin", "id:Davian"}) in listed
    # a naming sentence is not a list of different things
    ents2 = [_e("Myrddraal", "Faction"), _e("Halfmen", "Faction"), _e("Fade", "Faction")]
    assert syn.listed_together([("b:1:2", "Myrddraal are known by many names, among them "
                                          "Halfmen and Fade.")], ents2) == set()


def test_an_extracted_identity_edge_is_a_cue_and_is_a_a_weak_one() -> None:
    edges = [Edge("f", "SAME_AS", "m", "they all mean Myrddraal", "b:1:1"),
             Edge("l", "IS_A", "w", "Lan, the Warder", "b:1:2"),
             Edge("r", "TALKS_TO", "t", "Rand spoke", "b:1:3")]
    shapes = {(frozenset({c.a, c.b}), c.shape, c.strength) for c in syn.stated_pairs(edges)}
    assert shapes == {(frozenset({"f", "m"}), "stated", 0.8), (frozenset({"l", "w"}), "is_a", 0.15)}
    assert syn.is_identity_predicate("is also known as")
    assert not syn.is_identity_predicate("SERVES")


# --------------------------------------------------------------------------
# candidates

def _book():
    """Two names of one creature used in different chapters, serving one
    master and hunting the same people; a different creature they command; a
    place; and the hero and his father, who act on each other a great deal."""
    ents = [_e("Myrddraal", "Faction"), _e("Fade", "Character"), _e("Trollocs", "Faction"),
            _e("Dark One"), _e("Rand"), _e("Tam"), _e("Mat"), _e("Emond's Field", "Place"),
            _e("Shadar Logoth", "Place")]
    edges, vectors = [], {}

    def chunk(n, direction):
        cid = f"b:1:{n}"
        vectors[cid] = [1.0 if i == direction else 0.0 for i in range(8)]
        return cid

    n = 0
    for name, direction in (("Myrddraal", 0), ("Fade", 1)):
        for _ in range(4):
            n += 1
            c = chunk(n, direction if n % 2 else 2)
            edges += [Edge(f"id:{name}", "SERVES", "id:Dark One", "it served", c),
                      Edge(f"id:{name}", "HUNTS", "id:Rand", "it hunted Rand", c),
                      Edge(f"id:{name}", "HUNTS", "id:Mat", "it hunted Mat", c)]
    for _ in range(4):
        n += 1
        c = chunk(n, 3)
        edges += [Edge("id:Trollocs", "ATTACKS", "id:Emond's Field", "they attacked", c),
                  Edge("id:Trollocs", "SERVES", "id:Myrddraal", "they obeyed", c)]
    for _ in range(6):
        n += 1
        c = chunk(n, 4)
        edges += [Edge("id:Rand", "TALKS_TO", "id:Tam", "they talked", c),
                  Edge("id:Tam", "PROTECTS", "id:Rand", "he carried him", c)]
    n += 1
    edges.append(Edge("id:Mat", "VISITS", "id:Shadar Logoth", "Mat went in", chunk(n, 5)))
    return ents, edges, vectors


def test_similar_contexts_and_neighbours_propose_a_pair() -> None:
    ents, edges, vectors = _book()
    pairs = syn.candidate_pairs(ents, edges, [], vectors)
    keys = {frozenset({p.a, p.b}) for p in pairs}
    assert frozenset({"id:Myrddraal", "id:Fade"}) in keys
    # two who act on each other a great deal are never proposed
    assert frozenset({"id:Rand", "id:Tam"}) not in keys
    # a place is never one with a character
    assert not any("id:Emond's Field" in k and "id:Rand" in k for k in keys)
    pair = next(p for p in pairs if frozenset({p.a, p.b}) == frozenset({"id:Myrddraal",
                                                                         "id:Fade"}))
    assert pair.cue is None and pair.distribution > 0.5


def test_cue_pairs_rank_first_and_the_cap_holds() -> None:
    ents, edges, vectors = _book()
    chunks = [("b:9:9", "Shadar Logoth: See Emond's Field.")]   # a (false) cross-reference
    pairs = syn.candidate_pairs(ents, edges, chunks, vectors)
    assert frozenset({pairs[0].a, pairs[0].b}) == frozenset({"id:Shadar Logoth",
                                                              "id:Emond's Field"})
    assert pairs[0].cue.shape == "see"
    assert len(syn.candidate_pairs(ents, edges, chunks, vectors, cap=1)) == 1


def test_name_shapes_the_resolver_rules_on_are_never_candidates() -> None:
    ents = [_e("Red Ajah", "Faction"), _e("Yellow Ajah", "Faction"), _e("Holmes"),
            _e("Sherlock Holmes"), _e("King"), _e("Artur the High King")]
    chunks = [("b:9:1", "Red Ajah: See Yellow Ajah. Holmes: See Sherlock Holmes. "
                        "King: See Artur the High King.")]
    assert syn.candidate_pairs(ents, [], chunks, {}) == []


def test_a_pair_met_only_together_has_no_context_similarity() -> None:
    edges = [Edge("a", "X", "c", "ev", "b:1:1"), Edge("b", "X", "c", "ev", "b:1:1")]
    contexts = syn.Contexts(edges, {"b:1:1": [1.0, 0.0], "b:1:2": [0.0, 1.0]})
    assert contexts.cosine("a", "b") == 0.0
    assert contexts.together("a", "b") == 1.0


# --------------------------------------------------------------------------
# questions

def _question(a="Myrddraal", b="Fade", book="b", together="", chunk="") -> syn.SynonymQuestion:
    return syn.SynonymQuestion(book, syn.Side(f"id:{a}", a, "Faction", (f"{a} rode.",)),
                               syn.Side(f"id:{b}", b, "Faction", (f"{b} watched.",)),
                               together, chunk)


def test_questions_carry_both_sides_evidence_and_the_linking_sentence() -> None:
    ents, edges, vectors = _book()
    pairs = syn.candidate_pairs(ents, edges, [], vectors)
    pair = next(p for p in pairs if {p.a, p.b} == {"id:Myrddraal", "id:Fade"})
    (q,) = syn.questions_for("b", [pair], ents, edges)
    assert {q.a.name, q.b.name} == {"Myrddraal", "Fade"}
    assert q.a.snippets and q.b.snippets and len(q.a.snippets) <= syn.SNIPPETS
    prompt = syn.synonym_prompt([q])
    assert "Myrddraal" in prompt and "Fade" in prompt and "same, claimed or different" in prompt


def test_snippets_prefer_sentences_that_name_the_entity() -> None:
    ents = [_e("Fade", "Faction", "the Fade"), _e("Myrddraal", "Faction"), _e("Rand")]
    edges = [Edge("id:Fade", "HUNTS", "id:Rand", "He should have known.", "b:1:1"),
             Edge("id:Fade", "HUNTS", "id:Rand", "She is dead.", "b:1:2"),
             Edge("id:Fade", "HUNTS", "id:Rand", "And a Fade came.", "b:1:3"),
             Edge("id:Myrddraal", "HUNTS", "id:Rand", "The Myrddraal rode.", "b:1:4")]
    pair = syn.Pair("id:Fade", "id:Myrddraal", 1.0)
    (q,) = syn.questions_for("b", [pair], ents, edges)
    fade = q.a if q.a.name == "Fade" else q.b
    assert fade.snippets[0] == "And a Fade came."


def test_the_question_key_is_the_book_and_the_pair_whatever_the_snippets() -> None:
    q1 = _question()
    q2 = syn.SynonymQuestion("b", q1.b, syn.Side(q1.a.id, q1.a.name, q1.a.kind, ("other",)))
    assert q1.key == q2.key
    assert _question(book="other").key != q1.key
    assert syn.SynonymQuestion.from_dict(q1.as_dict()) == q1


def test_answers_map_back_by_number_and_echo() -> None:
    qs = [_question("Myrddraal", "Fade"), _question("Ba'alzamon", "Dark One"),
          _question("Rand", "Tam")]
    text = ("1. Fade / Myrddraal -> same\n2. Ba'alzamon (Faction) / Dark One -> claimed\n"
            "3. Rand / Tam -> they are father and son")
    answers = syn.parse_synonym_answers(qs, text)
    assert [answers[q.key] for q in qs] == ["same", "claimed", "different"]


@pytest.mark.parametrize("text", [
    "1. Fade / Myrddraal -> same",                                   # one missing
    "1. Fade / Myrddraal -> same\n2. Rand / Tam -> same",            # wrong echo
    "1. Fade / Myrddraal -> same\n1. Fade / Myrddraal -> same\n2. Dark One / Ba'alzamon -> no",
])
def test_an_answer_set_that_does_not_line_up_is_refused(text) -> None:
    qs = [_question("Myrddraal", "Fade"), _question("Ba'alzamon", "Dark One")]
    with pytest.raises(AnswerMismatchError):
        syn.parse_synonym_answers(qs, text)


class _FakeClient:
    def __init__(self, verdict: str = "same") -> None:
        self.prompts: list[str] = []
        self.verdict = verdict

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        import re
        lines = re.findall(r"^(\d+)\. (.+?) \(\w+\) / (.+?) \(\w+\)$", prompt, re.MULTILINE)
        return "\n".join(f"{n}. {a} / {b} -> {self.verdict}" for n, a, b in lines)


@pytest.mark.asyncio
async def test_asking_batches_the_questions() -> None:
    qs = [_question(f"A{i}", f"B{i}") for i in range(5)]
    client = _FakeClient()
    answers = await syn.ask_synonyms(client, qs, max_batch=2)
    assert len(client.prompts) == 3
    assert set(answers.values()) == {"same"} and len(answers) == 5


# --------------------------------------------------------------------------
# applying the answers

def test_same_answers_merge_into_the_heaviest_and_claims_stay_links() -> None:
    qs = {"mf": _question("Myrddraal", "Fade"), "fh": _question("Fade", "Halfman"),
          "bd": _question("Ba'alzamon", "Dark One", together="Ba'alzamon is the Dark One",
                          chunk="b:1:5"),
          "rt": _question("Rand", "Tam")}
    weight = {"id:Myrddraal": 70, "id:Fade": 60, "id:Halfman": 30, "id:Ba'alzamon": 100,
              "id:Dark One": 200, "id:Rand": 1000, "id:Tam": 200}
    applied = syn.plan([(qs["mf"], "same"), (qs["fh"], "same"), (qs["bd"], "claimed"),
                        (qs["rt"], "different")], [], weight)
    assert applied.merged == {"id:Myrddraal": ["id:Fade", "id:Halfman"]}
    assert applied.claimed == [("id:Ba'alzamon", "id:Dark One", "Ba'alzamon is the Dark One",
                                "b:1:5")]


def test_a_same_answer_never_joins_two_that_act_on_each_other() -> None:
    edges = [Edge("id:Rand", "TALKS_TO", "id:Tam", "ev", f"b:1:{i}") for i in range(10)]
    applied = syn.plan([(_question("Rand", "Tam"), "same")], edges,
                       {"id:Rand": 1000, "id:Tam": 20})
    assert applied.merged == {} and applied.refused == [("id:Rand", "id:Tam")]
    # a few edges the extractor wrote between two names of one thing do not block
    few = [Edge("id:Myrddraal", "INTERACTS_WITH", "id:Fade", "ev", f"b:1:{i}") for i in range(4)]
    applied = syn.plan([(_question("Myrddraal", "Fade"), "same")], few,
                       {"id:Myrddraal": 76, "id:Fade": 64})
    assert applied.merged == {"id:Myrddraal": ["id:Fade"]}


def test_merging_repoints_mentions_aliases_and_alias_records() -> None:
    m, f = entity_id("myrddraal", "Faction"), entity_id("fade", "Faction")
    old = entity_id("fades", "Character")
    resolution = ResolutionResult(
        entities=[CanonicalEntity(m, "Myrddraal", "Faction", ("myrddraal",), ("b",)),
                  CanonicalEntity(f, "The Fade", "Faction", ("fade", "fades"), ("b",))],
        aliases=[AliasRecord(old, "Fades", "Character", f)],
        mention_map={("Myrddraal", "Faction", "b"): m, ("The Fade", "Faction", "b"): f,
                     ("Fades", "Character", "b"): f})
    merged = syn.merge_resolution(resolution, {m: [f]})
    assert [e.id for e in merged.entities] == [m]
    assert set(merged.entities[0].aliases) == {"myrddraal", "fade", "fades"}
    assert set(merged.mention_map.values()) == {m}
    assert {(a.alias_id, a.canonical_id) for a in merged.aliases} == {(old, m), (f, m)}
    # the input is untouched
    assert len(resolution.entities) == 2
