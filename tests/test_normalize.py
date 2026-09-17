from lore.resolve.normalize import entity_id, normalize_name


def test_casefolds_and_collapses_whitespace() -> None:
    assert normalize_name("  MISS   BENNET ") == "bennet"


def test_strips_honorifics() -> None:
    assert normalize_name("Mr. Darcy") == "darcy"
    assert normalize_name("Lady Danholm") == "danholm"
    assert normalize_name("Captain Vane") == "vane"


def test_bare_name_matches_honorific_form() -> None:
    assert normalize_name("Darcy") == normalize_name("Mr. Darcy")


def test_strips_possessives_and_punctuation() -> None:
    assert normalize_name("Elizabeth Bennet's") == "elizabeth bennet"
    assert normalize_name("St. John, Esq.") == "john esq"


def test_leading_article_dropped() -> None:
    assert normalize_name("The House of Vaelen") == "house of vaelen"


def test_honorific_only_name_is_not_emptied() -> None:
    # "the Captain" is how a character is named; it must not normalize away.
    assert normalize_name("the Captain") == "captain"


def test_entity_id_is_deterministic_and_kind_scoped() -> None:
    a = entity_id("darcy", "Character")
    assert a == entity_id("darcy", "Character")
    assert a != entity_id("darcy", "Place")
    assert a.startswith("e") and len(a) == 13


def test_honorific_words_survive_inside_a_name() -> None:
    # "King" is a common surname; stripping it mid-name would fuse
    # unrelated characters onto one node.
    assert normalize_name("Larry King") == "larry king"
    assert normalize_name("Martin Luther King") == "martin luther king"
    assert normalize_name("Larry King") != normalize_name("Mr. Larry")


def test_degenerate_names_never_normalize_to_empty() -> None:
    for name in ("The", "the", "A", "an", "Mr."):
        assert normalize_name(name) != ""


def test_contentless_input_yields_empty() -> None:
    # Documented behaviour: no word characters means no key, and callers
    # must skip the mention rather than mint an entity.
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""
    assert normalize_name("...") == ""


# Task 11a — the title a surface form carries, which `normalize_name` strips.
def test_leading_titles_are_the_honorifics_normalize_strips() -> None:
    from lore.resolve.normalize import leading_titles

    assert leading_titles("Major Sholto") == frozenset({"major"})
    assert leading_titles("Mr. Sholto") == frozenset({"mr"})
    assert leading_titles("the Captain Morstan") == frozenset({"captain"})
    assert leading_titles("Sholto") == frozenset()
    # Spellings of one title are one title.
    assert leading_titles("Dr. Watson") == leading_titles("Doctor Watson")
    # A name that is nothing BUT a title keeps it as its name, not its title.
    assert leading_titles("the Captain") == frozenset()
