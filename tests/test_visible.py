"""The visibility predicate: what a reading position makes visible."""
import pytest

from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import open_db
from lore.ingest.registry import book_rows
from lore.query import visible
from lore.query.models import BookInfo
from lore.query.scope import resolve_scopes, union, world_books
from lore.query.visible import VisibleBooks, sees
from lore.reading_position import ReadingPosition, set_position
from tests.position_fixtures import HOLLOW, OTHER, build_position_library

INFOS = {"b1": BookInfo("One", "Saga", 1.0), "b2": BookInfo("Two", "Saga", 2.0),
         "b3": BookInfo("Three", "Saga", 3.0), "x": BookInfo("Standalone"),
         "y": BookInfo("Other series", "Elsewhere", 1.0), "d1": BookInfo("Dup A", "Dup", 1.0),
         "d2": BookInfo("Dup B", "Dup", 1.0)}
WORLD = frozenset(INFOS)


def pos(book, chapter, read=(), enabled=True):
    return ReadingPosition(1, book, chapter, tuple(read), enabled)


def made(book, chapter, read=()):
    got = visible.visible_books(WORLD, pos(book, chapter, read), INFOS)
    return got[0] if got else None


def test_the_same_book_is_visible_up_to_the_chapter_and_not_after():
    books = made("b2", 4)
    assert sees(books, "b2", 1) and sees(books, "b2", 4)
    assert not sees(books, "b2", 5)


def test_an_earlier_book_of_the_series_counts_as_fully_read_and_a_later_one_does_not():
    books = made("b2", 1)
    assert sees(books, "b1", 99)
    assert not sees(books, "b3", 1)
    assert set(books) == {"b1", "b2"}


def test_unknown_order_uses_the_read_toggle_and_never_guesses():
    assert not sees(made("b2", 3), "x", 1)  # no series: unknown, not marked read
    assert not sees(made("b2", 3), "y", 1)  # another series: unknown
    marked = made("b2", 3, read=["x", "y"])
    assert sees(marked, "x", 50) and sees(marked, "y", 50)
    # two books of one series with the same index are of unknown order too
    assert not sees(made("d1", 2), "d2", 1)
    assert sees(made("d1", 2, read=["d2"]), "d2", 1)


def test_a_toggle_never_reveals_a_book_known_to_come_later():
    assert not sees(made("b1", 2, read=["b2", "b3"]), "b2", 1)


def test_a_book_with_no_known_place_sees_only_itself_and_what_was_marked():
    books = made("x", 2, read=["b1"])
    assert set(books) == {"x", "b1"} and sees(books, "b1", 99)
    assert not sees(books, "b2", 1)  # x is unordered against the series: unknown


def test_disabled_or_unplaced_positions_do_not_apply():
    assert made("b1", 1) is not None
    assert visible.visible_books(WORLD, pos("b1", 1, enabled=False), INFOS) is None
    assert visible.visible_books(WORLD, pos(None, 1), INFOS) is None
    assert visible.visible_books(WORLD, pos("elsewhere", 1), INFOS) is None


def test_plain_sets_and_none_see_whole_books():
    assert sees(None, "any", 10**6)
    assert sees(frozenset({"a"}), "a", 10**6) and not sees(frozenset({"a"}), "b", 1)
    assert not VisibleBooks({"a"}, {"a": 2}).sees("a", 3)
    assert VisibleBooks({"a"}, {"zzz": 2}).caps == {}  # a cap of a book not in it is dropped


def test_restrict_and_merge_keep_the_caps():
    books = VisibleBooks({"a", "b"}, {"b": 2})
    only = visible.restrict(books, ["b", "c"])
    assert set(only) == {"b"} and only.caps == {"b": 2}
    merged = visible.merge([books, frozenset({"z"})])
    assert set(merged) == {"a", "b", "z"} and merged.caps == {"b": 2}
    assert type(visible.merge([frozenset({"a"}), frozenset({"b"})])) is frozenset


def test_the_cypher_clause_and_params_say_the_same_thing():
    books = VisibleBooks({"a", "b"}, {"b": 2})
    clause = visible.book_clause(books, "r")
    assert clause == (" AND r.book_id IN $books"
                      " AND NOT (r.book_id = $capb0 AND r.chapter_seq > $capc0)")
    assert visible.book_params(books) == {"books": ["a", "b"], "capb0": "b", "capc0": 2}
    assert visible.book_clause(frozenset({"a"})) == " AND r.book_id IN $books"
    assert visible.book_params(frozenset({"a"})) == {"books": ["a"]}
    assert visible.book_clause(None) == "" and visible.book_params(None) == {}


# --- through the scope -----------------------------------------------------------------

@pytest.fixture
def library(tmp_path):
    config = build_position_library(tmp_path)
    conn = open_db(config.sqlite_path)
    world = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
    yield config, conn, world
    conn.close()


def test_scopes_are_unchanged_until_a_position_is_on(library):
    _config, conn, world = library
    scope = resolve_scopes(conn, HOLLOW)[0]
    assert scope.books == {"hb1", "hb2", "hb3"} and scope.position is None
    assert not isinstance(scope.books, VisibleBooks)
    set_position(conn, ReadingPosition(world, "hb1", 2, (), False))
    assert resolve_scopes(conn, HOLLOW)[0].position is None


def test_a_position_narrows_the_world_scope_and_spoilers_bypasses_it(library):
    _config, conn, world = library
    set_position(conn, ReadingPosition(world, "hb2", 2, (), True))
    scope = resolve_scopes(conn, HOLLOW)[0]
    assert set(scope.books) == {"hb1", "hb2"} and scope.books.caps == {"hb2": 2}
    assert scope.sees("hb1", 3) and scope.sees("hb2", 2) and not scope.sees("hb2", 3)
    assert not scope.allows("hb3")
    assert (scope.position.title, scope.position.chapter) == ("Hollow Keep", 2)
    assert "up to chapter 2 of Hollow Keep" in scope.position.note()
    bypass = resolve_scopes(conn, HOLLOW, spoilers=True)[0]
    assert bypass.books == {"hb1", "hb2", "hb3"} and bypass.position is None


def test_another_worlds_scope_is_not_touched(library):
    _config, conn, world = library
    set_position(conn, ReadingPosition(world, "hb1", 1, (), True))
    scopes = resolve_scopes(conn, None)  # all worlds
    by_world = {s.world: s for s in scopes}
    assert by_world[HOLLOW].position is not None
    assert by_world[OTHER].position is None and by_world[OTHER].books == {"ob1"}
    both = union(scopes)
    assert both.caps == {"hb1": 1} and set(both) == {"hb1", "ob1"}


def test_a_one_world_library_is_still_narrowed(tmp_path):
    config = build_position_library(tmp_path, one_world=True)
    conn = open_db(config.sqlite_path)
    try:
        world = conn.execute("SELECT id FROM worlds").fetchone()[0]
        assert resolve_scopes(conn, None)[0].books is None
        set_position(conn, ReadingPosition(world, "hb1", 2, (), True))
        scope = resolve_scopes(conn, None)[0]
        assert scope.books is not None and scope.books.caps == {"hb1": 2}
    finally:
        conn.close()


def test_world_books_applies_the_position_with_or_without_a_world(library):
    _config, conn, world = library
    assert world_books(conn, None) is None
    set_position(conn, ReadingPosition(world, "hb1", 2, (), True))
    assert world_books(conn, HOLLOW).caps == {"hb1": 2}
    assert world_books(conn, None).caps == {"hb1": 2}
    assert world_books(conn, None, spoilers=True) is None
    assert world_books(conn, HOLLOW, spoilers=True) == {"hb1", "hb2", "hb3"}


# --- entities: who has been met, under which name -----------------------------------------

@pytest.fixture
def graph(library):
    config, conn, world = library
    kuzu = KuzuStore(config.kuzu_dir / "graph", read_only=True)
    yield kuzu, conn, world
    kuzu.close()


def names(graph, book, chapter, ids, read=()):
    kuzu, conn, world = graph
    got = visible.visible_books(frozenset({"hb1", "hb2", "hb3"}),
                                ReadingPosition(world, book, chapter, tuple(read), True),
                                book_rows(conn))
    return visible.visible_names(kuzu, got[0], ids, book_rows(conn))


def test_an_entity_is_shown_under_the_earliest_name_the_reader_met(graph):
    # Chapter 2 of the first book: only "Old Tam" has been met; "Ren Ashby" would spoil.
    assert names(graph, "hb1", 2, ["h_ren"]) == {"h_ren": ["Old Tam"]}
    # Chapter 3 reveals the canonical name, but the first-met name still leads.
    assert names(graph, "hb1", 3, ["h_ren"]) == {"h_ren": ["Old Tam", "Ren Ashby"]}
    # In the next book only the canonical name is mentioned, but the first book is read.
    assert names(graph, "hb2", 1, ["h_ren"])["h_ren"][0] == "Old Tam"


def test_an_entity_first_met_later_has_not_been_met(graph):
    assert "h_dorn" not in names(graph, "hb1", 2, ["h_dorn", "h_mira"])
    assert "h_dorn" in names(graph, "hb1", 3, ["h_dorn", "h_mira"])
    assert names(graph, "hb1", 0, ["h_mira"]) == {}
    assert "w_pip" not in names(graph, "hb1", 3, ["w_pip"])
    assert "w_pip" in names(graph, "hb1", 3, ["w_pip"], read=["hb3"])


def test_without_a_position_names_are_left_alone(graph):
    kuzu, conn, _world = graph
    assert visible.visible_names(kuzu, None, ["h_ren"]) == {"h_ren": []}
    assert visible.display_name({"h_ren": []}, "h_ren", "Ren Ashby") == "Ren Ashby"
    assert visible.met(kuzu, frozenset({"hb1"}), ["h_ren", "nobody"]) == {"h_ren", "nobody"}


# --- names written into beats and codex lines ------------------------------------------------

def test_respell_replaces_whole_words_in_any_case_and_longest_first():
    pairs = [("Ren", "Tam"), ("Ren Ashby", "Old Tam")]
    assert visible.respell("Ren Ashby met ren, not Renown.", pairs) == "Old Tam met Tam, not Renown."
    assert visible.respell("same", [("Same", "same")]) == "same"


def test_beats_and_codex_lines_use_the_name_the_reader_has_met(graph):
    from lore.beats.store import Beat
    from lore.query.models import CodexLine
    kuzu, conn, world = graph
    infos = book_rows(conn)
    got = visible.visible_books(frozenset({"hb1", "hb2", "hb3"}),
                                ReadingPosition(world, "hb1", 2, (), True), infos)[0]
    beat = Beat(id="b", book_id="hb1", chapter_seq=1, chunk_id="hb1:1:0",
                entities=("Ren Ashby", "Mira Cole"), kind="action",
                text="Ren Ashby greets Mira Cole.", quote="q", entity_ids=("h_ren", "h_mira"))
    (shown,) = visible.shown_beats(kuzu, got, [beat], infos)
    assert shown.entities == ("Old Tam", "Mira Cole") and shown.text == "Old Tam greets Mira Cole."
    assert visible.shown_beats(kuzu, None, [beat], infos) == [beat]
    line = CodexLine(chunk_id="hb1:1:0", book_id="hb1", chapter_seq=1, subject="Ren Ashby",
                     aspect="nature", text="Ren Ashby is wary.", entity_id="h_ren")
    (shown_line,) = visible.shown_lines(kuzu, got, [line], infos)
    assert (shown_line.subject, shown_line.text) == ("Old Tam", "Old Tam is wary.")
    assert visible.shown_lines(kuzu, None, [line], infos) == [line]
