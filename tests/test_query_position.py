"""Story position: where a passage sits in its book and series, and which
books can be ordered against each other at all."""
from lore.query.models import BookInfo, Passage
from lore.query.position import order_passages


def p(n: int, book: str, chapter: int, seq: int) -> Passage:
    return Passage(n=n, chunk_id=f"{book}:{chapter}:{seq}", book_id=book,
                   chapter_seq=chapter, text=f"text {n}")


BOOKS = {
    "a": BookInfo("Alpha", "The Saga", 1.0),
    "b": BookInfo("Beta", "The Saga", 2.0),
    "c": BookInfo("Gamma", "The Saga", 10.0),
    "x": BookInfo("Loose One"),
    "y": BookInfo("Loose Two"),
    "z": BookInfo("Other Series Book", "Another Cycle", 1.0),
}


def numbers(order) -> list[int]:
    return [pos.n for pos in order.ordered]


def test_one_book_is_ordered_by_chapter_then_chunk_position() -> None:
    order = order_passages([p(1, "x", 3, 0), p(2, "x", 1, 2), p(3, "x", 1, 10), p(4, "x", 1, 0)], BOOKS)
    assert numbers(order) == [4, 2, 3, 1]  # 10 after 2: numeric, not text, order
    assert order.note() == ""


def test_books_of_one_series_follow_the_series_index_numerically() -> None:
    order = order_passages([p(1, "c", 1, 0), p(2, "a", 9, 0), p(3, "b", 2, 0), p(4, "a", 1, 0)], BOOKS)
    assert numbers(order) == [4, 2, 3, 1]  # book 1, 2, then 10 (not "10" < "2")
    assert order.note() == ""


def test_labels_name_the_book_chapter_and_position_in_the_series() -> None:
    order = order_passages([p(1, "b", 5, 0), p(2, "x", 2, 0)], BOOKS)
    assert order.positions[1].label() == "Beta, book 2 of The Saga, chapter 5"
    assert order.positions[2].label() == "Loose One, chapter 2"
    assert order.positions[1].label(detail=True) == (
        "Beta, book 2 of The Saga, chapter 5, passage 1 in the chapter")


def test_a_fractional_index_is_shown_as_is() -> None:
    books = {"a": BookInfo("Alpha", "S", 1.0), "b": BookInfo("Novella", "S", 1.5)}
    order = order_passages([p(1, "b", 1, 0), p(2, "a", 1, 0)], books)
    assert numbers(order) == [2, 1]
    assert order.positions[1].label() == "Novella, book 1.5 of S, chapter 1"


def test_books_with_no_series_are_not_ordered_against_each_other() -> None:
    order = order_passages([p(1, "y", 1, 0), p(2, "x", 1, 0)], BOOKS)
    assert order.note()
    assert "Loose One" in order.note() and "Loose Two" in order.note()
    assert "unknown" in order.note().lower()
    # grouped by book, never interleaved, in the order first met
    assert numbers(order) == [1, 2]


def test_a_series_book_and_a_loose_book_have_unknown_relative_order() -> None:
    order = order_passages([p(1, "x", 1, 0), p(2, "a", 1, 0), p(3, "b", 1, 0)], BOOKS)
    note = order.note()
    assert "Loose One" in note and "Alpha" in note and "Beta" in note
    assert numbers(order)[:2] == [2, 3]  # the ordered series group is kept together
    assert sorted(numbers(order)) == [1, 2, 3]


def test_two_different_series_are_not_ordered_against_each_other() -> None:
    order = order_passages([p(1, "z", 1, 0), p(2, "a", 1, 0)], BOOKS)
    assert order.note()
    assert "Other Series Book" in order.note() and "Alpha" in order.note()


def test_series_without_an_index_orders_nothing() -> None:
    books = {"a": BookInfo("Alpha", "S", None), "b": BookInfo("Beta", "S", 2.0)}
    order = order_passages([p(1, "a", 1, 0), p(2, "b", 1, 0)], books)
    assert order.note()
    assert order.positions[1].label() == "Alpha, chapter 1"  # no "book N of"


def test_an_index_without_a_series_orders_nothing() -> None:
    books = {"a": BookInfo("Alpha", None, 1.0), "b": BookInfo("Beta", None, 2.0)}
    order = order_passages([p(1, "b", 1, 0), p(2, "a", 1, 0)], books)
    assert order.note()


def test_series_names_match_ignoring_case_and_spacing() -> None:
    books = {"a": BookInfo("Alpha", "The Saga", 1.0), "b": BookInfo("Beta", " the saga ", 2.0)}
    order = order_passages([p(1, "b", 1, 0), p(2, "a", 1, 0)], books)
    assert numbers(order) == [2, 1] and order.note() == ""


def test_two_books_with_the_same_index_are_not_ordered() -> None:
    books = {"a": BookInfo("Alpha", "S", 1.0), "b": BookInfo("Beta", "S", 1.0)}
    order = order_passages([p(1, "b", 1, 0), p(2, "a", 1, 0)], books)
    assert order.note()


def test_order_never_comes_from_the_book_id_or_the_order_given() -> None:
    # Two loose books: the same facts in either input order give the same
    # claim that the order is unknown; nothing pretends one is first.
    one = order_passages([p(1, "x", 1, 0), p(2, "y", 1, 0)], BOOKS)
    two = order_passages([p(1, "y", 1, 0), p(2, "x", 1, 0)], BOOKS)
    assert one.note() and two.note()


def test_a_book_missing_from_the_rows_is_labelled_by_its_id_and_unordered() -> None:
    order = order_passages([p(1, "ghost", 4, 0), p(2, "x", 1, 0)], BOOKS)
    assert order.positions[1].label() == "ghost, chapter 4"
    assert order.note()


def test_no_passages_is_an_empty_order() -> None:
    order = order_passages([], BOOKS)
    assert order.ordered == [] and order.note() == ""


def test_an_unparseable_chunk_id_still_orders_by_chapter() -> None:
    odd = Passage(n=1, chunk_id="weird", book_id="x", chapter_seq=2, text="t")
    order = order_passages([odd, p(2, "x", 1, 0)], BOOKS)
    assert numbers(order) == [2, 1]
