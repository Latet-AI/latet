"""A server's reviewer asks only with its own key, never another secret."""

import ast
import base64
import os
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest
from starlette.testclient import TestClient

from latet.config import Config, LlmReview
from latet.engine.reviewer import reviewer as reviewer_module
from latet.engine.reviewer.client import ReviewError, key_id
from latet.engine.reviewer.reviewer import fixed_key
from latet.gateway.app import OnServer, create_app
from latet.runtime import make_runtime
from latet.server.app import reviewer_key_of
from latet.server.master import MasterKey
from latet.server.settings import SECRET_BYTES, SECRET_LONGEST, ServerSettings, read_secret_file
from latet.state import IdentityRef, local_backends

from ...conftest import ROOT_KEY
from .conftest import EMILY, URL, MemorySettings, as_identity

KEY = "sk-server-" + "k" * 40
ELSEWHERE = "sk-environment-" + "e" * 30
REVIEWER = LlmReview(enabled=True, url="https://gpu.acme.lan/v1", remote=True, model="qwen")
API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}


def _space(tmp_path, reader=None, review=REVIEWER, identity=EMILY):
    settings = MemorySettings(Config(llm_review=review))
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=identity, reviewer_key=reader
    )
    rt.gateway.on_server = OnServer(URL, identity.id)
    return rt


@pytest.fixture
def nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_LLM_API_KEY", ELSEWHERE)
    monkeypatch.setenv("OTHER_REVIEW_KEY", ELSEWHERE)
    path = tmp_path / "other.key"
    path.write_text(ELSEWHERE, "utf-8")

    def refused(*args, **kwargs):
        raise AssertionError("a server read a saved key")

    monkeypatch.setattr(reviewer_module, "load_reviewer_key", refused)
    return REVIEWER.model_copy(update={"api_key_file": str(path), "api_key_env": "OTHER_REVIEW_KEY"})


@pytest.mark.parametrize(("reader", "expected"), [(None, None), (fixed_key(KEY), KEY)])
def test_a_space_reads_only_its_given_key(tmp_path, nothing_else, reader, expected, caplog):
    rt = _space(tmp_path, reader, nothing_else)
    try:
        assert rt.reviewer_key(rt.config.llm_review) == expected
        assert rt.mediator.reviewer.client.key_id == key_id(expected)
        rt.use_reviewer(rt.config)
        assert KEY not in repr(rt)
    finally:
        rt.close()
    assert KEY not in caplog.text and ELSEWHERE not in caplog.text


def test_file_bom_and_line_end(tmp_path):
    path = tmp_path / "reviewer.key"
    path.write_bytes(b"\xef\xbb\xbf" + KEY.encode() + b"\r\n")
    assert read_secret_file(path, "reviewer_key_file") == KEY


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b"", "holds nothing"),
        (b" \n", "holds nothing"),
        ((KEY + "\nsecond").encode(), "more than one line"),
        ((KEY + chr(7)).encode(), "control character"),
        ((KEY + " x").encode(), "a space"),
        (b"\xff", "not UTF-8"),
        (b"k" * (SECRET_LONGEST + 1), str(SECRET_LONGEST)),
        *[((KEY + chr(c) + "x").encode(), "outside ASCII") for c in (0x85, 0x2028, 0x202E, 0xE9, 0xFEFF)],
    ],
)
def test_unusable_secret_is_refused_without_its_content(tmp_path, content, said):
    path = tmp_path / "reviewer.key"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="reviewer_key_file") as exc:
        read_secret_file(path, "reviewer_key_file")
    assert said in str(exc.value)
    assert KEY not in str(exc.value) and str(path) not in str(exc.value)


def test_nonfiles_missing_and_zero_byte_paths_are_refused(tmp_path):
    for path in (tmp_path, tmp_path / "missing", Path("bad" + chr(0) + "path")):
        with pytest.raises(ValueError, match=r"reviewer_key_file.*cannot be read"):
            read_secret_file(path, "reviewer_key_file")


def test_read_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "large"
    path.write_bytes(b"k" * (SECRET_BYTES + 1))

    def refused(*args):
        raise AssertionError("oversize file was read")

    monkeypatch.setattr(os, "read", refused)
    with pytest.raises(ValueError, match="reviewer_key_file"):
        read_secret_file(path, "reviewer_key_file")


def test_a_growing_file_is_read_only_up_to_the_bound(tmp_path, monkeypatch):
    path = tmp_path / "grew"
    path.write_bytes(b"k")
    fstat, read = os.fstat, os.read
    reads = []

    def initially_small(fd):
        info = fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"k" * SECRET_BYTES)
        return info

    def bounded(fd, size):
        reads.append(size)
        assert size <= SECRET_BYTES + 1
        return read(fd, size)

    monkeypatch.setattr(os, "fstat", initially_small)
    monkeypatch.setattr(os, "read", bounded)
    with pytest.raises(ValueError, match="reviewer_key_file"):
        read_secret_file(path, "reviewer_key_file")
    assert reads == [SECRET_BYTES + 1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX special files")
def test_special_files_are_refused_without_waiting(tmp_path):
    import signal

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    def expired(*args):
        raise AssertionError("a special file blocked the read")

    before = signal.signal(signal.SIGALRM, expired)
    signal.alarm(2)
    try:
        for path in (fifo, Path("/dev/zero")):
            with pytest.raises(ValueError, match=r"reviewer_key_file.*cannot be read"):
                read_secret_file(path, "reviewer_key_file")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, before)


def test_environment_selects_the_key_file(tmp_path, monkeypatch):
    from latet.server.settings import load_server_settings

    path = tmp_path / "reviewer"
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@127.0.0.1/latet")
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("LATET_SERVER_REVIEWER_KEY_FILE", str(path))
    assert load_server_settings().reviewer_key_file == path


def _settings(path, **changes):
    return ServerSettings(
        url=URL,
        database_url="postgresql://latet@127.0.0.1/latet",
        valkey_url="redis://127.0.0.1:6379/0",
        reviewer_key_file=path,
        **changes,
    )


@pytest.mark.parametrize(
    "name", ["master_key_file", "database_password_file", "valkey_password_file", "metrics_token_file"]
)
def test_another_secrets_file_even_a_hard_link_is_refused(tmp_path, name):
    path, other = tmp_path / "reviewer", tmp_path / "other"
    path.write_text(KEY, "utf-8")
    os.link(path, other)
    with pytest.raises(ValueError, match="reviewer_key_file.*" + name) as exc:
        reviewer_key_of(_settings(path, **{name: other}), MasterKey([ROOT_KEY]))
    assert KEY not in str(exc.value) and str(path) not in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [
        base64.b64encode(ROOT_KEY).decode(),
        base64.b64encode(b"o" * 32).decode(),
        "latet_admin_x",
        "latet_token_x",
        "latet_key_x",
    ],
)
def test_another_secret_is_never_a_reviewer_key(tmp_path, value):
    from latet.server.tokens import ADMIN_PREFIX, KEY_PREFIX, TOKEN_PREFIX

    value = {
        "latet_admin_x": ADMIN_PREFIX + "x",
        "latet_token_x": TOKEN_PREFIX + "x",
        "latet_key_x": KEY_PREFIX + "x",
    }.get(value, value)
    path = tmp_path / "reviewer"
    path.write_text(value, "utf-8")
    with pytest.raises(ValueError, match="reviewer_key_file") as exc:
        reviewer_key_of(_settings(path), MasterKey([b"o" * 32, ROOT_KEY]))
    assert value not in str(exc.value) and str(path) not in str(exc.value)


def test_a_key_of_its_own_is_accepted(tmp_path):
    path = tmp_path / "reviewer"
    path.write_text(KEY, "utf-8")
    assert reviewer_key_of(_settings(path), MasterKey([ROOT_KEY])) == KEY


def test_every_version_of_a_master_key_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    master_path, path = tmp_path / "master", tmp_path / "reviewer"
    versions = [base64.b64encode(value).decode() for value in (b"o" * 32, ROOT_KEY)]
    master_path.write_text("\n".join(versions), "utf-8")
    settings = _settings(path, master_key_file=master_path)
    master = MasterKey.load(settings)
    for version in versions:
        path.write_text(version, "utf-8")
        with pytest.raises(ValueError, match=r"reviewer_key_file.*master key"):
            reviewer_key_of(settings, master)


def test_a_directly_constructed_runtime_defaults_to_no_key(nothing_else):
    from dataclasses import fields

    from latet.runtime import Runtime

    field = next(field for field in fields(Runtime) if field.name == "reviewer_key")
    assert field.default(nothing_else) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [("http://gpu.acme.lan:8080/v1", None), ("https://gpu.acme.lan/v1", KEY), ("http://127.0.0.1:8080/v1", KEY)],
)
def test_server_key_never_crosses_the_network_in_clear(url, expected):
    assert fixed_key(KEY)(REVIEWER.model_copy(update={"url": url})) == expected


class Recorded:
    keys: ClassVar[list] = []
    urls: ClassVar[list] = []

    def __init__(self, url, model=None, api_key=None, **kwargs):
        self.key_id = key_id(api_key)
        self.keys.append(api_key)
        self.urls.append(url)

    async def models(self):
        raise ReviewError("the stand-in answers nothing")

    async def ask(self, *args, **kwargs):
        raise ReviewError("the stand-in answers nothing")

    async def aclose(self):
        pass


def test_all_dashboard_calls_use_the_given_key_off_loop(tmp_path, nothing_else, monkeypatch):
    import asyncio

    from latet.api.rest import Dashboard
    from latet.columns import Column

    monkeypatch.setattr(reviewer_module, "ReviewClient", Recorded)
    monkeypatch.setattr(Dashboard, "_output_columns", lambda *args: ([Column("email", ["emily@acme.com"])], {}))
    loops = []

    def certificate(config):
        try:
            loops.append(asyncio.get_running_loop())
        except RuntimeError:
            loops.append(None)

    monkeypatch.setattr(reviewer_module, "certificate_of", certificate)
    Recorded.keys, Recorded.urls = [], []
    rt = _space(tmp_path, fixed_key(KEY), nothing_else)
    loops.clear()
    try:
        monkeypatch.setattr(Dashboard, "_shared", lambda self: None)
        with TestClient(as_identity(create_app(rt.gateway), EMILY.id), base_url=URL) as client:
            result = client.post(
                f"{API}/reviewer/test",
                json={"text": "Emily Carter called", "llm_review": {"url": "https://evil.example/v1", "remote": True}},
                headers=UI,
            )
            assert result.status_code == 200 and result.json()["ok"] is False
            assert (
                client.post(f"{API}/recognizers/draft", json={"description": "an order number"}, headers=UI).status_code
                == 502
            )
            assert client.post(f"{API}/exchanges/x/columns/review?slot=0", headers=UI).status_code == 502
            assert client.get(f"{API}/reviewer/key").json() == {"source": "server", "variable": None, "stored": None}
        assert set(Recorded.keys) == {KEY} and len(Recorded.keys) == 4
        assert set(Recorded.urls) == {REVIEWER.url}
        assert loops == [None, None, None]
    finally:
        rt.close()


def test_every_server_client_explicitly_chooses_its_key():
    src = Path(__file__).parents[3] / "src" / "latet"
    paths = [src / "runtime.py", *(src / "api").rglob("*.py"), *(src / "server").rglob("*.py")]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            direct = isinstance(node.func, ast.Name) and node.func.id == "client_for"
            threaded = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "client_for"
            )
            if direct or threaded:
                assert "key_of" in {kw.arg for kw in node.keywords}, (path, node.lineno)


async def test_one_dashboard_reviewer_call_without_waiting_and_other_identity_runs(tmp_path, monkeypatch):
    import asyncio

    import httpx

    entered, release = asyncio.Event(), asyncio.Event()

    class Held(Recorded):
        async def models(self):
            entered.set()
            await release.wait()
            raise ReviewError("the stand-in answers nothing")

    monkeypatch.setattr(reviewer_module, "ReviewClient", Held)
    rt = _space(tmp_path / "one", fixed_key(KEY))
    daniel = IdentityRef("u_danielokaf", "Daniel Okafor", "person")
    other = _space(tmp_path / "two", fixed_key(KEY), identity=daniel)
    try:
        transport = httpx.ASGITransport(app=as_identity(create_app(rt.gateway), EMILY.id))
        second = httpx.ASGITransport(app=as_identity(create_app(other.gateway), daniel.id))
        async with (
            httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as client,
            httpx.AsyncClient(transport=second, base_url=URL, trust_env=False) as another,
        ):
            first = asyncio.create_task(client.post(f"{API}/reviewer/test", json={}, headers=UI))
            await asyncio.wait_for(entered.wait(), 5)
            try:
                for endpoint, body in (
                    ("reviewer/test", {}),
                    ("recognizers/draft", {"description": "an order"}),
                    ("exchanges/x/columns/review?slot=0", {}),
                ):
                    result = await asyncio.wait_for(client.post(f"{API}/{endpoint}", json=body, headers=UI), 2)
                    assert result.status_code == 429 and result.headers["Retry-After"] == "5"
                entered.clear()
                independent = asyncio.create_task(another.post(f"{API}/reviewer/test", json={}, headers=UI))
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                release.set()
                await first
            assert (await independent).status_code == 200
            assert (await client.post(f"{API}/reviewer/test", json={}, headers=UI)).status_code == 200
    finally:
        rt.close()
        other.close()


def test_cleartext_warning_is_paced(tmp_path, caplog, monkeypatch):
    from latet.logline import Paced

    monkeypatch.setattr(reviewer_module, "_clear_key", Paced())
    review = REVIEWER.model_copy(update={"url": "http://gpu.acme.lan:8080/v1"})
    for name in ("first", "second"):
        rt = _space(tmp_path / name, fixed_key(KEY), review)
        try:
            assert rt.mediator.reviewer.client.key_id is None
        finally:
            rt.close()
    assert caplog.text.count("https address for the shared reviewer") == 1
    assert KEY not in caplog.text and review.url not in caplog.text


def test_a_computer_reads_its_key_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_LLM_API_KEY", ELSEWHERE)
    rt = make_runtime(Config(llm_review=REVIEWER), data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    try:
        assert rt.mediator.reviewer.client.key_id == key_id(ELSEWHERE)
    finally:
        rt.close()
