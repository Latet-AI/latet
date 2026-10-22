"""ES.10 local consumers. All keys, keyrings and files belong to the test."""

import io
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest
import typer
from starlette.requests import Request

from latet.api.rest import Dashboard
from latet.config import Config, IdentityConfig, LlmReview
from latet.engine.reviewer import reviewer as reviewer_module
from latet.logline import Paced
from latet.runtime import make_runtime
from latet.storage import keys

from ..conftest import ROOT_KEY
from .conftest import run_cli

BAD = [
    pytest.param("es10-" + chr(code) + "-private", id=name)
    for code, name in (
        (0, "nul"),
        (9, "tab"),
        (10, "newline"),
        (13, "return"),
        (32, "internal-space"),
        (127, "del"),
        (133, "c1"),
        (233, "non-ascii"),
        (0x202E, "bidi"),
        (0xFEFF, "internal-bom"),
    )
] + [pytest.param("X" * 4097, id="overlong"), pytest.param("", id="empty")]


def _fresh_pacing(monkeypatch):
    for module in (reviewer_module, keys):
        for name, value in list(vars(module).items()):
            if isinstance(value, Paced):
                monkeypatch.setattr(module, name, Paced())


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("where", ["file", "keyring"])
def test_direct_save_refuses_invalid_keys_before_touching_either_store(computer, keyring_on, monkeypatch, bad, where):
    monkeypatch.setenv("LATET_KEYRING", "on" if where == "keyring" else "off")
    data = computer / "data"
    data.mkdir()
    saved = data / "reviewer.key"
    saved.write_bytes(b"prior-file")
    keyring_on.set_password(keys._SERVICE, keys._REVIEWER, "prior-keyring")
    before = dict(keyring_on.kept)
    with pytest.raises((ValueError, OSError)) as refused:
        keys.save_reviewer_key(data, bad)
    assert saved.read_bytes() == b"prior-file" and keyring_on.kept == before
    if bad:
        assert bad not in str(refused.value)


@pytest.mark.parametrize("value", ["!", "~", "K" * 4096])
def test_direct_save_accepts_the_printable_ascii_boundaries(computer, value):
    assert keys.save_reviewer_key(computer / "data", value) == "file"
    assert keys.load_reviewer_key(computer / "data") == value


@pytest.mark.parametrize("source", ["file", "env", "keyring", "saved-file"])
@pytest.mark.parametrize("bad", BAD[:-1])
def test_an_invalid_selected_source_is_absent_paced_and_never_replaced_by_a_lower_one(
    computer, keyring_on, monkeypatch, caplog, source, bad
):
    _fresh_pacing(monkeypatch)
    data = computer / "data"
    data.mkdir()
    saved = data / "reviewer.key"
    saved.write_text(bad if source == "saved-file" else "lower-file", encoding="utf-8")
    keyring_on.set_password(keys._SERVICE, keys._REVIEWER, bad if source == "keyring" else "lower-keyring")
    monkeypatch.setenv("LATET_KEYRING", "on" if source == "keyring" else "off")
    monkeypatch.delenv("LATET_LLM_API_KEY", raising=False)
    config = LlmReview()
    if source == "env":
        if "\0" in bad:
            # Native environments cannot hold NUL: exercise this source's final validation with a local mapping.
            monkeypatch.setattr(reviewer_module, "os", SimpleNamespace(environ={config.api_key_env: bad}))
        else:
            monkeypatch.setenv("LATET_LLM_API_KEY", bad)
    elif source == "file":
        chosen = computer / "selected-key"
        chosen.write_text(bad, encoding="utf-8")
        config = LlmReview(api_key_file=str(chosen))
        monkeypatch.setenv("LATET_LLM_API_KEY", "lower-env")
    before = saved.read_bytes(), dict(keyring_on.kept)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert reviewer_module.api_key_of(config) is None
            assert reviewer_module.key_source(config) is None
    assert (saved.read_bytes(), keyring_on.kept) == before
    assert len(caplog.records) == 1
    assert bad not in caplog.text and repr(bad) not in caplog.text
    assert "es10-" not in caplog.text and str(computer) not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize("source", ["file", "saved-file"])
def test_invalid_utf8_in_a_local_key_file_is_absent_without_store_changes(computer, monkeypatch, caplog, source):
    data = computer / "data"
    data.mkdir()
    selected = data / "reviewer.key" if source == "saved-file" else computer / "selected-key"
    selected.write_bytes(b"private-es10-\xff")
    config = LlmReview(api_key_file=str(selected)) if source == "file" else LlmReview()
    if source == "file":
        monkeypatch.setenv("LATET_LLM_API_KEY", "lower-env")
    else:
        monkeypatch.delenv("LATET_LLM_API_KEY", raising=False)
    with caplog.at_level(logging.INFO):
        assert reviewer_module.api_key_of(config) is None
    assert selected.read_bytes() == b"private-es10-\xff"
    assert "private-es10" not in caplog.text and str(selected) not in caplog.text


def test_only_a_file_decoding_boundary_accepts_a_leading_bom(computer, monkeypatch):
    chosen = computer / "selected-key"
    chosen.write_bytes(b"\xef\xbb\xbfsk-file\r\n")
    assert reviewer_module.api_key_of(LlmReview(api_key_file=str(chosen))) == "sk-file"
    monkeypatch.setenv("LATET_LLM_API_KEY", chr(0xFEFF) + "sk-env")
    assert reviewer_module.api_key_of(LlmReview()) is None


@pytest.mark.parametrize("bad", BAD)
async def test_local_dashboard_put_refuses_invalid_keys_before_storage_and_reload(computer, monkeypatch, bad):
    data = computer / "data"
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False), ner="off"),
        data=data,
        config_file=computer / "config.yaml",
        root_key=ROOT_KEY,
    )
    writes, reloads = [], []
    monkeypatch.setattr(keys, "save_reviewer_key", lambda *args: writes.append(args))
    monkeypatch.setattr(rt, "use_reviewer", lambda config: reloads.append(config))
    raw = json.dumps({"key": bad}).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    try:
        request = Request({"type": "http", "method": "PUT", "headers": []}, receive)
        answer = await Dashboard(rt).reviewer_key(request)
        assert answer.status_code == 400
        assert writes == [] and reloads == []
        if bad:
            assert bad not in bytes(answer.body).decode()
    finally:
        rt.close()


@pytest.mark.parametrize("bad", ["es10-private internal-space", "es10-private" + chr(0xE9), "X" * 8193])
def test_piped_cli_rejects_bad_input_without_saving_reloading_or_prompting(computer, monkeypatch, bad):
    import latet.cli.llm  # noqa: F401 - registers the commands

    writes, reloads, prompts = [], [], []
    monkeypatch.setattr(keys, "save_reviewer_key", lambda *args: writes.append(args))
    monkeypatch.setattr("latet.cli.admin._reload_answer", lambda: reloads.append(True))
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: prompts.append(True))
    answer = run_cli("llm", "key", input=bad + "\n")
    assert answer.exit_code != 0 and writes == [] and reloads == [] and prompts == []
    assert bad not in answer.output


def test_fixed_server_key_never_reads_invalid_local_sources(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a fixed server reader consulted a local source")

    monkeypatch.setattr(reviewer_module, "_key_and_source", forbidden)
    reader = reviewer_module.fixed_key("server-key")
    assert reader(LlmReview(url="https://gpu.acme.lan/v1", remote=True)) == "server-key"
    assert reviewer_module.fixed_key(None)(LlmReview()) is None


def test_a_local_reviewer_client_is_built_without_an_invalid_selected_key(computer, monkeypatch):
    monkeypatch.setenv("LATET_LLM_API_KEY", "es10-private" + chr(0xE9))
    keys.save_reviewer_key(computer / "data", "lower-valid-saved-key")
    seen = []

    def client(url, model, api_key, **kwargs):
        seen.append(api_key)
        return object()

    monkeypatch.setattr(reviewer_module, "ReviewClient", client)
    reviewer_module.client_for(LlmReview())
    assert seen == [None]


@pytest.mark.parametrize("source", ["file", "saved-file"])
@pytest.mark.parametrize("raw_bytes", [8192, 8193])
def test_local_key_files_bound_raw_reads_before_whitespace_normalization(computer, monkeypatch, source, raw_bytes):
    data = computer / "data"
    data.mkdir()
    selected = data / "reviewer.key" if source == "saved-file" else computer / "selected-key"
    selected.write_bytes(b"K" + b" " * (raw_bytes - 1))
    config = LlmReview(api_key_file=str(selected)) if source == "file" else LlmReview()
    monkeypatch.delenv("LATET_LLM_API_KEY", raising=False)
    text_reads, binary_reads = [], []
    original_open, original_read = io.open, os.read
    identity = selected.stat()

    class TextRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            text_reads.append(size)
            return self.stream.read(size)

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return TextRead(stream) if str(path) == str(selected) else stream

    def read(fd, size):
        metadata = os.fstat(fd)
        result = original_read(fd, size)
        if (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino):
            binary_reads.append((size, len(result)))
        return result

    monkeypatch.setattr(io, "open", opened)
    monkeypatch.setattr(os, "read", read)
    assert reviewer_module.api_key_of(config) == ("K" if raw_bytes == 8192 else None)
    assert text_reads == [], "raw secret bytes must be bounded before text decoding"
    if raw_bytes == 8192:
        assert binary_reads and sum(count for _, count in binary_reads) == 8192
    else:
        assert binary_reads == [], "the reviewed reader rejects an oversized regular file before reading"
    assert all(0 < requested <= 8193 for requested, _ in binary_reads)


@pytest.mark.parametrize("raw_bytes", [8192, 8193])
def test_piped_keys_bound_raw_bytes_before_normalizing_or_saving(computer, monkeypatch, raw_bytes):
    from latet.cli.llm import llm_key

    writes, reloads, prompts, text_reads, binary_reads = [], [], [], [], []

    class InputBytes(io.BytesIO):
        def readline(self, size: int | None = -1):
            result = super().readline(size)
            binary_reads.append((size, len(result)))
            return result

    class InputText(io.TextIOWrapper):
        def readline(self, size: int = -1):
            text_reads.append(size)
            return super().readline(size)

    stream = InputText(InputBytes(b"K" + b" " * (raw_bytes - 2) + b"\n"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(keys, "save_reviewer_key", lambda data, key: writes.append(key) or "file")
    monkeypatch.setattr("latet.cli.admin._reload_answer", lambda: reloads.append(True))
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: prompts.append(True))
    try:
        if raw_bytes == 8192:
            llm_key(remove=False)
            assert writes == ["K"] and reloads == [True]
        else:
            with pytest.raises(typer.BadParameter):
                llm_key(remove=False)
            assert writes == [] and reloads == []
        assert prompts == [] and text_reads == []
        assert binary_reads and all(0 < requested <= 8193 for requested, _ in binary_reads)
        assert sum(count for _, count in binary_reads) <= 8193
    finally:
        stream.close()


def _selected_local_key(computer, keyring_on, monkeypatch, source, value):
    """Only fixture-owned stores; each higher-priority source has a valid lower credential to refuse falling back to."""
    data = computer / "data"
    data.mkdir(mode=0o700)  # as Latet makes it: a readable one is tightened, with a warning of its own
    saved = data / "reviewer.key"
    saved.write_text(value if source == "saved-file" else "lower-file", encoding="utf-8")
    keyring_on.set_password(keys._SERVICE, keys._REVIEWER, value if source == "keyring" else "lower-keyring")
    monkeypatch.setenv("LATET_KEYRING", "on" if source == "keyring" else "off")
    monkeypatch.delenv("LATET_LLM_API_KEY", raising=False)
    config = LlmReview()
    selected = saved
    if source == "env":
        monkeypatch.setenv("LATET_LLM_API_KEY", value)
    elif source == "file":
        selected = computer / "selected-key"
        selected.write_text(value, encoding="utf-8")
        config = LlmReview(api_key_file=str(selected))
        monkeypatch.setenv("LATET_LLM_API_KEY", "lower-env")
    return config, selected, saved


@pytest.mark.parametrize("source", ["file", "saved-file", "env", "keyring"])
def test_local_key_normalization_preserves_each_source_contract(computer, keyring_on, monkeypatch, caplog, source):
    _fresh_pacing(monkeypatch)
    config, selected, saved = _selected_local_key(
        computer, keyring_on, monkeypatch, source, " \tsk-private-normalized \r\n"
    )
    before = selected.read_bytes(), saved.read_bytes(), dict(keyring_on.kept)
    with caplog.at_level(logging.WARNING):
        value = reviewer_module.api_key_of(config)
    assert value == ("sk-private-normalized" if source in ("file", "saved-file") else None)
    if source in ("env", "keyring"):
        kind = "environment" if source == "env" else "saved keyring"
        assert [r.getMessage() for r in caplog.records] == [f"the local reviewer's {kind} key is unusable"]
    else:
        assert caplog.records == []
    assert (selected.read_bytes(), saved.read_bytes(), keyring_on.kept) == before


@pytest.mark.parametrize("source", ["file", "saved-file", "env", "keyring"])
async def test_runtime_starts_without_using_an_invalid_selected_local_key(
    computer, keyring_on, monkeypatch, caplog, source
):
    _fresh_pacing(monkeypatch)
    invalid = "es10-private-start-" + chr(0xE9)
    review, selected, saved = _selected_local_key(computer, keyring_on, monkeypatch, source, invalid)
    before = selected.read_bytes(), saved.read_bytes(), dict(keyring_on.kept)
    config = Config(
        identity=IdentityConfig(enabled=False), ner="off", llm_review=review.model_copy(update={"enabled": True})
    )
    with caplog.at_level(logging.WARNING):
        runtime = make_runtime(config, data=computer / "data", config_file=computer / "config.yaml", root_key=ROOT_KEY)
    try:
        reviewer = runtime.mediator.reviewer
        assert reviewer is not None
        assert reviewer.client.key_id is None and "authorization" not in reviewer.client._http.headers
        assert runtime.reviewer_key(review) is None
        assert (selected.read_bytes(), saved.read_bytes(), keyring_on.kept) == before
        kind = {
            "file": "configured file",
            "saved-file": "saved file",
            "env": "environment",
            "keyring": "saved keyring",
        }[source]
        assert [r.getMessage() for r in caplog.records] == [f"the local reviewer's {kind} key is unusable"]
        assert invalid not in caplog.text and str(computer) not in caplog.text
        assert all(r.exc_info is None for r in caplog.records)
    finally:
        if runtime.mediator.reviewer is not None:
            await runtime.mediator.reviewer.aclose()
        runtime.close()
