"""Checks on an uploaded book before LORE parses it (release step 5).

The file comes from the internet, so nothing about it is trusted: not its
name, not its Content-Type, not the sizes its zip headers claim. `sniff`
names the type from the magic bytes; `check_book` then refuses anything that
is not plainly an EPUB or a PDF, or that could hurt the server:

EPUB (a zip):
- it must open as a zip, hold a `mimetype` entry saying application/epub+zip
  and a META-INF/container.xml;
- at most `max_entries` entries, only stored or deflated ones;
- no entry name that is absolute, has a drive letter, a backslash, a NUL, or
  a `.`/`..` component, no duplicate names, no symlink entries;
- no entry flagged encrypted, no DRM (META-INF/rights.xml, or an
  encryption.xml that encrypts anything beyond IDPF/Adobe font obfuscation);
- every entry is actually inflated here, in memory, counting bytes: an entry
  whose size/compressed size ratio passes `max_ratio` (above 1 MB), or a
  total past `max_uncompressed`, is refused; zipfile stops at the size the
  header claims and checks the CRC, so a header that lies fails too;
- no XML entity declarations (`<!ENTITY`) in any entry: the parsers do not
  expand external entities, and this keeps it that way whatever parses them.
Nothing is ever extracted to disk; the EPUB parser (ebooklib) reads entries
into memory and writes nothing.

PDF: a `%PDF-` header, opens with the PDF reader (PyMuPDF), not encrypted or
password-protected, at least one and at most `max_pages` pages.

A refusal is `Refused(code, message)`: the code is one of the upload API's
friendly codes (`not_a_book`, `unsafe_file`, `encrypted`, `too_many_pages`);
the message never echoes the file's own text.
"""
from __future__ import annotations

import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 300 * 1024 * 1024
MAX_RATIO = 100
RATIO_FLOOR = 1024 * 1024  # small entries may compress well; only big ones count
MAX_PAGES = 5000
_CHUNK = 64 * 1024
_EPUB_MIMETYPE = b"application/epub+zip"
# Font obfuscation (IDPF and Adobe) is not DRM: the text is readable.
_FONT_OBFUSCATION = frozenset({
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
})
_ENTITY = b"<!ENTITY"
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class Limits:
    max_entries: int = MAX_ENTRIES
    max_uncompressed: int = MAX_UNCOMPRESSED
    max_ratio: int = MAX_RATIO
    max_pages: int = MAX_PAGES


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def sniff(head: bytes) -> str | None:
    """'epub' or 'pdf' from the first bytes of the file, else None."""
    if head.startswith(b"PK\x03\x04"):
        return "epub"
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


def check_book(path: Path, kind: str, limits: Limits) -> None:
    if kind == "epub":
        _check_epub(path, limits)
    elif kind == "pdf":
        _check_pdf(path, limits)
    else:
        raise Refused("not_a_book", "only EPUB and PDF books can be added")


# --- EPUB -------------------------------------------------------------------

def _unsafe(why: str) -> Refused:
    return Refused("unsafe_file", f"this EPUB was refused: {why}")


def _not_epub(why: str) -> Refused:
    return Refused("not_a_book", f"this is not a readable EPUB: {why}")


def _bad_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return True
    if name.startswith("/") or _DRIVE.match(name):
        return True
    parts = name.rstrip("/").split("/")
    return any(p in ("", ".", "..") for p in parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _check_epub(path: Path, limits: Limits) -> None:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise _not_epub("the file is not a valid zip") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise _unsafe(f"it has more than {limits.max_entries} entries")
        names: set[str] = set()
        claimed = 0
        for info in infos:
            name = info.filename
            # zipfile cuts a name at a NUL; the name as stored is orig_filename.
            if _bad_name(info.orig_filename) or _bad_name(name) or posixpath.normpath(name) != name.rstrip("/"):
                raise _unsafe("an entry has an unsafe name")
            if name in names:
                raise _unsafe("two entries share a name")
            names.add(name)
            if _is_symlink(info):
                raise _unsafe("an entry is a symbolic link")
            if info.flag_bits & 0x1:
                raise Refused("encrypted", "this EPUB is encrypted (DRM)")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise _not_epub("an entry uses an unsupported compression")
            claimed += info.file_size
            if claimed > limits.max_uncompressed:
                raise _unsafe("it unpacks to too much data")
            if (info.file_size > RATIO_FLOOR and
                    info.file_size > limits.max_ratio * max(1, info.compress_size)):
                raise _unsafe("an entry is compressed suspiciously well")
        if "mimetype" not in names:
            raise _not_epub("it has no mimetype entry")
        if "META-INF/container.xml" not in names:
            raise _not_epub("it has no META-INF/container.xml")
        if "META-INF/rights.xml" in names:
            raise Refused("encrypted", "this EPUB is protected by DRM")
        total = 0
        kept: dict[str, bytes] = {}
        for info in infos:
            if info.is_dir():
                continue
            keep = info.filename in ("mimetype", "META-INF/encryption.xml")
            size, tail, body = 0, b"", []
            try:
                with archive.open(info) as entry:
                    while chunk := entry.read(_CHUNK):
                        size += len(chunk)
                        total += len(chunk)
                        if total > limits.max_uncompressed:
                            raise _unsafe("it unpacks to too much data")
                        if size > RATIO_FLOOR and size > limits.max_ratio * max(1, info.compress_size):
                            raise _unsafe("an entry is compressed suspiciously well")
                        if _ENTITY in tail + chunk:
                            raise _unsafe("an entry declares XML entities")
                        tail = chunk[-(len(_ENTITY) - 1):]
                        if keep and size <= _CHUNK * 4:
                            body.append(chunk)
            except Refused:
                raise
            except (zipfile.BadZipFile, OSError, ValueError, EOFError,
                    NotImplementedError, RuntimeError):
                raise _not_epub("an entry is damaged") from None
            if keep:
                kept[info.filename] = b"".join(body)
        if kept.get("mimetype", b"").strip() != _EPUB_MIMETYPE:
            raise _not_epub("its mimetype is not application/epub+zip")
        if "META-INF/encryption.xml" in kept:
            _check_encryption(kept["META-INF/encryption.xml"])


def _check_encryption(xml: bytes) -> None:
    """Refuse unless every encrypted resource is only an obfuscated font."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise Refused("encrypted", "this EPUB is encrypted (DRM)") from None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "EncryptionMethod":
            if el.get("Algorithm") not in _FONT_OBFUSCATION:
                raise Refused("encrypted", "this EPUB is encrypted (DRM)")


# --- PDF --------------------------------------------------------------------

def _check_pdf(path: Path, limits: Limits) -> None:
    import pymupdf

    with path.open("rb") as f:
        if not f.read(5) == b"%PDF-":
            raise Refused("not_a_book", "this is not a PDF")
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception:  # noqa: BLE001 -- any reader failure is "not a book"
        raise Refused("not_a_book", "this PDF cannot be read") from None
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise Refused("encrypted",
                          "this PDF is password-protected; remove the password and try again")
        pages = doc.page_count
    except Refused:
        raise
    except Exception:  # noqa: BLE001
        raise Refused("not_a_book", "this PDF cannot be read") from None
    finally:
        doc.close()
    if pages < 1:
        raise Refused("not_a_book", "this PDF has no pages")
    if pages > limits.max_pages:
        raise Refused("too_many_pages", f"this PDF has more than {limits.max_pages} pages")
