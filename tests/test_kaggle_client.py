from pathlib import Path
from unittest.mock import patch
import subprocess

import pytest

from lore.config import KaggleConfig
from lore.kaggle_api.client import KaggleClient, KaggleRunError


def _cfg() -> KaggleConfig:
    return KaggleConfig(username="testuser", poll_interval_s=0, poll_timeout_s=1)


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def test_push_kernel_invokes_cli(tmp_path: Path) -> None:
    client = KaggleClient(_cfg())
    with patch("lore.kaggle_api.client.subprocess.run",
               return_value=FakeCompleted()) as run:
        client.push_kernel(tmp_path)
    args = run.call_args.args[0]
    assert args[:3] == ["kaggle", "kernels", "push"]
    assert str(tmp_path) in args


def test_kernel_status_parses_complete() -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "complete"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        assert client.kernel_status() == "complete"


def test_kernel_status_parses_error() -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "error"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        assert client.kernel_status() == "error"


def test_run_and_wait_raises_on_error(tmp_path: Path) -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "error"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        with pytest.raises(KaggleRunError):
            client.run_and_wait(tmp_path, tmp_path / "out")


def test_authenticated_checks_credentials_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = KaggleClient(_cfg())
    assert client.authenticated() is False
    (tmp_path / ".kaggle").mkdir()
    (tmp_path / ".kaggle" / "kaggle.json").write_text("{}")
    assert client.authenticated() is True


def test_push_kernel_raises_on_timeout(tmp_path: Path) -> None:
    client = KaggleClient(_cfg())
    with patch("lore.kaggle_api.client.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=["kaggle"], timeout=300)):
        with pytest.raises(KaggleRunError):
            client.push_kernel(tmp_path)
