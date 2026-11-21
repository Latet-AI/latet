"""A SIEM file is bounded JSON data before any administration credential is resolved."""

import json

import httpx
import pytest

from .test_cli import _run

CANARY = "secret-canary-yaml"


@pytest.mark.parametrize(
    "payload",
    [
        "siem: &loop [*loop]",
        "siem: [&item {name: collector}, *item]",
        "a: &a [one, two]\nb: &b [*a, *a, *a]\nsiem: [*b, *b, *b]",
        "siem: [2027-03-01]",
        "siem: [!!set {one: null}]",
        "siem: [!!binary c2VjcmV0LWNhbmFyeS15YW1s]",
        "siem: [.nan]",
        "siem: [.inf]",
        "siem: [-.inf]",
        "siem: [{1: one}]",
        "siem: [{null: one}]",
        "siem: [{true: one}]",
        "siem: " + "[" * 40 + "null" + "]" * 40,
        "siem: [" + "0," * 5000 + "0]",
        'siem: ["' + r"\0" * 12000 + '"]',
        'siem: ["\\uD800"]',
    ],
    ids=[
        "cycle",
        "alias",
        "expanded-alias",
        "timestamp",
        "set",
        "binary",
        "nan",
        "infinity",
        "negative-infinity",
        "integer-key",
        "null-key",
        "boolean-key",
        "depth",
        "nodes",
        "expanded-json-bytes",
        "surrogate",
    ],
)
def test_siem_yaml_rejects_unsafe_values_before_admin_call(monkeypatch, tmp_path, caplog, payload):
    from latet.ee.events import cli

    calls = []

    def administration(*args, **kwargs):
        calls.append(True)
        raise AssertionError("invalid input reached administration")

    monkeypatch.setattr(cli, "admin_call", administration)
    path = tmp_path / "outputs.yaml"
    text = f"canary: {CANARY}\n" + payload
    assert len(text.encode("utf-8")) <= cli.MAX_FILE
    path.write_text(text, encoding="utf-8")
    done = _run("siem", "set", str(path))
    assert calls == [], "invalid YAML must be refused before token resolution or transport"
    assert done.exit_code == 2
    assert "cannot be read as a file of outputs" in done.stderr
    assert CANARY not in done.stdout + done.stderr + caplog.text
    assert "Traceback" not in done.output


@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize("format", ["yaml", "json"])
def test_siem_yaml_preserves_normal_values_through_httpx_serialization(monkeypatch, tmp_path, wrapper, format):
    from latet.ee.events import cli

    outputs = [
        {
            "name": "collector",
            "kind": "webhook",
            "url": "https://siem.example/events",
            "headers": {"X-Label": "caf\N{LATIN SMALL LETTER E WITH ACUTE}"},
        }
    ]
    if format == "json":
        text = json.dumps({"siem": outputs} if wrapper else outputs, ensure_ascii=False)
    else:
        text = (
            "- name: collector\n  kind: webhook\n  url: https://siem.example/events\n"
            "  headers:\n    X-Label: caf\N{LATIN SMALL LETTER E WITH ACUTE}\n"
        )
        if wrapper:
            text = "siem:\n" + "".join("  " + line + "\n" for line in text.splitlines())
    calls = []

    def administration(method, path, body, *, query):
        # The real HTTPX JSON encoder used by ServerClient; constructing a request opens no socket.
        request = httpx.Request(method, "https://siem.example/administration", json=body)
        calls.append((method, path, json.loads(request.content), query))
        return {"outputs": outputs}

    monkeypatch.setattr(cli, "admin_call", administration)
    path = tmp_path / "outputs.yaml"
    path.write_text(text, encoding="utf-8")
    done = _run("siem", "set", str(path))
    assert done.exit_code == 0, done.output
    assert calls == [("PUT", "ee/siem", {"outputs": outputs}, None)]
    assert "SIEM outputs saved: collector." in done.output
