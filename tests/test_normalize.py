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
