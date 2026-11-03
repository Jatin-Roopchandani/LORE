"""LORE's editorial policy for answers that go beyond what a book states.

The text lives in `viewpoint.md`, packaged with the code. `lore.toml`
`[lore.answers] viewpoint = "<path>"` replaces it. The writer prompt includes
the text verbatim and the check prompt quotes the parts that apply, so both
read the same policy.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_VIEWPOINT = Path(__file__).with_name("viewpoint.md")


class ViewpointError(ValueError):
    """The configured viewpoint file is missing, unreadable or empty. Falling
    back to the default would hide a mistake in the config, so it is loud."""


def load_viewpoint(path: Path | str | None = None) -> str:
    """The policy text: the file at `path` (relative paths are read from the
    working directory, as `data_dir` is), or the packaged default."""
    source = Path(path) if path else DEFAULT_VIEWPOINT
    try:
        text = source.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ViewpointError(f"cannot read the viewpoint file {source}: {exc}") from exc
    if not text:
        raise ViewpointError(f"the viewpoint file {source} is empty")
    return text
