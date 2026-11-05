"""Every request profile id Latet's profiles can produce is one an exception can name and the history keeps (P2-7
review I-2): the ids are one closed set, ``profile_ids.PROFILE_IDS``, that ``config.RequestException`` and the
history's summary both check, instead of a grammar that refused ``anthropic.count_tokens.1``, the Gemini ids and
Vertex's. Saving settings writes no default a 0.10 Latet would refuse (I-4)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from latet.config import Config, Destination, ExceptionComponent, RequestException, load_config, save_config
from latet.gateway.profile_ids import PROFILE_IDS
from latet.storage.exchanges import safe_summary

_OBSERVED = Path(__file__).resolve().parents[1] / "data" / "request-coverage" / "profiles.json"


def _observed() -> set[str]:
    text = _OBSERVED.read_text(encoding="utf-8")
    return set(re.findall(r'"(?:profile|id)": "([a-z0-9-]+(?:\.[A-Za-z0-9_-]+){1,6})"', text))


def _exception(profile: str) -> RequestException:
    return RequestException(
        destination="anthropic",
        profile=profile,
        components=[ExceptionComponent(kind="header", name="x-company-team")],
        reason="custom-header",
    )


def test_every_profile_the_observed_calls_were_admitted_by_is_in_the_set():
    observed = _observed()
    assert len(observed) > 50
    assert observed <= PROFILE_IDS, sorted(observed - PROFILE_IDS)


def test_the_ids_the_review_found_refused_are_in_the_set():
    for profile in (
        "anthropic.count_tokens.1",
        "gemini.v1beta.generateContent.1",
        "gemini.v1beta.streamGenerateContent.1",
        "gemini.v1beta.countTokens.1",
        "gemini.v1beta.embedContent.1",
        "vertex.anthropic.rawPredict.1",
        "vertex.anthropic.streamRawPredict.1",
        "company.vertex.rawPredict.1",
        "azure-openai.responses.1",
        "foundry.embeddings.1",
    ):
        assert profile in PROFILE_IDS, profile


@pytest.mark.parametrize("profile", sorted(PROFILE_IDS))
def test_an_exception_may_name_every_profile_id(profile):
    assert _exception(profile).profile == profile


@pytest.mark.parametrize("profile", sorted(PROFILE_IDS))
def test_the_history_keeps_every_profile_id_and_its_websocket_form(profile):
    assert safe_summary({"profile": profile})["profile"] == profile
    assert safe_summary({"profile": profile + ".ws-envelope-1"})["profile"] == profile + ".ws-envelope-1"


@pytest.mark.parametrize(
    "profile", ["anthropic.messages.2", "openai.files.1", "gemini.v2.generateContent.1", "anthropic.messages.1.x"]
)
def test_an_id_no_profile_produces_is_refused_and_not_kept(profile):
    with pytest.raises(ValueError, match="request profile"):
        _exception(profile)
    assert safe_summary({"profile": profile})["profile"] is None


def test_an_exception_for_a_websocket_profile_is_refused():
    """No exception applies to a WebSocket in 0.11.0 (F-2): its handshake's ids are the history's alone."""
    with pytest.raises(ValueError, match="request profile"):
        _exception("openai.responses.1.ws-envelope-1")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    from latet.cli import admin

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)
    return tmp_path


def test_latet_config_add_accepts_a_gemini_exception(isolated):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    item = json.dumps(
        {
            "destination": "gemini",
            "profile": "gemini.v1beta.generateContent.1",
            "components": [{"kind": "header", "name": "x-company-team"}],
            "reason": "custom-header",
        }
    )
    result = CliRunner().invoke(app, ["config", "add", "request_exceptions", item])
    assert result.exit_code in (0, 3), result.output  # 3: it waits for your approval, as a loosening does
    [row] = load_config().request_exceptions
    assert row.profile == "gemini.v1beta.generateContent.1"


def test_saved_settings_write_a_destinations_request_profile_only_when_chosen(tmp_path):
    """Review I-4: 0.10.4's Destination forbids fields it does not know, so the default `request_profile: standard`
    is never written; a profile someone chose is."""
    path = tmp_path / "config.yaml"
    config = Config(
        upstreams=[
            Destination(name="llm-acme-com", url="https://llm.acme.com"),
            Destination(
                name="azure-acme", url="https://acme.openai.azure.com/openai", request_profile="azure-openai-1"
            ),
        ]
    )
    save_config(config, path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    plain, azure = saved["upstreams"]
    assert "request_profile" not in plain
    assert azure["request_profile"] == "azure-openai-1"
    assert load_config(path).upstreams == config.upstreams


def test_the_approved_copy_writes_no_default_request_profile_either(tmp_path):
    from latet.state.local import REVIEWED_FILE, LocalSettings

    config = Config(upstreams=[Destination(name="llm-acme-com", url="https://llm.acme.com")])
    settings = LocalSettings(tmp_path / "config.yaml", tmp_path / "data")
    settings.save_reviewed(config)
    reviewed = json.loads((tmp_path / "data" / REVIEWED_FILE).read_text(encoding="utf-8"))
    assert "request_profile" not in reviewed["upstreams"][0]
    assert settings.reviewed() == config
