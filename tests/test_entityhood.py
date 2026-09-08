import pytest

from lore.resolve.entityhood import is_probable_entity


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
