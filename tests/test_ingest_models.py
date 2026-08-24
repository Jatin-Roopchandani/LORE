from pathlib import Path

from lore.ingest.models import (
    Chunk,
    IngestError,
    ParsedBook,
    ParsedChapter,
    book_id_for_file,
)


def test_book_id_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    a = tmp_path / "a.epub"
    b = tmp_path / "b.epub"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    c = tmp_path / "c.epub"
    c.write_bytes(b"different content")
    assert book_id_for_file(a) == book_id_for_file(b)
    assert book_id_for_file(a) != book_id_for_file(c)
    assert len(book_id_for_file(a)) == 16
    assert all(ch in "0123456789abcdef" for ch in book_id_for_file(a))


def test_models_are_frozen() -> None:
    chapter = ParsedChapter(seq=1, title="One", text="Hello.")
    book = ParsedBook(title="T", author=None, chapters=[chapter])
    chunk = Chunk(id="ab:1:0", book_id="ab", chapter_seq=1, scene_seq=0, seq=0, text="Hello.")
    for obj, attr in ((chapter, "title"), (book, "title"), (chunk, "text")):
        try:
            setattr(obj, attr, "changed")
            raise AssertionError("expected FrozenInstanceError")
        except AttributeError:
            pass


def test_ingest_error_is_exception() -> None:
    assert issubclass(IngestError, Exception)
