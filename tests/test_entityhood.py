import pytest

from lore.resolve.entityhood import is_generic_name, is_probable_entity


@pytest.mark.parametrize("name, expected", [
    ("the box", False),
    ("his hands", False),
    ("The two detectives", False),
    ("My dear fellow", False),
    ("the Marchioness de Brinvilliers", True),
    ("The Daily News", True),
    ("the United States", True),
    ("Sherlock Holmes", True),
])
def test_table_from_the_brief(name: str, expected: bool) -> None:
    assert is_probable_entity(name) is expected


def test_empty_string_is_rejected_without_crashing() -> None:
    assert is_probable_entity("") is False


def test_whitespace_only_is_rejected_without_crashing() -> None:
    assert is_probable_entity("   ") is False


def test_single_capitalised_word_is_kept() -> None:
    assert is_probable_entity("Holmes") is True


def test_possessive_chain_is_rejected() -> None:
    assert is_probable_entity("his two companions") is False


def test_only_capital_is_the_sentence_initial_determiner() -> None:
    # "pass" is not capitalised once "The" is stripped.
    assert is_probable_entity("The pass") is False


def test_leading_curly_quote_before_accented_capital_is_kept() -> None:
    assert is_probable_entity("’Émile") is True


def test_bare_accented_capital_is_kept() -> None:
    assert is_probable_entity("Émile") is True


# Self-review probes beyond the brief's table.


def test_name_is_only_a_determiner_is_rejected() -> None:
    assert is_probable_entity("the") is False
    assert is_probable_entity("The") is False


def test_name_is_only_punctuation_is_rejected() -> None:
    assert is_probable_entity("...") is False
    assert is_probable_entity("!!!") is False


def test_leading_and_trailing_whitespace_does_not_change_verdict() -> None:
    assert is_probable_entity("  Sherlock Holmes  ") is True
    assert is_probable_entity("   the box   ") is False


def test_internal_double_spaces_do_not_change_verdict() -> None:
    assert is_probable_entity("Sherlock  Holmes") is True


def test_trailing_period_does_not_change_verdict() -> None:
    assert is_probable_entity("Sherlock Holmes.") is True


def test_hyphenated_name_is_kept() -> None:
    assert is_probable_entity("Jabez Wilson-Smith") is True


def test_apostrophed_name_is_kept() -> None:
    assert is_probable_entity("O'Brien") is True


def test_possessive_of_a_named_entity_is_kept() -> None:
    # "Mormon" is capitalised, so stripping the leading "the" still leaves
    # a capitalised token behind.
    assert is_probable_entity("the Mormon's son") is True


def test_initial_as_the_capitalised_token_is_kept() -> None:
    assert is_probable_entity("J. Ferrier") is True


def test_roman_numeral_as_the_capitalised_token_is_kept() -> None:
    assert is_probable_entity("Henry V") is True


# --------------------------------------------------------------------------
# Task 1h defect 1: bare pronouns were entities. `I` was the second-busiest
# node in the whole graph (116 edges); nine pronoun nodes existed in all.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "i", "he", "she", "we", "they", "you", "it", "me", "him", "her", "us",
    "them",
])
def test_every_bare_pronoun_is_rejected_lowercase(name: str) -> None:
    assert is_probable_entity(name) is False


@pytest.mark.parametrize("name", [
    "I", "He", "She", "We", "They", "You", "It", "Me", "Him", "Her", "Us",
    "Them",
])
def test_every_bare_pronoun_is_rejected_capitalised(name: str) -> None:
    assert is_probable_entity(name) is False


@pytest.mark.parametrize("name", ["YOU", "HE", "I", "We", "You"])
def test_the_pronoun_nodes_measured_in_the_live_graph_are_rejected(
    name: str,
) -> None:
    assert is_probable_entity(name) is False


@pytest.mark.parametrize("name", ["He.", "  I  ", "\tYou\n", "it,", "(we)"])
def test_a_pronoun_with_whitespace_or_punctuation_is_still_rejected(
    name: str,
) -> None:
    assert is_probable_entity(name) is False


@pytest.mark.parametrize("name", [
    "Ivan", "Ida", "Iris", "Wells", "Hera", "Item", "Mel", "Uso", "Sheila",
    "Theydon", "Herr Muller", "He Xiaolong", "I. Ferrier",
])
def test_a_name_that_merely_starts_with_a_pronoun_survives(name: str) -> None:
    assert is_probable_entity(name) is True


def test_a_pronoun_that_is_only_part_of_a_longer_name_survives() -> None:
    assert is_probable_entity("He Xiaolong") is True
    assert is_probable_entity("The Us Weekly") is True


# --------------------------------------------------------------------------
# Task 1h defect 2: all-caps chapter headings were entities, and because a
# long heading shares a token with many real names the resolver pulled real
# characters into them — `john watson` merged onto a table-of-contents row.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "CHAPTER II",
    "CHAPTER III",
    "CHAPTER IV",
    "THE CONCLUSION",
    "THE COUNTRY OF THE SAINTS",
    "A FLIGHT FOR LIFE",
    "A CONTINUATION OF THE REMINISCENCES OF JOHN WATSON, M.D.",
    "A STUDY IN SCARLET",
    "ON THE GREAT ALKALI PLAIN",
    "OUR ADVERTISEMENT BRINGS A VISITOR",
    "PART II",
    "THE AVENGING ANGELS",
    "THE FLOWER OF UTAH",
    "THE LAURISTON GARDENS MYSTERY",
    "THE SCIENCE OF DEDUCTION",
    "TOBIAS GREGSON SHOWS WHAT HE CAN DO",
    "WHAT JOHN RANCE HAD TO TELL",
    "JOHN FERRIER TALKS WITH THE PROPHET",
    "MR. SHERLOCK HOLMES",
    "JOHN WATSON",
])
def test_all_caps_multi_word_headings_are_rejected(name: str) -> None:
    assert is_probable_entity(name) is False


@pytest.mark.parametrize("name", [
    "Sherlock Holmes", "Dr. John Watson", "Mr. Lestrade", "John Ferrier",
    "The Daily News", "the United States", "Mrs. Cecil Forrester",
])
def test_real_names_in_prose_case_survive_the_heading_rule(name: str) -> None:
    assert is_probable_entity(name) is True


@pytest.mark.parametrize("name", ["IRS", "FOUNDATION", "PROPHET", "M.D."])
def test_a_single_all_caps_token_is_not_over_reached(name: str) -> None:
    assert is_probable_entity(name) is True


def test_uncased_multi_word_name_is_not_treated_as_all_caps() -> None:
    # str.isupper() is False with no cased characters; the ordinary rule
    # (no capitalised token) must be what rejects this, not the heading rule.
    assert is_probable_entity("221 43") is False


# --------------------------------------------------------------------------
# Task 1h defect 3: the cross-book merge decision. `is_generic_name` is the
# deterministic replacement for the 3B adjudicator, which measurably refused
# `Sherlock Holmes` <-> `Mr. Sherlock Holmes` while accepting
# `the police Inspector` <-> `the police Inspector`.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "police inspector",
    "the police Inspector",
    "inspector",
    "Inspector",
    "INSPECTOR",
    "the Inspector's",
    "inspector.",
    "the Inspector",
    "captain",
    "the Captain",
    "the Doctor",
    "friend, the Doctor",
    "the woman",
    "a man",
    "the stranger",
    "The Prisoner",
    "servant",
    "his companion",
])
def test_generic_names_are_generic(name: str) -> None:
    assert is_generic_name(name) is True


@pytest.mark.parametrize("name", [
    "sherlock holmes",
    "Sherlock Holmes",
    "Mr. Sherlock Holmes",
    "Inspector Lestrade",
    "Doctor Watson",
    "Dr. Watson",
    "Captain Morstan",
    "Colonel Sebastian Moran",
    "Professor Moriarty",
    "Lestrade",
    "Brigham Young",
    "Young",
    "Jonathan Small",
    "Small",
    "Jefferson Hope",
    "the Marchioness de Brinvilliers",
    "Baker Street",
    "the United States",
])
def test_distinctive_names_are_not_generic(name: str) -> None:
    assert is_generic_name(name) is False


def test_a_name_with_no_tokens_is_not_generic() -> None:
    # "every token is generic" is vacuously true of no tokens; that must not
    # become a refusal, so an empty name is reported distinctive.
    assert is_generic_name("") is False
    assert is_generic_name("   ") is False
    assert is_generic_name("...") is False


def test_one_real_name_beside_a_role_word_is_enough_to_be_distinctive() -> None:
    assert is_generic_name("the police Inspector") is True
    assert is_generic_name("the police Inspector Lestrade") is False


# --------------------------------------------------------------------------
# Task 1i defect 2: Gutenberg header / licence boilerplate.
#
# The seven names the brief measured spanning both novels are minted from
# Gutenberg's header and licence, which are ingested as ordinary chapters.
# The fix is keyed on the PASSAGE, not on the name, so the assertions below
# quote the real chunk text from `data/`'s rebuilt corpus.

# `05ab674df048d13f:1:0` and `0ee225a9a3b5fbc2:1:0` — the header block that
# mints `Arthur Conan Doyle` (Faction) and `United States`.
_HEADER = (
    "This eBook is for the use of anyone anywhere in the United States and "
    "most other parts of the world at no cost and with almost no restrictions "
    "whatsoever. If you are not located in the United States, you will have "
    "to check the laws of the country where you are located before using this "
    "eBook. Title: A Study in Scarlet Author: Arthur Conan Doyle Release "
    "date: April 1, 1995 [eBook #244] Most recently updated: December 9, 2025 "
    "Language: English Credits: Roger Squires and David Widger"
)

# `05ab674df048d13f:19:0` — mints `The Foundation`.
_LICENCE_HEAD = (
    "PLEASE READ THIS BEFORE YOU DISTRIBUTE OR USE THIS WORK Section 1. 1.A. "
    "1.B. It may only be used on or associated in any way with an electronic "
    "work by people who agree to be bound by the terms of this agreement. "
    "The Foundation makes no representations concerning the copyright status "
    "of any work in any country other than the United States."
)

# `05ab674df048d13f:19:1` — mints `TRADEMARK_OWNER`, `DISTRIBUTOR`,
# `FOUNDATION`.
_LICENCE_DISCLAIMER = (
    "1.F.1. 1.F.2. YOU AGREE THAT YOU HAVE NO REMEDIES FOR NEGLIGENCE, "
    "STRICT LIABILITY, BREACH OF WARRANTY OR BREACH OF CONTRACT EXCEPT THOSE "
    "PROVIDED IN PARAGRAPH 1.F.3. YOU AGREE THAT THE FOUNDATION, THE "
    "TRADEMARK OWNER, AND ANY DISTRIBUTOR UNDER THIS AGREEMENT WILL NOT BE "
    "LIABLE TO YOU FOR ACTUAL, DIRECT, INDIRECT, CONSEQUENTIAL, PUNITIVE OR "
    "INCIDENTAL DAMAGES."
)

# `05ab674df048d13f:19:3` — mints `50 states of the United States` and
# `the IRS`.
_LICENCE_DONATIONS = (
    "The Foundation is committed to complying with the laws regulating "
    "charities and charitable donations in all 50 states of the United "
    "States. Compliance requirements are not uniform and it takes a "
    "considerable effort, much paperwork and many fees to meet and keep up "
    "with these requirements. Thus, we do not necessarily keep eBooks in "
    "compliance with any particular paper edition."
)

# `05ab674df048d13f:18:10` — the MIXED chunk: the novel's last paragraph
# runs straight into the licence with no chapter break.
_MIXED_TAIL = (
    "“That’s the result of all our Study in Scarlet: to get them a "
    "testimonial!” “Never mind,” I answered, “I have all "
    "the facts in my journal, and the public shall know them.” Updated "
    "editions will replace the previous one—the old editions will be "
    "renamed. Creating the works from print editions not protected by U.S. "
    "copyright law means that no one owns a United States copyright in these "
    "works, so the Foundation (and you!) can copy and distribute it in the "
    "United States. START: FULL LICENSE"
)


@pytest.mark.parametrize("text", [
    _HEADER,
    _LICENCE_HEAD,
    _LICENCE_DISCLAIMER,
    _LICENCE_DONATIONS,
    _MIXED_TAIL,
    "*** END OF THE PROJECT GUTENBERG EBOOK PRIDE AND PREJUDICE ***",
    "Section 3. Information about the Project Gutenberg Literary Archive "
    "Foundation",
    "Please visit https://www.gutenberg.org/donate/ for donation methods.",
    "Email contact links and up to date contact information can be found at "
    "the Foundation's website and official page at www.gutenberg.org/contact",
    "1.C. The Project Gutenberg™ trademark is owned by the Foundation.",
])
def test_gutenberg_boilerplate_is_detected(text: str) -> None:
    from lore.resolve.entityhood import is_gutenberg_boilerplate

    assert is_gutenberg_boilerplate(text) is True


@pytest.mark.parametrize("text", [
    # Real prose from both novels, including every passage that names one of
    # the entities the licence stoplist approach would have had to ban.
    "In the year 1878 I took my degree of Doctor of Medicine of the "
    "University of London, and proceeded to Netley to go through the course "
    "prescribed for surgeons in the army.",
    "(Being a reprint from the Reminiscences of John H. Watson, M.D., Late "
    "of the Army Medical Department.)",
    "There are seventy odd millions in the United States, and Utah is only "
    "one small corner of it.",
    "He comes to England with the double idea of regaining what he would "
    "consider to be his rights.",
    "“For me,” said Sherlock Holmes, “there still remains the "
    "cocaine-bottle.” And he stretched his long white hand up for it.",
    "The Foundation stone of the temple was laid that spring, and the "
    "Elders gave thanks.",
    "Arthur Conan Doyle sat in the chair by the fire and said nothing.",
    "",
    "   ",
])
def test_ordinary_prose_is_not_boilerplate(text: str) -> None:
    from lore.resolve.entityhood import is_gutenberg_boilerplate

    assert is_gutenberg_boilerplate(text) is False


def test_boilerplate_detection_is_case_insensitive() -> None:
    from lore.resolve.entityhood import is_gutenberg_boilerplate

    assert is_gutenberg_boilerplate("project gutenberg literary archive") is True
    assert is_gutenberg_boilerplate("PROJECT GUTENBERG LITERARY ARCHIVE") is True


def test_the_names_the_brief_measured_are_still_ordinary_names() -> None:
    """The fix is keyed on the passage, so no ordinary word is banned.

    `The Foundation`, `United States` and `Arthur Conan Doyle` remain
    perfectly legal entity names — a different book may use any of them.
    """
    from lore.resolve.entityhood import is_gutenberg_boilerplate

    for name in ("The Foundation", "the United States", "Arthur Conan Doyle",
                 "the IRS", "DISTRIBUTOR", "TRADEMARK_OWNER", "FOUNDATION",
                 "50 states of the United States"):
        assert is_gutenberg_boilerplate(name) is False


@pytest.mark.parametrize("name", ["Narrator", "narrator", "NARRATOR", "Narrator.", "The Narrator"])
def test_a_bare_narrator_role_is_rejected(name: str) -> None:
    # Speaker resolution rewrites "I"/"the narrator" to a named speaker only
    # when it is certain. An unresolved capitalised "Narrator" used to pass as
    # a name and became its own node (64 edges on the Gemini-built graph).
    # A role is not a person; dropping it matches what happens to "I".
    assert is_probable_entity(name) is False


def test_a_name_containing_narrator_is_kept() -> None:
    assert is_probable_entity("The Narrator's Guild of Bath") is True
