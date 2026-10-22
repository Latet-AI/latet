"""Destinations: where agents' requests go after Latet, their names, and the changes that wait for you."""

import pytest
from pydantic import ValidationError

from latet.config import Config, Destination
from latet.destinations import add_destination, name_for
from latet.gateway.routes import destination_upstream
from latet.protection import destination_changes, loosened


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://llm.acme.com/anthropic/", "https://llm.acme.com/anthropic"),
        ("http://127.0.0.1:4000", "http://127.0.0.1:4000"),
        ("http://[::1]:4000", "http://[::1]:4000"),
        ("http://10.0.0.5:4000/v1", "http://10.0.0.5:4000/v1"),
        ("http://llm:4000", "http://llm:4000"),
        ("http://gw.lan", "http://gw.lan"),
    ],
)
def test_an_address_is_https_outside_the_local_network(url, kept):
    assert Destination(name="acme", url=url).url == kept


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.acme.com",
        "https://user:secret@llm.acme.com",
        "https://llm.acme.com?key=1",
        "https://llm.acme.com#f",
        "ftp://x",
        "http://evil.example\\@127.0.0.1:5283",  # Python and Node read two different hosts
        "https://llm.acme.com /v1",
        "https://llm.acme.com\t/v1",
        "http://[::1",
    ],
)
def test_an_address_holds_no_credentials_nor_query(url):
    with pytest.raises(ValidationError):
        Destination(name="acme", url=url)


@pytest.mark.parametrize("name", ["openai", "bedrock", "vertex", "_latet", "Acme", "-acme", "a" * 64])
def test_a_name_is_a_route_of_its_own(name):
    with pytest.raises(ValidationError):
        Destination(name=name, url="https://llm.acme.com")


def test_two_destinations_cannot_share_a_name():
    with pytest.raises(ValidationError):
        Config(upstreams=[{"name": "acme", "url": "https://a.acme.com"}, {"name": "acme", "url": "https://b.acme.com"}])


def test_a_destination_is_a_route():
    upstream = destination_upstream(Destination(name="acme", url="https://llm.acme.com/v1"))
    assert upstream.prefix == "/acme"
    assert upstream.target("/acme/chat/completions", "") == "https://llm.acme.com/v1/chat/completions"


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://llm.acme.com", "llm-acme-com"),
        ("https://acme.openai.azure.com/openai/v1", "azure-acme"),
        ("https://acme.cognitiveservices.azure.com/openai/v1", "azure-acme"),
        ("https://acme.services.ai.azure.com/anthropic", "foundry-acme"),
        ("http://10.0.0.5:4000", "10-0-0-5-4000"),
        ("https://Gateway.ACME.com/", "gateway-acme-com"),
    ],
)
def test_a_name_comes_from_the_address(url, name):
    assert name_for(url) == name


def test_the_same_address_keeps_its_destination():
    config, name, added = add_destination(Config(), "https://llm.acme.com", "claude")
    assert (name, added) == ("llm-acme-com", True)
    assert config.upstreams == [Destination(name="llm-acme-com", url="https://llm.acme.com", agent="claude")]
    again, name, added = add_destination(config, "https://llm.acme.com/", "codex")
    assert (name, added) == ("llm-acme-com", False) and again is config


def test_a_new_address_never_takes_an_existing_name():
    config = Config(upstreams=[{"name": "llm-acme-com", "url": "https://llm.acme.com/anthropic"}])
    config, name, added = add_destination(config, "https://llm.acme.com/openai", "codex")
    assert (name, added) == ("llm-acme-com-2", True)
    assert [d.url for d in config.upstreams] == ["https://llm.acme.com/anthropic", "https://llm.acme.com/openai"]


def test_changing_or_removing_a_destination_lowers_protection():
    acme = {"name": "acme", "url": "https://llm.acme.com"}
    before = Config(upstreams=[acme])
    moved = Config(upstreams=[{**acme, "url": "https://elsewhere.example"}])
    assert loosened(before, moved) == ["destination acme: its address changed"]
    assert loosened(before, Config()) == ["destination acme: removed"]
    assert loosened(Config(), before) == []
    assert destination_changes(before, moved) == [
        {"name": "acme", "before": "https://llm.acme.com", "after": "https://elsewhere.example"}
    ]
    assert destination_changes(before, Config()) == [{"name": "acme", "before": "https://llm.acme.com", "after": None}]
