import tomllib
from pathlib import Path

import lore


def test_version_matches_pyproject() -> None:
    # One version, set in pyproject.toml; lore.__version__ must agree with it.
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert lore.__version__ == pyproject["project"]["version"]
