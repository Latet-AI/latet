"""EOP-31: byte bounds are shared; each startup secret keeps its own format.

These call existing consumers so missing future helper symbols cannot masquerade as RED.
Fault injection stays below the real parsing/validation boundary and never opens a special file.
Actual platform probes need the separately assigned isolated watchdog run.
"""

import base64
import errno
import os
import stat
import traceback
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from latet.server.master import MasterKey, MasterKeyError
from latet.server.settings import ServerSettings, _with_file_password, read_secret_file

MASTER_BYTES = 65536
PASSWORD_BYTES = 8192
REVIEWER_BYTES = 8192
ENCODED_ROOT = b"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
PASSWORD = "eop31 caf\N{LATIN SMALL LETTER E WITH ACUTE} / @ password"
REVIEWER = "eop31-reviewer-key-0123456789"
KINDS = ("master", "database", "valkey", "reviewer")
SENTINEL = "eop31-private-error-material"


@pytest.fixture(autouse=True)
def no_environment_master(monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)


def _limit(kind):
    return {"master": MASTER_BYTES, "database": PASSWORD_BYTES, "valkey": PASSWORD_BYTES, "reviewer": REVIEWER_BYTES}[
        kind
    ]


def _setting(kind):
    return (
        "master_key_file"
        if kind == "master"
        else f"{kind}_password_file"
        if kind != "reviewer"
        else "reviewer_key_file"
    )


def _payload(kind):
    return ENCODED_ROOT if kind == "master" else REVIEWER.encode() if kind == "reviewer" else PASSWORD.encode()


def _expected(kind):
    return 1 if kind == "master" else REVIEWER if kind == "reviewer" else PASSWORD


def _read(kind, path):
    if kind == "master":
        settings = ServerSettings(
            url="https://latet.acme.lan",
            database_url="postgresql://latet:test@127.0.0.1/latet",
            valkey_url="redis://:test@127.0.0.1/0",
            master_key_file=path,
        )
        return MasterKey.load(settings).version
    if kind == "reviewer":
        return read_secret_file(path, "reviewer_key_file")
    url = "postgresql://latet@127.0.0.1/latet" if kind == "database" else "redis://127.0.0.1/0"
    result = _with_file_password(f"{kind}_url", url, f"{kind}_password_file", path)
    return unquote(urlsplit(result).password or "")


def _error(kind):
    return MasterKeyError if kind == "master" else ValueError


def _safe(error, kind, path):
    shown = "".join(traceback.format_exception(error))
    assert str(path) not in shown
    assert SENTINEL not in shown
    assert ENCODED_ROOT.decode() not in shown
    assert PASSWORD not in shown and REVIEWER not in shown
    assert error.__cause__ is None and error.__context__ is None
    assert _setting(kind) in str(error) or (kind == "master" and "LATET_MASTER_KEY_FILE" in str(error))


def _info(original, *, mode=None, size=None):
    fields = list(original)
    if mode is not None:
        fields[0] = mode
    if size is not None:
        fields[6] = size
    return os.stat_result(fields)


class FileIO:
    """Keep real descriptor ownership/I/O; inject only the chosen filesystem fault.

    The finite read fuse also contains accidental non-progress/oversize mutations.
    It is not evidence for native special-file safety or remote filesystem latency.
    """

    def __init__(self, monkeypatch, path, limit):
        self.path, self.limit = path, limit
        self.descriptors = set()
        # The secret's descriptors still open: once closed, its number may be the next folder pytest's clean-up opens.
        self.live = set()
        self.closed = []
        self.read_sizes = []
        self.consumed = 0
        self.short: int | None = None
        self.post_mode: int | None = None
        self.post_size: int | None = None
        self.read_error = self.close_error = self.fstat_error = False
        self.read_error_after = 0
        self.real_open, self.real_read = os.open, os.read
        self.real_fstat, self.real_close = os.fstat, os.close
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "read", self.read)
        monkeypatch.setattr(os, "fstat", self.fstat)
        monkeypatch.setattr(os, "close", self.close)

    def open(self, path, flags, *args, **kwargs):
        descriptor = self.real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == os.fspath(self.path):
            self.descriptors.add(descriptor)
            self.live.add(descriptor)
        return descriptor

    def read(self, descriptor, size):
        if descriptor not in self.live:
            return self.real_read(descriptor, size)
        self.read_sizes.append(size)
        assert len(self.read_sizes) <= 64, "reader failed to stop at EOF or its finite byte ceiling"
        assert 0 < size <= self.limit + 1, "reader requested an unrestricted or oversized read"
        if self.read_error and len(self.read_sizes) > self.read_error_after:
            raise OSError(errno.EIO, SENTINEL, str(self.path))
        content = self.real_read(descriptor, min(size, self.short) if self.short else size)
        self.consumed += len(content)
        assert self.consumed <= self.limit + 1, "reader retained bytes beyond the sentinel"
        return content

    def fstat(self, descriptor):
        info = self.real_fstat(descriptor)
        if descriptor in self.live:
            if self.fstat_error:
                raise OSError(errno.EIO, SENTINEL, str(self.path))
            return _info(info, mode=self.post_mode, size=self.post_size)
        return info

    def close(self, descriptor):
        if descriptor not in self.live:
            return self.real_close(descriptor)
        self.closed.append(descriptor)
        self.live.discard(descriptor)
        self.real_close(descriptor)
        if self.close_error:
            raise OSError(errno.EIO, SENTINEL, str(self.path))

    def assert_closed(self):
        assert self.descriptors, "startup consumer did not use the bounded descriptor reader"
        assert sorted(self.closed) == sorted(self.descriptors), "descriptor not closed exactly once"


@pytest.mark.parametrize("kind", KINDS)
def test_bom_crlf_and_surrounding_whitespace_preserve_each_secret_format(tmp_path, kind):
    path = tmp_path / "secret"
    path.write_bytes(b"\xef\xbb\xbf \r\n" + _payload(kind) + b"\r\n \t")
    assert _read(kind, path) == _expected(kind)


def test_all_255_master_versions_keep_oldest_first_and_open_old_seals(tmp_path):
    path = tmp_path / "master"
    keys = [bytes([version]) * 32 for version in range(255)]
    path.write_bytes(b"\xef\xbb\xbf" + b"\r\n".join(base64.b64encode(key) for key in keys) + b"\r\n")
    settings = ServerSettings.model_construct(master_key_file=path)
    master = MasterKey.load(settings)
    assert master.version == 255
    first = MasterKey([bytes(32)]).seal(b"oldest version", "eop31")
    assert master.open(first, "eop31") == b"oldest version"
    last = master.seal(b"newest version", "eop31")
    assert last[:2] == b"\x01\xff"
    assert MasterKey(keys).open(last, "eop31") == b"newest version"


@pytest.mark.parametrize("bad", ["duplicate", "256th", "short", "invalid"])
def test_master_format_refuses_duplicate_256th_short_and_invalid_versions(tmp_path, bad):
    path = tmp_path / "master"
    payloads = {
        "duplicate": ENCODED_ROOT + b"\r\n" + ENCODED_ROOT,
        "256th": b"\n".join(base64.b64encode(bytes([n]) * 32) for n in range(256)),
        "short": ENCODED_ROOT + b"\n" + base64.b64encode(b"short"),
        "invalid": ENCODED_ROOT + b"\nnot-a-base64-key",
    }
    path.write_bytes(payloads[bad])
    with pytest.raises(MasterKeyError):
        _read("master", path)
    assert path.read_bytes() == payloads[bad]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("extra", [0, 1], ids=["exact-cap", "cap-plus-one"])
def test_raw_byte_cap_counts_bom_and_padding_before_format_trim(tmp_path, kind, extra):
    path = tmp_path / "secret"
    raw = b"\xef\xbb\xbf" + _payload(kind) + b"\r\n"
    raw += b" " * (_limit(kind) + extra - len(raw))
    path.write_bytes(raw)
    if extra:
        with pytest.raises(_error(kind)) as caught:
            _read(kind, path)
        _safe(caught.value, kind, path)
    else:
        assert _read(kind, path) == _expected(kind)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("kind", KINDS)
def test_known_oversize_file_is_refused_before_open(tmp_path, monkeypatch, kind):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind) + b" " * (_limit(kind) + 1))
    real_open = os.open

    def no_open(found, flags, *args, **kwargs):
        assert os.fspath(found) != os.fspath(path), "known oversized secret was opened"
        return real_open(found, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_open)
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR], ids=["fifo", "device", "directory"])
def test_known_nonregular_file_is_refused_before_open(tmp_path, monkeypatch, kind, mode):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    original, real_stat, real_open = path.stat(), os.stat, os.open

    def special(found, *args, **kwargs):
        if os.fspath(found) == os.fspath(path):
            return _info(original, mode=mode, size=0)
        return real_stat(found, *args, **kwargs)

    def no_open(found, flags, *args, **kwargs):
        assert os.fspath(found) != os.fspath(path), "known special secret was opened"
        return real_open(found, flags, *args, **kwargs)

    monkeypatch.setattr(os, "stat", special)
    monkeypatch.setattr(os, "open", no_open)
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR], ids=["fifo", "device", "directory"])
def test_post_open_type_is_authoritative_after_regular_precheck(tmp_path, monkeypatch, kind, mode):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    io = FileIO(monkeypatch, path, _limit(kind))
    io.post_mode = mode
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
    assert io.read_sizes == [], "swapped special file was read"
    io.assert_closed()


@pytest.mark.parametrize("kind", KINDS)
def test_post_open_size_is_checked_before_read(tmp_path, monkeypatch, kind):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    io = FileIO(monkeypatch, path, _limit(kind))
    io.post_size = _limit(kind) + 1
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
    assert io.read_sizes == [], "fstat oversize file was read"
    io.assert_closed()


@pytest.mark.parametrize("kind", KINDS)
def test_growth_after_open_is_refused_at_the_single_extra_byte(tmp_path, monkeypatch, kind):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    io = FileIO(monkeypatch, path, _limit(kind))
    real_fstat = io.fstat
    grown = False

    def grow(descriptor):
        nonlocal grown
        info = real_fstat(descriptor)
        if descriptor in io.descriptors and not grown:
            with path.open("ab") as stream:
                stream.write(b" " * (_limit(kind) + 1 - path.stat().st_size))
            grown = True
        return info

    monkeypatch.setattr(os, "fstat", grow)
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
    assert grown and io.consumed == _limit(kind) + 1
    io.assert_closed()


@pytest.mark.parametrize("kind", KINDS)
def test_short_reads_accumulate_until_eof_instead_of_accepting_a_prefix(tmp_path, monkeypatch, kind):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    io = FileIO(monkeypatch, path, _limit(kind))
    io.short = 7
    assert _read(kind, path) == _expected(kind)
    assert io.consumed == len(_payload(kind))
    assert len(io.read_sizes) > 1
    io.assert_closed()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", ["fstat", "read", "late-read", "close"])
def test_read_and_close_failures_refuse_without_returning_or_replacing_a_secret(tmp_path, monkeypatch, kind, failure):
    path = tmp_path / "secret"
    path.write_bytes(_payload(kind))
    io = FileIO(monkeypatch, path, _limit(kind))
    io.read_error, io.close_error, io.fstat_error = (
        failure in ("read", "late-read"),
        failure == "close",
        failure == "fstat",
    )
    if failure == "late-read":
        io.short, io.read_error_after = 7, 1
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
    io.assert_closed()
    assert path.read_bytes() == _payload(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_os_error_words_path_and_exception_context_never_escape(tmp_path, monkeypatch, kind):
    path = tmp_path / SENTINEL
    path.write_bytes(_payload(kind))

    def refused(*args, **kwargs):
        raise OSError(errno.EACCES, SENTINEL, str(path))

    monkeypatch.setattr(Path, "read_text", refused)
    monkeypatch.setattr(os, "open", refused)
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fault", ["missing", "directory", "nul", "utf8", "empty"])
def test_expected_bad_inputs_refuse_safely_without_creating_a_secret(tmp_path, kind, fault):
    path = tmp_path / SENTINEL
    if fault == "directory":
        path.mkdir()
    elif fault == "nul":
        path = Path(str(path) + chr(0) + "tail")
    elif fault in ("utf8", "empty"):
        path.write_bytes(b"\xff" + SENTINEL.encode() if fault == "utf8" else b" \r\n")
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
    if fault == "missing":
        assert not path.exists()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_regular_deployment_links_remain_supported(tmp_path, kind, link):
    target, path = tmp_path / "mounted-secret", tmp_path / "secret"
    target.write_bytes(_payload(kind))
    if link == "hard":
        os.link(target, path)
    else:
        try:
            path.symlink_to(target)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                pytest.skip("creating Windows symbolic links requires unavailable privilege")
            raise
    assert _read(kind, path) == _expected(kind)


@pytest.mark.skipif(os.name != "nt", reason="Windows native device names")
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", ["NUL", "CON", "COM1", "NUL.txt", "//./NUL"])
def test_windows_device_names_refuse_even_if_stat_claims_regular(tmp_path, monkeypatch, kind, name):
    fixture = tmp_path / "regular"
    fixture.write_bytes(_payload(kind))
    info = fixture.stat()
    path = Path(name)
    real_stat = os.stat

    def regular(found, *args, **kwargs):
        if os.fspath(found) == os.fspath(path):
            return info
        return real_stat(found, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("native device open attempted despite the known device spelling")

    monkeypatch.setattr(os, "stat", regular)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(_error(kind)) as caught:
        _read(kind, path)
    _safe(caught.value, kind, path)
