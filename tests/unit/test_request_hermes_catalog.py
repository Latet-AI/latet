"""Closed Hermes catalogue header compatibility and family isolation."""

import pytest

from latet.gateway import request_profiles as rp
from latet.gateway.coverage import EnvelopeRefused, envelope_of, inventory
from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream

CHATGPT = next(route for route in DEFAULT_UPSTREAMS if route.name == "chatgpt")
OPENAI = next(route for route in DEFAULT_UPSTREAMS if route.name == "openai")
RESIDENCY_HEADER = "x-openai-internal-codex-residency"


def _catalog_admission(region: str, upstream: Upstream = CHATGPT) -> rp.Admission:
    path = f"{upstream.prefix}/models" if upstream == CHATGPT else f"{upstream.prefix}/v1/models"
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"client_version=99.0.0" if upstream == CHATGPT else b"",
        "headers": [
            (b"host", b"127.0.0.1:5285"),
            (b"authorization", b"Bearer synthetic-token"),
            (RESIDENCY_HEADER.encode("ascii"), region.encode("ascii")),
        ],
    }
    return rp.admit(inventory(envelope_of(scope, "http", upstream.name, b"")), upstream)


@pytest.mark.parametrize("region", ["us", "eu", "no_constraint"])
def test_hermes_catalog_residency(region):
    admission = _catalog_admission(region)
    assert admission.profile.id == "chatgpt.codex.models.1"
    assert (RESIDENCY_HEADER, region) in admission.headers


@pytest.mark.parametrize("region", ["EU", "", "no_constraint; secret=synthetic"])
def test_hermes_catalog_rejects_invalid_residency(region):
    with pytest.raises(EnvelopeRefused) as error:
        _catalog_admission(region)
    assert error.value.code == "envelope-header-value"


@pytest.mark.parametrize("region", ["us", "eu", "no_constraint"])
def test_residency_is_not_admitted_on_public_openai(region):
    with pytest.raises(EnvelopeRefused) as error:
        _catalog_admission(region, OPENAI)
    assert error.value.code == "envelope-header-unknown"
