"""The upload checks (lore/ingest/upload_check.py): every guard on an untrusted
EPUB or PDF, with the bad files built here. No network."""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pymupdf
import pytest

from lore.ingest import upload_check as uc
from lore.ingest.upload_check import Limits, Refused, check_book, sniff
from tests.fixtures import make_epub, make_pdf

CONTAINER = (b'<?xml version="1.0"?><container version="1.0" '
             b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             b'</rootfiles></container>')


def good_epub(tmp_path: Path, name: str = "good.epub") -> Path:
    body = "<p>" + " ".join(f"Sentence {i} of the story." for i in range(60)) + "</p>"
    return make_epub(tmp_path / name, "A Good Book", "An Author",
                     [("One", body), ("Two", body)])


def raw_zip(path: Path, entries: list[tuple], *, mimetype: bool = True) -> Path:
    """entries: (name, data) or (ZipInfo, data); a valid mimetype and
    container come first unless `mimetype` is False."""
    with zipfile.ZipFile(path, "w") as z:
        if mimetype:
            z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", CONTAINER, zipfile.ZIP_DEFLATED)
        for name, data in entries:
            z.writestr(name, data, zipfile.ZIP_DEFLATED)
    return path


def code_of(path: Path, kind: str, limits: Limits | None = None) -> str:
    with pytest.raises(Refused) as caught:
        check_book(path, kind, limits or Limits())
    return caught.value.code


# --- sniffing -------------------------------------------------------------

def test_sniff_by_magic_bytes():
    assert sniff(b"PK\x03\x04rest") == "epub"
    assert sniff(b"%PDF-1.7\n") == "pdf"
    assert sniff(b"<html>") is None
    assert sniff(b"") is None
    assert sniff(b" %PDF-1.7") is None      # the header must be at the start
    assert sniff(b"PK\x05\x06") is None      # an empty zip is not a book


# --- EPUB -------------------------------------------------------------------

def test_a_real_epub_passes(tmp_path):
    check_book(good_epub(tmp_path), "epub", Limits())


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.epub"
    p.write_bytes(b"PK\x03\x04" + b"garbage" * 100)
    assert code_of(p, "epub") == "not_a_book"


def test_a_zip_with_no_mimetype_entry(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [("a.txt", b"hello")], mimetype=False)
    assert code_of(p, "epub") == "not_a_book"


def test_a_zip_with_the_wrong_mimetype(tmp_path):
    p = tmp_path / "x.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/zip")
        z.writestr("META-INF/container.xml", CONTAINER)
    assert code_of(p, "epub") == "not_a_book"


def test_a_zip_with_no_container(tmp_path):
    p = tmp_path / "x.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    assert code_of(p, "epub") == "not_a_book"


def test_zip_bomb_high_ratio(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [("OEBPS/big.xhtml", b"\0" * (20 * 1024 * 1024))])
    assert code_of(p, "epub") == "unsafe_file"


def test_zip_bomb_too_many_entries(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [(f"OEBPS/{i}.txt", b"x") for i in range(60)])
    assert code_of(p, "epub", Limits(max_entries=50)) == "unsafe_file"


def test_zip_bomb_too_large_in_total(tmp_path):
    import os
    entries = [(f"OEBPS/{i}.bin", os.urandom(300_000)) for i in range(4)]
    p = raw_zip(tmp_path / "x.epub", entries)
    assert code_of(p, "epub", Limits(max_uncompressed=1_000_000)) == "unsafe_file"


def _patch_sizes(path: Path, name: str, size: int) -> None:
    """Rewrite the uncompressed size `name` claims, in its local and central headers."""
    data = bytearray(path.read_bytes())
    raw = name.encode()
    i = data.find(b"PK\x03\x04")
    while i != -1:
        n = int.from_bytes(data[i + 26:i + 28], "little")
        if data[i + 30:i + 30 + n] == raw:
            data[i + 22:i + 26] = size.to_bytes(4, "little")
        i = data.find(b"PK\x03\x04", i + 4)
    i = data.find(b"PK\x01\x02")
    while i != -1:
        n = int.from_bytes(data[i + 28:i + 30], "little")
        if data[i + 46:i + 46 + n] == raw:
            data[i + 24:i + 28] = size.to_bytes(4, "little")
        i = data.find(b"PK\x01\x02", i + 4)
    path.write_bytes(bytes(data))


def test_a_header_that_lies_about_the_size_is_caught(tmp_path):
    # The headers say 10 bytes; the entry inflates to far more. Reading stops
    # at the claimed size and the CRC no longer matches.
    p = raw_zip(tmp_path / "x.epub", [("OEBPS/a.xhtml", b"A" * 100_000)])
    _patch_sizes(p, "OEBPS/a.xhtml", 10)
    assert code_of(p, "epub") == "not_a_book"


@pytest.mark.parametrize("name", [
    "../evil.xhtml", "OEBPS/../../evil.xhtml", "/etc/evil", "\\windows\\evil",
    "OEBPS\\..\\evil", "C:/evil", "..", "OEBPS/./a.xhtml",
])
def test_path_traversal_and_absolute_names(tmp_path, name):
    p = raw_zip(tmp_path / "x.epub", [(zipfile.ZipInfo(name), b"ok")])
    assert code_of(p, "epub") == "unsafe_file"


def test_a_nul_in_an_entry_name(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [("OEBPS/aXb.xhtml", b"ok")])
    p.write_bytes(p.read_bytes().replace(b"OEBPS/aXb.xhtml", b"OEBPS/a\0b.xhtml"))
    assert code_of(p, "epub") == "unsafe_file"


def test_a_symlink_entry(tmp_path):
    info = zipfile.ZipInfo("OEBPS/link.xhtml")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    p = raw_zip(tmp_path / "x.epub", [(info, b"/etc/passwd")])
    assert code_of(p, "epub") == "unsafe_file"


def test_duplicate_entry_names(tmp_path):
    import warnings
    p = tmp_path / "x.epub"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/a.xhtml", b"one")
            z.writestr("OEBPS/a.xhtml", b"two")
    assert code_of(p, "epub") == "unsafe_file"


def test_an_entry_flagged_encrypted(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [("OEBPS/a.xhtml", b"secret text")])
    data = bytearray(p.read_bytes())
    # set bit 0 of the general-purpose flags in every local and central header
    i = data.find(b"PK\x03\x04")
    while i != -1:
        data[i + 6] |= 1
        i = data.find(b"PK\x03\x04", i + 4)
    i = data.find(b"PK\x01\x02")
    while i != -1:
        data[i + 8] |= 1
        i = data.find(b"PK\x01\x02", i + 4)
    p.write_bytes(bytes(data))
    assert code_of(p, "epub") == "encrypted"


def test_drm_encryption_xml_is_encrypted(tmp_path):
    enc = (b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
           b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
           b'<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
           b'<enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>'
           b'</enc:EncryptedData></encryption>')
    p = raw_zip(tmp_path / "x.epub", [("META-INF/encryption.xml", enc)])
    assert code_of(p, "epub") == "encrypted"


def test_adobe_rights_file_is_encrypted(tmp_path):
    p = raw_zip(tmp_path / "x.epub", [("META-INF/rights.xml", b"<rights/>")])
    assert code_of(p, "epub") == "encrypted"


def test_font_obfuscation_alone_is_not_encryption(tmp_path):
    src = good_epub(tmp_path)
    enc = (b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
           b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
           b'<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
           b'<enc:CipherData><enc:CipherReference URI="EPUB/font.otf"/></enc:CipherData>'
           b'</enc:EncryptedData></encryption>')
    out = tmp_path / "fonts.epub"
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out, "w") as b:
        for info in a.infolist():
            b.writestr(info, a.read(info))
        b.writestr("META-INF/encryption.xml", enc)
    check_book(out, "epub", Limits())


def test_an_xml_entity_declaration_is_refused(tmp_path):
    opf = (b'<?xml version="1.0"?><!DOCTYPE package [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           b'<package/>')
    p = raw_zip(tmp_path / "x.epub", [("OEBPS/content.opf", opf)])
    assert code_of(p, "epub") == "unsafe_file"


def test_an_unusual_compression_method_is_refused(tmp_path):
    p = tmp_path / "x.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/a.xhtml", b"x" * 1000, zipfile.ZIP_BZIP2)
    assert code_of(p, "epub") == "not_a_book"


def test_the_checks_never_write_entry_paths(tmp_path, monkeypatch):
    # A traversal entry is refused before anything opens it, and nothing is
    # written anywhere: the directory holds just the file afterwards.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    p = good_epub(work)
    check_book(p, "epub", Limits())
    assert sorted(x.name for x in work.iterdir()) == ["good.epub"]


# --- PDF --------------------------------------------------------------------

def test_a_real_pdf_passes(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", ["word " * 200, "more " * 200])
    check_book(p, "pdf", Limits())


def test_a_pdf_header_that_is_not_a_pdf(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    assert code_of(p, "pdf") == "not_a_book"


def test_an_encrypted_pdf(tmp_path):
    p = tmp_path / "e.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret " * 50)
    doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()
    assert code_of(p, "pdf") == "encrypted"


def test_too_many_pages(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", ["page"] * 6)
    assert code_of(p, "pdf", Limits(max_pages=5)) == "too_many_pages"
    check_book(p, "pdf", Limits(max_pages=6))


def test_kind_must_be_known(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hello")
    assert code_of(p, "txt") == "not_a_book"


def test_limits_defaults():
    lim = Limits()
    assert lim.max_entries == uc.MAX_ENTRIES == 5000
    assert lim.max_uncompressed == uc.MAX_UNCOMPRESSED == 300 * 1024 * 1024
    assert lim.max_ratio == uc.MAX_RATIO == 100
    assert lim.max_pages == uc.MAX_PAGES == 5000
