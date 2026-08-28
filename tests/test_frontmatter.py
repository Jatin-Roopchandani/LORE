from lore.ingest.frontmatter import is_front_matter


def test_detects_gutenberg_licence_block() -> None:
    text = """
    This eBook is for the use of anyone anywhere in the United States and
    most other parts of the world at no cost and with almost no restrictions
    whatsoever. You may copy it, give it away or re-use it under the terms
    of the Project Gutenberg License included with this eBook or online at
    www.gutenberg.org. If you are not located in the United States, you
    will have to check the laws of the country where you are located
    before using this eBook.
    """
    assert is_front_matter(text) is True


def test_detects_end_of_ebook_notice() -> None:
    text = """
    *** END OF THE PROJECT GUTENBERG EBOOK PRIDE AND PREJUDICE ***

    Updated editions will replace the previous one--the old editions will
    be renamed.
    """
    assert is_front_matter(text) is True


def test_narrative_prose_is_not_front_matter() -> None:
    text = (
        "It is a truth universally acknowledged, that a single man in "
        "possession of a good fortune, must be in want of a wife. However "
        "little known the feelings or views of such a man may be on his "
        "first entering a neighbourhood, this truth is so well fixed in "
        "the minds of the surrounding families, that he is considered as "
        "the rightful property of some one or other of their daughters."
    )
    assert is_front_matter(text) is False


def test_narrative_mentioning_a_place_is_not_front_matter() -> None:
    text = (
        "Elizabeth smiled at the thought of such a journey. She had never "
        "travelled to the United States, nor did she expect to, but she "
        "delighted in hearing Mr. Darcy describe the ships that sailed "
        "there each spring from the busy ports of England."
    )
    assert is_front_matter(text) is False
