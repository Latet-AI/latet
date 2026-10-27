"""The bridge token (the Claude Code plugin's, for the daemon on this computer): new ones start with ``latet_brg_`` so
that Latet masks them by their shape; one made before stays as it is until it is replaced."""

import re
import stat
import sys

import pytest

from latet.storage import keys

NEW = re.compile(r"latet_brg_[A-Za-z0-9_-]{43}")
OLD = "Zq7wZ2k4NBX5pL3mD_r8S-v6Hj9Ta0Bc1Eo2Fi3Gu4J5"  # 43 characters and no prefix: what 0.9.10 and before made


@pytest.fixture(autouse=True)
def _no_environment_token(monkeypatch):
    monkeypatch.delenv("LATET_BRIDGE_TOKEN", raising=False)


def test_a_new_bridge_token_starts_with_latet_brg_and_is_43_url_safe_characters_more():
    tokens = {keys.new_bridge_token() for _ in range(50)}
    assert len(tokens) == 50 and all(NEW.fullmatch(token) for token in tokens)


def test_the_first_bridge_token_is_new_and_kept_private_and_the_same_after(tmp_path):
    first = keys.load_bridge_token(tmp_path)
    assert NEW.fullmatch(first)
    assert (tmp_path / "bridge.token").read_text("utf-8").strip() == first
    assert keys.load_bridge_token(tmp_path) == first
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "bridge.token").stat().st_mode) == 0o600


def test_a_bridge_token_made_before_the_prefix_is_kept_and_not_replaced(tmp_path):
    (tmp_path / "bridge.token").write_text(OLD + "\n", "utf-8")
    assert keys.load_bridge_token(tmp_path) == OLD
    assert (tmp_path / "bridge.token").read_text("utf-8").strip() == OLD  # no one is locked out by the upgrade


def test_a_bridge_token_from_the_environment_is_used_as_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", OLD)
    assert keys.load_bridge_token(tmp_path) == OLD
    assert not (tmp_path / "bridge.token").exists()
