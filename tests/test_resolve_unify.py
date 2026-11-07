"""Same name under different kinds or numbers; the text's own equivalences."""
from lore.resolve.unify import find_equivalences, fold_map, majority_kinds


def test_plurals_fold_onto_a_singular_name_written_as_a_common_noun() -> None:
    names = [("b", "the Fades"), ("b", "Fades"), ("b", "The Fade"), ("b", "Halfmen"),
             ("b", "The Halfman"), ("b", "Trollocs"), ("b", "Trolloc")]
    folded = fold_map(names)
    assert folded[("b", "fades")] == "fade"
    assert folded[("b", "halfmen")] == "halfman"
    # "Trollocs"/"Trolloc": neither is ever written as a common noun here
    assert ("b", "trollocs") not in folded
    assert fold_map(names + [("b", "the Trollocs")])[("b", "trollocs")] == "trolloc"


def test_a_plural_without_its_singular_or_a_surname_does_not_fold() -> None:
    assert fold_map([("b", "the Fades")]) == {}
    # "Holmes" and a "Holme" written as names: a surname, not a plural
    assert fold_map([("b", "Holmes"), ("b", "Holme")]) == {}
    # folding is per book
    assert fold_map([("b1", "the Fades"), ("b2", "Fade")]) == {}
    assert fold_map([("b", "Miss"), ("b", "Mis")]) == {}


def test_one_kind_per_name_by_weight_then_kind_order() -> None:
    kinds = majority_kinds({("b", "myrddraal", "Faction"): 41, ("b", "myrddraal", "Character"): 30,
                            ("b", "myrddraal", "Concept"): 3, ("b", "dark one", "Character"): 161,
                            ("b", "dark one", "Concept"): 39, ("b", "eye", "Place"): 2,
                            ("b", "eye", "Object"): 2})
    assert kinds == {("b", "myrddraal"): "Faction", ("b", "dark one"): "Character",
                     ("b", "eye"): "Place"}


_NAMES = ["Myrddraal", "Halfmen", "Eyeless", "Fades", "Ba'alzamon", "The Dark One",
          "Moiraine", "Aes Sedai", "Shai'tan"]


def test_naming_statements_are_merge_candidates() -> None:
    chunks = [("c1", "Myrddraal, also called Halfmen, the Eyeless, and Fades, are the "
                     "Dark One's creatures."),
              ("c2", "The Myrddraal, the Halfmen, as men call them, waited.")]
    found = {(e.name, e.alias, e.believed) for e in find_equivalences(chunks, _NAMES)}
    assert ("Myrddraal", "Halfmen", False) in found
    assert ("Myrddraal", "Eyeless", False) in found
    assert ("Myrddraal", "Fades", False) in found
    assert all(not believed for _, _, believed in found)


def test_an_identity_a_character_claims_is_a_belief_never_a_naming() -> None:
    chunks = [("c3", "“Ba'alzamon is the Dark One,” Rand said."),
              ("c4", "Some believed Shai'tan was the Dark One himself.")]
    found = {(e.name, e.alias, e.believed) for e in find_equivalences(chunks, _NAMES)}
    assert ("Dark One", "Ba'alzamon", True) in found
    assert ("Dark One", "Shai'tan", True) in found


def test_unrelated_sentences_give_nothing() -> None:
    # measured false candidates: a verb "named", and a name later in the sentence
    chunks = [("c5", "Moiraine, the Aes Sedai, rode on. The Myrddraal is near."),
              ("c6", "Moiraine is tired."),
              ("c7", "“Old Moiraine named the Dark One."),
              ("c8", "She plotted with Myrddraal, called Halfmen, to seize the throne for "
                     "Ba'alzamon.")]
    assert {(e.name, e.alias) for e in find_equivalences(chunks, _NAMES)} == {
        ("Myrddraal", "Halfmen")}
    chunks = chunks[:3]
    assert find_equivalences(chunks, _NAMES) == []
