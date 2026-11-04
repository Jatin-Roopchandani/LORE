"""lore.tenancy: ids that become directory names, and a user's own config."""
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from lore import tenancy
from lore.config import LoreConfig
from lore.tenancy import (
    LOCAL_USER, InvalidUserId, config_for, resolve_user, user_data_dir,
    validate_user_id,
)


def request() -> Request:
    return Request({"type": "http", "headers": [], "method": "GET", "path": "/"})


@pytest.mark.parametrize("good", [
    str(uuid.uuid4()), "alice", "a", "u-1", "bob_smith", "a1b2", "x" * 64])
def test_safe_ids_pass_through_unchanged(good):
    assert validate_user_id(good) == good


@pytest.mark.parametrize("bad", [
    "", ".", "..", "../x", "a/b", "a\\b", "/etc", "~", "a b", "a\x00b", "A",
    "Alice", "-a", "a-", "_a", "a.b", "x" * 65, "local", "é",
    "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", " " + str(uuid.uuid4()), None, 7,
    "..%2f", "a/../b"])
def test_unsafe_ids_are_refused(bad):
    with pytest.raises(InvalidUserId):
        validate_user_id(bad)


def test_user_data_dir_is_under_users_and_created(tmp_path):
    path = user_data_dir(tmp_path, "alice")
    assert path == tmp_path / "users" / "alice" and path.is_dir()
    assert user_data_dir(tmp_path, "alice") == path  # again: fine


def test_a_refused_id_creates_nothing(tmp_path):
    with pytest.raises(InvalidUserId):
        user_data_dir(tmp_path, "../escape")
    assert list(tmp_path.iterdir()) == []


def test_a_symlinked_user_dir_cannot_escape(tmp_path):
    (tmp_path / "users").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tmp_path / "users" / "evil").symlink_to(outside)
    with pytest.raises(InvalidUserId):
        user_data_dir(tmp_path, "evil")


def test_config_for_swaps_only_the_data_dir(tmp_path):
    base = LoreConfig(data_dir=tmp_path, extraction_provider="gemini")
    cfg = config_for(base, "alice")
    assert cfg.data_dir == tmp_path / "users" / "alice"
    assert cfg.sqlite_path == tmp_path / "users" / "alice" / "lore.db"
    assert cfg.extraction_provider == "gemini"
    assert cfg.model_dump(exclude={"data_dir"}) == base.model_dump(exclude={"data_dir"})
    assert base.data_dir == tmp_path  # the base is untouched


def test_the_local_user_is_the_base_library(tmp_path):
    base = LoreConfig(data_dir=tmp_path / "data")
    assert config_for(base, "local") is base
    assert not (tmp_path / "data").exists() and not (tmp_path / "users").exists()


def test_local_mode_resolves_the_local_user_whatever_the_request(monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    assert resolve_user(request()) == LOCAL_USER and LOCAL_USER.is_local


def test_multi_mode_without_auth_fails_closed(monkeypatch):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    with pytest.raises(HTTPException) as exc:
        resolve_user(request())
    assert exc.value.status_code == 401


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("0", False), ("", False), ("no", False)])
def test_multi_user_flag(monkeypatch, value, expected):
    monkeypatch.setenv("LORE_MULTI_USER", value)
    assert tenancy.multi_user_enabled() is expected
