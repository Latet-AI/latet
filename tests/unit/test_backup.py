"""The backup file: sealed in chunks, refused when anything in it was changed, moved, cut or swapped."""

import io
import json
import sqlite3
import tarfile

import pytest

from latet.storage import backup
from latet.storage.backup import BackupError, read_backup, write_backup

FAST = {"memory": 8 * 1024, "iterations": 1, "lanes": 1}  # the smallest Argon2 cost accepted: tests only
PASS = "correct horse battery staple"


def _vault(data, values=3):
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / "latet.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, original BLOB)")
    db.executemany("INSERT INTO entities VALUES (?, ?)", [(f"e{i}", b"sealed" * 50) for i in range(values)])
    db.commit()
    return db


@pytest.fixture
def made(tmp_path):
    data = tmp_path / "data"
    db = _vault(data, values=2000)
    config = tmp_path / "config.yaml"
    config.write_bytes(b"profile: max\n")
    (data / "certificates").mkdir()
    (data / "certificates" / f"{'a' * 64}.pem").write_text("-----BEGIN CERTIFICATE-----\n", "utf-8")
    path = tmp_path / "b.latet"
    contents = write_backup(path, PASS, data, config, vault=True, history=False, root_key=b"k" * 32, kdf=FAST)
    yield path, contents, tmp_path
    db.close()


def test_a_backup_comes_back_whole(made):
    path, contents, tmp = made
    assert contents.vault and contents.values == 2000 and contents.certificates == 1
    assert b"profile" not in path.read_bytes()  # nothing in clear past the header
    opened = read_backup(path, PASS, tmp / "here")
    assert opened.config == b"profile: max\n" and opened.root_key == b"k" * 32
    assert opened.contents.values == 2000 and list(opened.certificates) == [f"{'a' * 64}.pem"]
    copy = sqlite3.connect(opened.staged["latet.db"])
    assert copy.execute("SELECT count(*) FROM entities").fetchone()[0] == 2000
    copy.close()
    opened.discard()
    assert list((tmp / "here").iterdir()) == []


@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 200])
def test_every_size_at_the_chunk_edges(tmp_path, monkeypatch, size):
    monkeypatch.setattr(backup, "CHUNK", 64)  # tar's own blocks then span many chunks
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.yaml"
    config.write_bytes(b"x" * size)
    path = tmp_path / "b.latet"
    write_backup(path, PASS, data, config, vault=False, history=False, kdf=FAST)
    assert read_backup(path, PASS, tmp_path / "here").config == b"x" * size


def _records(path, chunk):
    raw = path.read_bytes()
    magic_end = raw.index(b"\n") + 1
    header_end = raw.index(b"\n", magic_end) + 1
    body = raw[header_end:]
    size = chunk + backup.TAG
    return raw[:header_end], [body[i : i + size] for i in range(0, len(body), size)]


def test_a_changed_moved_cut_or_swapped_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "CHUNK", 4096)
    data = tmp_path / "data"
    db = _vault(data, values=200)
    config = tmp_path / "config.yaml"
    config.write_bytes(b"profile: max\n")
    path = tmp_path / "b.latet"
    write_backup(path, PASS, data, config, vault=True, history=False, root_key=b"k" * 32, kdf=FAST)
    db.close()
    header, records = _records(path, 4096)
    assert len(records) > 3

    def refused(raw, match="wrong, or the file is damaged"):
        bad = tmp_path / "bad.latet"
        bad.write_bytes(raw)
        with pytest.raises(BackupError, match=match):
            read_backup(bad, PASS, tmp_path / "here")
        assert not list((tmp_path / "here").glob(".*.restoring"))  # nothing left behind

    with pytest.raises(BackupError, match="wrong"):
        read_backup(path, "another passphrase", tmp_path / "here")
    changed = bytearray(records[1])
    changed[10] ^= 1
    refused(header + b"".join([records[0], bytes(changed), *records[2:]]))
    refused(header + b"".join([records[1], records[0], *records[2:]]))  # moved
    refused(header + b"".join(records[:-1]))  # cut at a chunk's end
    refused(header + b"".join(records)[:-100])  # cut inside one
    refused(header + b"".join([*records, records[-1]]))  # a chunk added
    swapped = header.replace(b'"iterations":1', b'"iterations":2')
    refused(swapped + b"".join(records))  # the header is bound to every chunk


def test_a_header_asking_too_much_or_from_a_newer_latet(tmp_path):
    path = tmp_path / "b.latet"
    salt = "c2FsdHNhbHRzYWx0c2FsdA=="
    for kdf, match in (
        ({"name": "argon2id", "memory": 64 * 1024 * 1024, "iterations": 1, "lanes": 1}, "outside"),
        ({"name": "scrypt", "memory": 8192, "iterations": 1, "lanes": 1}, "not one Latet knows"),
    ):
        path.write_bytes(backup.MAGIC + json.dumps({"format": 1, "kdf": kdf, "salt": salt}).encode() + b"\n")
        with pytest.raises(BackupError, match=match):
            read_backup(path, PASS, tmp_path / "here")
    path.write_bytes(backup.MAGIC + json.dumps({"format": 9, "kdf": {}, "salt": salt}).encode() + b"\n")
    with pytest.raises(BackupError, match="newer"):
        read_backup(path, PASS, tmp_path / "here")
    path.write_bytes(b"PK\x03\x04 a zip file\n")
    with pytest.raises(BackupError, match="not a Latet backup"):
        read_backup(path, PASS, tmp_path / "here")


def _crafted(tmp_path, build):
    """A backup sealed properly, holding what ``build(tar)`` puts in it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = b"s" * 16
    header = backup._header(FAST, salt)
    aead = AESGCM(backup._key(PASS, salt, FAST))
    out = io.BytesIO()
    out.write(header)
    sealer = backup._Sealer(out, lambda nonce, chunk: aead.encrypt(nonce, chunk, header))
    with tarfile.open(fileobj=sealer, mode="w|") as tar:
        build(tar)
    sealer.finish()
    path = tmp_path / "crafted.latet"
    path.write_bytes(out.getvalue())
    return path


def _add(tar, name, data=b"x", kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.type = kind
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


@pytest.mark.parametrize(
    "build",
    [
        lambda tar: _add(tar, "../../escape.txt"),
        lambda tar: _add(tar, "bridge.token"),
        lambda tar: _add(tar, "certificates/not-a-fingerprint.pem"),
        lambda tar: _add(tar, "latet.db", kind=tarfile.SYMTYPE),
        lambda tar: (_add(tar, "config.yaml"), _add(tar, "config.yaml")),
        lambda tar: _add(tar, "root.key", b"k" * 5000),
    ],
)
def test_only_what_a_backup_may_hold(tmp_path, build):
    path = _crafted(tmp_path, build)
    with pytest.raises(BackupError, match="should not"):
        read_backup(path, PASS, tmp_path / "here")
    assert not (tmp_path / "escape.txt").exists()


def test_a_vault_without_its_key_is_incomplete(tmp_path):
    manifest = json.dumps({"created": "x", "latet": "1", "platform": "x", "vault": True, "history": False})
    path = _crafted(tmp_path, lambda tar: _add(tar, "manifest.json", manifest.encode()))
    with pytest.raises(BackupError, match="incomplete"):
        read_backup(path, PASS, tmp_path / "here")


def test_the_note_after_a_restore_names_every_model_and_key_each_machine_keeps_for_itself():
    from latet.cli.backup import NOT_MOVED

    assert "latet ner install" in NOT_MOVED and "latet ocr install" in NOT_MOVED and "latet llm key" in NOT_MOVED
