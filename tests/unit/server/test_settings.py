"""A Latet server's start-up settings, its secrets and their route keys, its master key and the sealing of what each
identity keeps: nothing here needs PostgreSQL or Valkey."""

import base64
import hashlib
import hmac
import os
import random
import re
import socket
import threading
import traceback
from urllib.parse import quote, unquote, urlsplit

import pytest
import redis.asyncio.connection
import redis.connection
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import pq
from pydantic import ValidationError

from latet.server import settings as server_settings
from latet.server.bus import redis_url
from latet.server.master import MasterKey, MasterKeyError
from latet.server.sealing import Sealer
from latet.server.settings import ServerSettings, load_server_settings, with_password
from latet.server.tokens import (
    ADMIN_PREFIX,
    KEY_PREFIX,
    TOKEN_PREFIX,
    is_route_key,
    new_id,
    new_secret,
    route_key,
    secret_hash,
)

from ...conftest import ROOT_KEY

URL = "https://latet.acme.lan"
TLS = "?sslmode=verify-full&sslrootcert=system"  # what an address off this machine says, for the server to check
DATABASE = "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet" + TLS
VALKEY = "rediss://:v4lkey-pw@cache.acme.lan:6379/0"
ODD = "\N{ACCOUNT OF}"  # one character that reads as "a/c": the standard library refuses an address that holds it
_VARIABLES = (
    "LATET_SERVER_URL", "LATET_SERVER_DATABASE_URL", "LATET_SERVER_VALKEY_URL", "LATET_SERVER_PORT",
    "LATET_MASTER_KEY_FILE", "LATET_SERVER_CONFIG", "LATET_SERVER_LOG_FORMAT", "LATET_SERVER_DATABASE_PASSWORD_FILE",
    "LATET_SERVER_VALKEY_PASSWORD_FILE", "LATET_SERVER_VALKEY_UNAUTHENTICATED",
)  # fmt: skip


def _settings(**changes):
    return ServerSettings(**({"url": URL, "database_url": DATABASE, "valkey_url": VALKEY} | changes))


@pytest.mark.parametrize(
    ("given", "url"),
    [("https://latet.acme.lan", URL), ("https://Latet.Acme.LAN/", URL), ("https://latet.acme.lan:443", URL),
     ("https://latet.acme.lan:8443", "https://latet.acme.lan:8443"), ("http://127.0.0.1:6655", "http://127.0.0.1:6655")],
)  # fmt: skip
def test_the_address_is_kept_as_a_browser_names_its_origin(given, url):
    assert _settings(url=given).url == url


@pytest.mark.parametrize(
    "given", ["http://latet.acme.lan", "https://latet.acme.lan/latet", "https://u:p@latet.acme.lan", "latet.acme.lan"]
)
def test_an_address_that_is_not_https_alone_or_this_machines_is_refused(given):
    with pytest.raises(ValidationError):
        _settings(url=given)


def test_the_databases_and_valkeys_addresses_are_checked_and_never_shown():
    with pytest.raises(ValidationError, match="PostgreSQL"):
        _settings(database_url="mysql://db")
    with pytest.raises(ValidationError, match="Valkey"):
        _settings(valkey_url="http://cache")
    for given in ("valkeys://:v4lkey-pw@cache.acme.lan:6379/0", "valkey://:v4lkey-pw@127.0.0.1:6379/0"):
        assert _settings(valkey_url=given).valkey_url == given  # valkeys:// is TLS; valkey:// is on this machine
    shown = repr(_settings())
    assert "s3cret-pw" not in shown and "v4lkey-pw" not in shown


def test_valkey_needs_a_password_unless_on_this_machine_or_said_so(tmp_path, monkeypatch):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="password"):
        _settings(valkey_url="rediss://cache.acme.lan:6379/0")  # over TLS: it is the password that is wanted
    assert _settings(valkey_url="redis://127.0.0.1:6379/0").valkey_url == "redis://127.0.0.1:6379/0"  # the tests
    assert _settings(valkey_url="rediss://cache.acme.lan:6379/0", valkey_unauthenticated=True).valkey_unauthenticated
    (tmp_path / "db.pw").write_text("s3cret-pw\n", "utf-8")
    (tmp_path / "valkey.pw").write_text("v4lkey-pw\n", "utf-8")
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@db.acme.lan:5432/latet" + TLS)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", "rediss://cache.acme.lan:6379/0")
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(tmp_path / "db.pw"))
    monkeypatch.setenv("LATET_SERVER_VALKEY_PASSWORD_FILE", str(tmp_path / "valkey.pw"))
    found = load_server_settings()
    assert (found.database_url, found.valkey_url) == (DATABASE, VALKEY)  # the passwords, from their files
    assert "s3cret-pw" not in repr(found) and "v4lkey-pw" not in repr(found)
    assert with_password(DATABASE, "another") == DATABASE  # an address with a password keeps its own


def test_the_limits_and_the_proxys_address_are_settings():
    found = _settings()
    assert (found.max_request_mb, found.vault_limit, found.admin_token_days, found.forwarded_allow_ips) == (
        32, 200_000, 90, "127.0.0.1"
    )  # fmt: skip
    for wrong in ({"max_request_mb": 0}, {"vault_limit": 10}, {"admin_token_days": 0}, {"forwarded_allow_ips": "*"}):
        with pytest.raises(ValidationError):
            _settings(**wrong)
    assert _settings(forwarded_allow_ips="10.0.5.2, 10.0.5.0/24").forwarded_allow_ips == "10.0.5.2,10.0.5.0/24"


def test_a_certificate_comes_with_its_key(tmp_path):
    with pytest.raises(ValidationError, match="together"):
        _settings(certificate=tmp_path / "server.crt")


def test_variables_go_over_server_yaml(tmp_path, monkeypatch):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "server.yaml"
    path.write_text(f"url: {URL}\ndatabase_url: {DATABASE}\nvalkey_url: {VALKEY}\nport: 6000\n", "utf-8")
    assert load_server_settings(path).port == 6000
    monkeypatch.setenv("LATET_SERVER_PORT", "7000")
    monkeypatch.setenv("LATET_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    monkeypatch.setenv("LATET_SERVER_LOG_FORMAT", "json")
    found = load_server_settings(path)
    assert (found.port, found.master_key_file, found.log_format) == (7000, tmp_path / "master.key", "json")
    monkeypatch.setenv("LATET_SERVER_CONFIG", str(path))
    assert load_server_settings().url == URL


def test_a_secret_is_long_kept_by_its_hash_and_its_route_is_derived():
    one, two = new_secret(TOKEN_PREFIX), new_secret(TOKEN_PREFIX)
    assert one.startswith("latet_pat_") and len(one) == len(TOKEN_PREFIX) + 43 and one != two
    assert len(secret_hash(one)) == 64 and secret_hash(one) != secret_hash(two) and one not in secret_hash(one)
    assert route_key(one) == route_key(one) != route_key(two) and is_route_key(route_key(one))
    assert not is_route_key(route_key(one).upper()) and not is_route_key("short")
    assert new_secret(KEY_PREFIX).startswith("latet_key_") and new_secret(ADMIN_PREFIX).startswith("latet_adm_")
    identity = new_id("u_")
    assert identity.startswith("u_") and len(identity) == 12 and identity[2:].isalnum() and identity == identity.lower()


def test_the_master_key_is_needed_and_seals_with_its_place(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY_FILE"):
        MasterKey.load(_settings())
    (tmp_path / "master.key").write_text(MasterKey.generate() + "\n", "utf-8")
    master = MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))
    sealed = master.seal(b"root key of u_emilycarte", "root-key:u_emilycarte")
    assert b"emilycarte" not in sealed and master.open(sealed, "root-key:u_emilycarte") == b"root key of u_emilycarte"
    with pytest.raises(InvalidTag):
        master.open(sealed, "root-key:u_danielokaf")  # a sealed value moved elsewhere does not open
    monkeypatch.setenv("LATET_MASTER_KEY", base64.b64encode(b"\x01" * 16).decode())
    with pytest.raises(MasterKeyError, match="32 bytes"):
        MasterKey.load(_settings())


def test_a_sealed_value_names_the_version_of_the_key_that_sealed_it(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    first, second = MasterKey.generate(), MasterKey.generate()
    (tmp_path / "master.key").write_text(f"{first}\n{second}\n", "utf-8")  # oldest first: the last is current
    master = MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))
    older = MasterKey([base64.b64decode(first)])
    sealed = older.seal(b"x", "root-key:u_emilycarte")
    assert (master.version, older.version, sealed[0], sealed[1]) == (2, 1, 1, 1)  # the format, then the version
    assert master.open(sealed, "root-key:u_emilycarte") == b"x"  # sealed with version 1, which it still holds
    now = master.seal(b"x", "root-key:u_emilycarte")
    assert now[1] == 2 and master.open(now, "root-key:u_emilycarte") == b"x"
    with pytest.raises(MasterKeyError, match="version 2"):
        older.open(now, "root-key:u_emilycarte")
    with pytest.raises(MasterKeyError, match="not a sealed value"):
        master.open(b"\x07" + now[1:], "root-key:u_emilycarte")
    assert master.derive(b"bus") != older.derive(b"bus") and len(master.derive(b"bus")) == 32
    signed = master.sealer(b"sign-ins").seal_json({"s": "u_emilycarte"}, "signin:session:abc")
    assert master.sealer(b"sign-ins").open_json(signed, "signin:session:abc") == {"s": "u_emilycarte"}
    with pytest.raises(InvalidTag):
        master.sealer(b"sign-ins").open_json(signed, "signin:session:xyz")  # bound to its key


def test_what_an_identity_keeps_is_sealed_with_its_own_key():
    mine, theirs = Sealer(ROOT_KEY, b"storage"), Sealer(b"\x08" * 32, b"storage")
    sealed = mine.seal_json({"text": "emily.carter@acme.com"}, "u_emilycarte:x")
    assert sealed[:2] == b"\x01\x01" and b"emily" not in sealed  # the format, and the root key's version
    assert mine.open_json(sealed, "u_emilycarte:x") == {"text": "emily.carter@acme.com"}
    with pytest.raises(InvalidTag):
        theirs.open(sealed, "u_emilycarte:x")
    assert mine.keyed("Orion") == mine.keyed("Orion") != theirs.keyed("Orion") and b"Orion" not in mine.keyed("Orion")
    with pytest.raises(ValueError, match="version 2"):
        mine.open(b"\x01\x02" + sealed[2:], "u_emilycarte:x")  # a version this sealer does not hold


def _shown(error: BaseException) -> str:
    """All an error shows of itself: its words, its repr, and its chain as a traceback prints it (no source lines)."""
    return f"{error}\n{error!r}\n{''.join(traceback.format_exception(error, limit=0))}"


def _holds_no_password(error: BaseException) -> bool:
    return not any(part in _shown(error) for part in ("s3cret", "v4lkey"))


@pytest.mark.parametrize(
    "wrong",
    [
        {"database_url": "postgre://latet:s3cret-pw@db/latet"},  # a mistyped scheme
        {"valkey_url": "redi://:v4lkey-pw@cache.acme.lan:6379/0"},
        {"database_password": "s3cret-pw"},  # a setting that does not exist, where a file's name is wanted
        {"port": "s3cret-pw"},
        # The settings refused as a whole: pydantic would show them all, shortened in the middle.
        {"valkey_url": "redis://cache.acme.lan:6379/0", "database_url": "postgres://l:s3cret-pw@db/l"},
        {"key": "server.key", "database_url": "postgres://l:s3cret-pw@db/l"},
    ],
)
def test_a_start_up_error_never_shows_a_password(wrong):
    given = {"url": URL, "valkey_url": VALKEY, "key": None, "database_url": DATABASE}  # the password comes last
    with pytest.raises(ValidationError) as refused:
        ServerSettings(**(given | wrong))
    assert _holds_no_password(refused.value), _shown(refused.value)


def test_an_address_that_cannot_be_read_is_refused_in_words_that_do_not_quote_it(tmp_path, monkeypatch):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    database = f"postgresql://latet:s3cret{ODD}pw@db.acme.lan:5432/latet"  # urlsplit's own error quotes the address
    valkey = f"redis://:v4lkey{ODD}pw@cache.acme.lan:6379/0"
    for wrong, words in (({"database_url": database}, "PostgreSQL"), ({"valkey_url": valkey}, "Valkey")):
        with pytest.raises(ValidationError, match=words) as refused:
            _settings(**wrong)
        assert _holds_no_password(refused.value), _shown(refused.value)
    slashed = "postgresql://latet:s3cret/pw@db.acme.lan:5432/latet"  # the "port" urlsplit reads is half the password
    for url in (database, valkey, slashed, "redis://cache.acme.lan:63x79/0"):
        with pytest.raises(ValueError, match="cannot be read") as refused:
            with_password(url, "v4lkey-pw")
        assert _holds_no_password(refused.value), _shown(refused.value)
    (tmp_path / "db.pw").write_text("v4lkey-pw\n", "utf-8")
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", slashed)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(tmp_path / "db.pw"))
    with pytest.raises(ValueError, match=r"database_url holds @ after its host") as refused:  # the setting is named
        load_server_settings()  # and the address judged as it is written: the words it has without the file
    assert _holds_no_password(refused.value), _shown(refused.value)


def test_a_password_from_a_file_changes_nothing_else_of_an_address():
    password = "p@ss:w/rd#?%20 &=\N{LATIN SMALL LETTER E WITH ACUTE}"  # whatever a file holds: it stays the password
    for url in (
        "postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full",
        "redis://cache.acme.lan:6379/0",
        "rediss://default@[2001:db8::5]:6379/0",
        "unix:///run/valkey.sock",
        "postgresql://latet@DB.Acme.LAN:05432/latet",  # the host and the port as they are written, not as they are read
        "postgresql://latet@db.acme.lan:0/latet",
        "rediss://default@[2001:DB8::5]:/2",
    ):
        given, made = urlsplit(url), urlsplit(with_password(url, password))
        assert unquote(made.password or "") == password and (made.username or None) == given.username
        assert (made.scheme, made.hostname, made.port, made.path, made.query, made.fragment) == (
            given.scheme, given.hostname, given.port, given.path, given.query, given.fragment
        )  # fmt: skip
        put_in = f":{quote(password, safe='')}@"  # taken out again, the address is the one that was given
        assert with_password(url, password).replace(put_in, "@" if given.username else "", 1) == url


def test_a_server_yaml_that_cannot_be_read_is_said_by_its_place_and_never_quoted(tmp_path, monkeypatch):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "server.yaml"
    path.write_text(f"url: {URL}\ndatabase_url: {DATABASE}: oops\nvalkey_url: {VALKEY}\n", "utf-8")
    with pytest.raises(ValueError, match=r"server\.yaml is not valid YAML \(line 2, column \d+\)") as refused:
        load_server_settings(path)  # PyYAML's own words quote the line, with the password in it
    assert _holds_no_password(refused.value), _shown(refused.value)
    path.write_bytes(f"url: {URL}\ndatabase_url: {DATABASE}\nvalkey_url: ".encode() + b"\xff\n")
    with pytest.raises(ValueError, match=r"server\.yaml is not UTF-8 text") as refused:
        load_server_settings(path)  # the decoding error's repr holds the whole file
    assert _holds_no_password(refused.value), _shown(refused.value)
    path.write_text("- url\n- database_url\n", "utf-8")
    with pytest.raises(ValueError, match="holds no settings"):
        load_server_settings(path)
    with pytest.raises(FileNotFoundError):
        load_server_settings(tmp_path / "another.yaml")


def test_a_password_file_that_cannot_be_read_stops_the_start_and_is_not_shown(tmp_path, monkeypatch):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@db.acme.lan:5432/latet")
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", "s3cret-pw")  # the password, where its file is wanted
    with pytest.raises(ValueError, match="database_password_file names a file that cannot be read") as refused:
        load_server_settings()
    assert _holds_no_password(refused.value), _shown(refused.value)
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(tmp_path / "db.pw"))
    (tmp_path / "db.pw").write_bytes(b"s3cret-pw\xff\n")
    with pytest.raises(ValueError, match=r"database_password_file names a file .* not UTF-8 text") as refused:
        load_server_settings()  # the decoding error's repr holds the file, which is the password
    assert _holds_no_password(refused.value), _shown(refused.value)


def test_a_master_key_that_is_not_one_stops_the_start_and_is_never_shown(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    path = tmp_path / "master.key"
    settings = _settings(master_key_file=path)
    with pytest.raises(MasterKeyError, match="cannot be read"):  # the file is not there
        MasterKey.load(settings)
    path.write_text("\n", "utf-8")
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY_FILE"):  # a file that holds no key
        MasterKey.load(settings)
    path.write_bytes(bytes(range(0xC0, 0xE0)))  # the key's 32 bytes as they are, where their base64 is wanted
    with pytest.raises(MasterKeyError) as refused:
        MasterKey.load(settings)
    assert str(refused.value) == "master_key_file names a file that cannot be read: it is not UTF-8 text"
    assert "Unicode" not in _shown(refused.value)  # not chained to the decoding error, whose repr holds the key
    path.write_text("s3cret-pw is not a key\n", "utf-8")
    with pytest.raises(MasterKeyError, match="base64") as refused:
        MasterKey.load(settings)
    assert _holds_no_password(refused.value), _shown(refused.value)
    path.write_text(f"{MasterKey.generate()}\n{base64.b64encode(b'too short').decode()}\n", "utf-8")
    with pytest.raises(MasterKeyError, match="32 bytes"):  # one line of the file is not a key: none is used
        MasterKey.load(settings)
    with pytest.raises(MasterKeyError, match="32 bytes"):
        MasterKey([])


def test_a_sealer_takes_no_key_that_is_not_32_bytes():
    for wrong in (b"", b"\x08" * 16, [], [ROOT_KEY, b""]):
        with pytest.raises(ValueError, match="32 bytes"):
            Sealer(wrong, b"storage")


def test_a_sealed_value_has_a_nonce_of_its_own_and_opens_for_its_purpose_alone(monkeypatch):
    master, storage, valkey = MasterKey([b"\x05" * 32]), Sealer(ROOT_KEY, b"storage"), Sealer(ROOT_KEY, b"valkey")
    drawn, urandom, here = [], os.urandom, threading.get_ident()

    def recorded(size):  # what this test draws: a thread that an earlier test left running may draw meanwhile
        data = urandom(size)
        if threading.get_ident() == here:
            drawn.append(data)
        return data

    monkeypatch.setattr(os, "urandom", recorded)
    for seal, opened in ((master.seal, master.open), (storage.seal, storage.open)):
        drawn.clear()
        blobs = [seal(b"the same text", "u_emilycarte:x") for _ in range(200)]
        assert [blob[2:14] for blob in blobs] == drawn and len(set(drawn)) == 200  # 12 bytes of the system's, each time
        assert len(set(blobs)) == 200 and all(len(blob) == 2 + 12 + len(b"the same text") + 16 for blob in blobs)
        for at in range(len(blobs[0])):  # one bit changed anywhere, the frame's two bytes included: it does not open
            changed = blobs[0][:at] + bytes([blobs[0][at] ^ 1]) + blobs[0][at + 1 :]
            with pytest.raises((InvalidTag, ValueError, MasterKeyError)):
                opened(changed, "u_emilycarte:x")
    with pytest.raises(InvalidTag):
        valkey.open(storage.seal(b"x", "u_emilycarte:x"), "u_emilycarte:x")  # one root key, two purposes: two keys
    with pytest.raises(InvalidTag):
        master.open(master.sealer(b"sign-ins").seal(b"x", "signin:session:abc"), "signin:session:abc")
    assert storage.keyed("Orion") != valkey.keyed("Orion") and master.derive(b"bus") != master.derive(b"audit")
    older, both = MasterKey([b"\x05" * 32]), MasterKey([b"\x05" * 32, b"\x06" * 32])
    sealed = older.sealer(b"sign-ins").seal(b"x", "signin:session:abc")
    assert both.sealer(b"sign-ins").open(sealed, "signin:session:abc") == b"x"  # the older version is still held
    with pytest.raises(InvalidTag):  # said to be of version 2, it is opened with that key: which is another
        both.sealer(b"sign-ins").open(sealed[:1] + b"\x02" + sealed[2:], "signin:session:abc")


def test_the_proxys_addresses_never_mean_every_address():
    for wrong in ("0.0.0.0/0", "::/0", "10.0.5.2, 0.0.0.0/0", "*, 10.0.5.2", "", "the proxy"):
        with pytest.raises(ValidationError):
            _settings(forwarded_allow_ips=wrong)


@pytest.mark.parametrize(
    ("given", "words", "quoted"),
    [
        ("10.0.5.2, the-proxy.acme.lan", "entry 2 is not an address or a network", "the-proxy"),
        ("192.168.7.9/16", "entry 1 is an address with a network's length: write the network, 192.168.0.0/16", "7.9"),
        ("10.0.5.2, 10.0.5.0/24, 172.16.4.1/12", "entry 3 is an address with a network's length: write the network, 172.16.0.0/12", "4.1"),
        ("10.0.5.2, 2001:db8::7/32", "entry 2 is an address with a network's length: write the network, 2001:db8::/32", "::7"),
        ("10.0.5.2, 0.0.0.0/0", "entry 2 is every address", "0.0.0.0"),
        ("10.0.5.7/0", "entry 1 is every address", "write the network"),  # not told to write what is refused next
        ("::5/0", "entry 1 is every address", "write the network"),
    ],
)  # fmt: skip
def test_a_proxys_address_that_is_refused_is_named_by_its_place_and_not_quoted(given, words, quoted):
    with pytest.raises(ValidationError, match=f"forwarded_allow_ips: {re.escape(words)}") as refused:
        _settings(forwarded_allow_ips=given)  # a network is read as uvicorn reads it: with no address's bits in it
    assert quoted not in str(refused.value)


@pytest.mark.parametrize(
    ("given", "place"),
    [
        ("0.0.0.0/1, 128.0.0.0/1", 1),  # two networks of a length of 1: every IPv4 address
        ("10.0.5.2, 10.0.0.0/7", 2),
        ("2000::/3", 1),
        ("10.0.5.2, 2001:db8::/31", 2),
        # IPv4 addresses written in IPv6, as a listener on both families sees a client: every one, or half of them.
        ("::ffff:0:0/96", 1),
        ("::ffff:0:0/97", 1),
        ("::/80", 1),  # holds them all
    ],
)
def test_the_proxys_addresses_are_no_network_wider_than_a_proxys_own(given, place):
    """Every client in a network believed to be the proxy can say where it comes from: the limits by address and the
    administration log's address rest on this setting. An IPv4 network is /8 or longer, an IPv6 one /32 or longer, and
    IPv4 addresses written in IPv6 count as IPv4 addresses."""
    with pytest.raises(ValidationError, match=f"forwarded_allow_ips: entry {place} is a network wider") as refused:
        _settings(forwarded_allow_ips=given)
    assert given.split(", ")[place - 1].partition("/")[0] not in str(refused.value)  # named by its place alone


def test_a_proxys_network_as_wide_as_allowed_is_taken():
    for given in ("10.0.0.0/8", "2001:db8::/32", "::ffff:10.0.0.0/104", "10.0.5.2", "::ffff:10.0.5.2", "fd00::/48"):
        assert _settings(forwarded_allow_ips=given).forwarded_allow_ips == given


@pytest.mark.parametrize(
    ("setting", "address", "advice"),
    [
        ("database_url", "postgresql://latet:s3cret-pw@[fe80::1%25eth0]:5432/latet", "?host=fe80::1%25eth0"),
        ("valkey_url", "rediss://:v4lkey-pw@[fe80::1%25eth0]:6379/0", None),
    ],
)
def test_an_ipv6_host_with_a_zone_is_refused_in_a_sentence_that_is_true(setting, address, advice):
    """It is an IPv6 address between brackets, with a zone after it: the words said it was not one."""
    with pytest.raises(ValidationError, match=rf"{setting} has an IPv6 host with a zone") as refused:
        _settings(**{setting: address})
    said = str(refused.value)
    assert "is not an IPv6 address" not in said and _holds_no_password(refused.value)
    assert (advice in said) if advice else ("?host=" not in said)


@pytest.mark.parametrize(
    "query",
    ["sslmode=require&sslrootcert=system", "sslrootcert=system&sslmode=verify-ca", "sslmode=prefer&sslrootcert=system",
     "sslmode=disable&sslrootcert=system", "sslrootcert=system&sslmode="],
)  # fmt: skip
@pytest.mark.parametrize("host", ["db.acme.lan", "127.0.0.1"])
def test_sslrootcert_system_beside_another_sslmode_is_refused_in_words_on_this_machine_too(query, host, libpq_alone):
    """libpq takes the system's certificate authorities with verify-full alone and refuses any other mode beside them,
    saying only "weak sslmode": the operator would see the driver's error by its type, whatever the address's place.
    The network's word changes nothing of it."""
    if query.endswith("=verify-ca") or "require" in query:
        assert _libpq_refuses_as_weak(query)  # why
    address = f"postgresql://latet:s3cret-pw@{host}:5432/latet?{query}"
    for private in (False, True):
        with pytest.raises(ValidationError, match="database_url names sslrootcert=system") as refused:
            _settings(database_url=address, database_private_network=private)
        assert "verify-full" in str(refused.value) and _holds_no_password(refused.value)


@pytest.mark.parametrize(
    "parameter",
    [
        "socket_timeout=60", "socket_connect_timeout=60", "socket_keepalive=false", "health_check_interval=0",
        "max_connections=100000", "timeout=0", "client_name=latet:listen",
    ],
)  # fmt: skip
def test_what_the_server_sets_of_its_valkey_connections_is_not_an_addresss_parameter(parameter):
    """redis-py lets an address's parameters win over what the code gives it: the times a connection and an answer
    have, the pool's size and its wait, the keepalives, the health check and the listening connection's name are the
    server's."""
    with pytest.raises(ValidationError, match="valkey_url sets among its parameters what the server sets") as refused:
        _settings(valkey_url=f"rediss://:v4lkey-pw@cache.acme.lan:6379/0?{parameter}")
    assert _holds_no_password(refused.value)


def test_how_many_connections_an_instance_keeps_to_valkey_is_a_setting():
    assert _settings().valkey_connections == 100
    assert _settings(valkey_connections=8).valkey_connections == 8
    for wrong in (0, 3, 100_001):
        with pytest.raises(ValidationError):
            _settings(valkey_connections=wrong)


@pytest.mark.parametrize(
    ("setting", "address", "words"),
    [
        ("database_url", "postgresql://latet:s3cret%pw@db.acme.lan:5432/latet", "% that starts no escape"),
        ("database_url", "postgresql://latet:s3cret%zzpw@db.acme.lan:5432/latet", "% that starts no escape"),
        ("database_url", "postgresql://latet:s3cret%00pw@db.acme.lan:5432/latet", "%00"),
        ("database_url", "postgresql://latet@db.acme.lan:5432/latet?password=s3cret%zz", "% that starts no escape"),
        ("database_url", "postgresql://latet:s3cret pw@db.acme.lan:5432/latet", "a space"),
        ("database_url", " postgresql://latet:s3cret-pw@db.acme.lan:5432/latet", "a space"),
        ("database_url", "postgresql://latet:s3cret\tpw@db.acme.lan:5432/latet", "a space"),
        ("database_url", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet\n", "a space"),
        ("database_url", "postgresql://latet:s3cret/pw@db.acme.lan:5432/latet", "@ after its host"),
        ("database_url", "postgresql://latet:s3cret?pw@db.acme.lan:5432/latet", "@ after its host"),
        ("database_url", "postgresql://latet:s3cret-pw@db.acme.lan:54x32/latet", "a port that cannot be read"),
        ("database_url", "postgresql://latet:s3cret@pw@db.acme.lan:5432/latet", "@ more than once"),
        ("database_url", "postgresql://la@tet:s3cret-pw@db.acme.lan:5432/latet", "@ more than once"),
        ("database_url", "postgresql://latet:12345/pw@db.acme.lan:5432/latet", "@ after its host"),
        # Read by libpq, and not by urlsplit: refused with the way to write it that both read.
        (
            "database_url",
            "postgresql://latet:s3cret-pw@db1.acme.lan:5432,db2.acme.lan:5433/latet",
            "?host=a,b&port=1,2",
        ),
        ("valkey_url", "redis://:v4lkey%pw@cache.acme.lan:6379/0", "% that starts no escape"),
        ("valkey_url", "redis://:v4lkey pw@cache.acme.lan:6379/0", "a space"),
        ("valkey_url", "redis://:v4lkey/pw@cache.acme.lan:6379/0", "@ after its host"),
        ("valkey_url", "redis://:v4lkey-pw@cache.acme.lan:63x79/0", "a port that cannot be read"),
        ("valkey_url", "redis://:12345/pw@cache.acme.lan:6379/0", "@ after its host"),
        # Read alike by urlsplit and redis-py, at the last @: one way to write an address all the same.
        ("valkey_url", "redis://:v4lkey@pw@cache.acme.lan:6379/0", "@ more than once"),
        ("valkey_url", "redis://default:v4lkey-pw@cache.acme.lan:6379@/0", "@ more than once"),
    ],
)
def test_an_address_its_driver_would_not_read_is_refused_in_latets_own_words(setting, address, words):
    """libpq says ``invalid percent-encoded token: "<the password>"`` and the like, or reads another host out of the
    address than Latet does; redis-py ``Port could not be cast to integer value as '<half the password>'``."""
    with pytest.raises(ValidationError, match=rf"{setting} [^\n]*{re.escape(words)}") as refused:
        _settings(**{setting: address})
    assert _holds_no_password(refused.value), _shown(refused.value)


def test_what_the_settings_accept_each_driver_reads_as_latet_does(tmp_path, monkeypatch):
    conninfo_to_dict = pytest.importorskip("psycopg.conninfo").conninfo_to_dict  # libpq's own parser: no connection
    parse_url = pytest.importorskip("redis.connection").parse_url
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    password = "p@ss:w/rd#?%20 &=+[]{}|^~<>;,!$*()\N{LATIN SMALL LETTER E WITH ACUTE}"  # whatever a file holds
    (tmp_path / "db.pw").write_text(password, "utf-8")
    (tmp_path / "valkey.pw").write_text(password, "utf-8")
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full")
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", "rediss://default@cache.acme.lan:6380/2")
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(tmp_path / "db.pw"))
    monkeypatch.setenv("LATET_SERVER_VALKEY_PASSWORD_FILE", str(tmp_path / "valkey.pw"))
    found = load_server_settings()
    read = conninfo_to_dict(found.database_url)
    assert (read["user"], read["password"], read["host"], read["port"], read["dbname"], read["sslmode"]) == (
        "latet", password, "db.acme.lan", "5432", "latet", "verify-full"
    )  # fmt: skip
    read = parse_url(found.valkey_url)
    assert (read["username"], read["password"], read["host"], read["port"], read["db"]) == (
        "default", password, "cache.acme.lan", 6380, 2
    )  # fmt: skip
    hosts = "postgresql://latet:s3cret-pw@/latet?host=db1.acme.lan,db2.acme.lan&port=5432,5433&sslmode=verify-ca"
    read = conninfo_to_dict(_settings(database_url=hosts).database_url)
    assert (read["host"], read["port"], read["password"]) == ("db1.acme.lan,db2.acme.lan", "5432,5433", "s3cret-pw")


def _mac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def test_one_purpose_gives_each_use_a_key_of_its_own_and_nothing_opens_another_way():
    key = b"\x05" * 32
    master, sealer = MasterKey([key]), Sealer(key, b"bus")
    sealing, keyed, derived = _mac(key, b"seal:bus"), _mac(key, b"keyed:bus"), _mac(key, b"derive:bus")
    assert master.derive(b"bus") == derived and sealer.keyed("Orion") == _mac(keyed, b"Orion")
    blob = master.sealer(b"bus").seal(b"x", "a")
    assert AESGCM(sealing).decrypt(blob[2:14], blob[14:], b"\x01\x01a") == b"x"
    # Not the key of another use of that purpose, not the master key as it is, not a key made without naming the use.
    for other in (master.derive(b"bus"), keyed, key, _mac(key, b"bus"), _mac(key, b"bus-keyed")):
        with pytest.raises(InvalidTag):
            AESGCM(other).decrypt(blob[2:14], blob[14:], b"\x01\x01a")
    own = master.seal(b"x", "a")  # what the master key seals itself, under a purpose no sealer of it is given for
    for other in (key, sealing, derived, _mac(key, b"seal:")):
        with pytest.raises(InvalidTag):
            AESGCM(other).decrypt(own[2:14], own[14:], b"\x01\x01a")
    with pytest.raises(InvalidTag):
        master.sealer(b"bus").open(own, "a")
    with pytest.raises(InvalidTag):
        master.open(blob, "a")
    with pytest.raises(ValueError, match="the master key's own"):
        master.sealer(b"master")


def test_the_sealed_format_is_what_it_was(monkeypatch):
    """Known answers, made with HMAC-SHA256 and AES-GCM alone: what was sealed must open after any change here, so a
    change of a tag, of the frame or of what a value is bound to has to fail this first."""
    urandom, here, nonce = os.urandom, threading.get_ident(), bytes(range(12))
    monkeypatch.setattr(os, "urandom", lambda size: nonce if threading.get_ident() == here else urandom(size))
    sealer, master = Sealer(ROOT_KEY, b"storage"), MasterKey([b"\x05" * 32])
    assert sealer.seal(b"emily.carter@acme.com", "u_emilycarte:x").hex() == (
        "0101000102030405060708090a0b669e25a17be4e5c74add51aa9c321a942a07ca66a80c983050425665ca8c1bdb3879c82ecb"
    )
    assert sealer.keyed("Orion").hex() == "0669dd6822f19e4f19b638c3a24f871043729eaf75ac5da457f6a0fcbdfde74b"
    assert master.derive(b"bus").hex() == "d8f098298281aad63eec205f47dcd6216af3462fa7834b7afa8f6ab4037e0e4c"
    assert master.seal(b"root key of u_emilycarte", "root-key:u_emilycarte").hex() == (
        "0101000102030405060708090a0b0112eefe23432a12aba0e49de750e8da4d7c5951970e5895d7a369b7c813ecdee2dbb4319db6779f"
    )
    # A second version of each key: its frame says 01 02 and is bound to 01 02, and it is the last key, not the
    # first, that seals, hashes and is derived from.
    sealer, master = Sealer([ROOT_KEY, b"\x08" * 32], b"storage"), MasterKey([b"\x05" * 32, b"\x06" * 32])
    assert sealer.seal(b"emily.carter@acme.com", "u_emilycarte:x").hex() == (
        "0102000102030405060708090a0ba55c60298abb5bbeb6547a53c9a2daf2ca122d3e58fdc77fef297216b5c170bedd853af675"
    )
    assert sealer.keyed("Orion").hex() == "75f314fc26ab070fa22ac59fc120e6868be61f694590cc6302d957616e180b36"
    assert master.derive(b"bus").hex() == "ebf835705bbd471779fe2e870c2974443be485f0725e6c5f9f7f4a9d8fb4dd1c"
    assert master.seal(b"root key of u_emilycarte", "root-key:u_emilycarte").hex() == (
        "0102000102030405060708090a0bfe46d8b1eb99ba88925304fba9e808c7a9d386707545972b0edd631d60759bf6f33d28f125d0088a"
    )


def test_the_master_key_given_by_its_variable_and_by_its_file_is_refused(tmp_path, monkeypatch):
    first, second = MasterKey.generate(), MasterKey.generate()
    (tmp_path / "master.key").write_text(first + "\n", "utf-8")
    monkeypatch.setenv("LATET_MASTER_KEY", second)
    with pytest.raises(MasterKeyError, match=r"LATET_MASTER_KEY and .*LATET_MASTER_KEY_FILE") as refused:
        MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))  # which of the two it is, is not guessed
    assert first not in _shown(refused.value) and second not in _shown(refused.value)
    assert _holds_its_words_alone(refused.value)


def test_a_variable_of_nothing_but_spaces_gives_no_master_key(tmp_path, monkeypatch):
    key = MasterKey.generate()
    (tmp_path / "master.key").write_text(key + "\n", "utf-8")
    sealed = MasterKey([base64.b64decode(key)]).seal(b"x", "root-key:u_emilycarte")
    monkeypatch.setenv("LATET_MASTER_KEY", " \n")
    master = MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))  # the file's key is read
    assert master.open(sealed, "root-key:u_emilycarte") == b"x"
    monkeypatch.setenv("LATET_MASTER_KEY", f"  {key}\n")  # and the spaces around a key are no part of it
    assert MasterKey.load(_settings()).open(sealed, "root-key:u_emilycarte") == b"x"


def test_the_master_keys_variable_holds_one_key(monkeypatch):
    first, second = MasterKey.generate(), MasterKey.generate()
    monkeypatch.setenv("LATET_MASTER_KEY", f"{first}\n{second}")
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY holds one key") as refused:
        MasterKey.load(_settings())  # its versions live in the file
    assert first not in _shown(refused.value) and second not in _shown(refused.value)
    assert _holds_its_words_alone(refused.value)


def test_a_master_key_file_that_holds_one_key_twice_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    key = MasterKey.generate()
    (tmp_path / "master.key").write_text(f"{key}\n{MasterKey.generate()}\n{key}\n", "utf-8")
    with pytest.raises(MasterKeyError, match="twice") as refused:
        MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))  # two versions would open each other's
    assert key not in _shown(refused.value)


@pytest.mark.parametrize(
    ("make", "error"), [(MasterKey, MasterKeyError), (lambda keys: Sealer(keys, b"storage"), ValueError)]
)
def test_a_key_given_twice_is_refused_by_what_would_hold_it(make, error):
    with pytest.raises(error, match="twice"):
        make([ROOT_KEY, b"\x08" * 32, ROOT_KEY])


def test_the_frames_two_bytes_are_sealed_with_the_value():
    sealer = Sealer([ROOT_KEY, b"\x08" * 32], b"storage")
    sealer._aeads[2] = sealer._aeads[1]  # as if two versions held one key, which neither constructor lets happen
    older = Sealer(ROOT_KEY, b"storage").seal(b"x", "u_emilycarte:x")
    assert sealer.open(older, "u_emilycarte:x") == b"x"
    with pytest.raises(InvalidTag):
        sealer.open(older[:1] + b"\x02" + older[2:], "u_emilycarte:x")  # said to be of version 2: with one key, even


@pytest.mark.parametrize(
    ("make", "error"), [(MasterKey, MasterKeyError), (lambda keys: Sealer(keys, b"storage"), ValueError)]
)
def test_more_versions_than_a_sealed_value_can_name_are_refused_at_once(make, error):
    keys = [bytes([n // 256, n % 256]) * 16 for n in range(256)]
    with pytest.raises(error, match="255"):
        make(keys)  # not at the first seal
    most = make(keys[:255])
    sealed = most.seal(b"x", "root-key:u_emilycarte")
    assert (most.version, sealed[1]) == (255, 255) and most.open(sealed, "root-key:u_emilycarte") == b"x"


def _raised(call) -> Exception:
    """What ``call`` raises, to look at what the error holds beyond its words."""
    try:
        call()
    except Exception as error:
        return error
    pytest.fail("nothing was raised")


def _holds_its_words_alone(error: Exception) -> bool:
    """No error it was raised from or while another was handled, and no list of what was refused (pydantic's
    ``errors()`` and ``json()`` keep the refused value): nothing leads from it to a password, a key or a file."""
    plain = type(error) in (ValueError, MasterKeyError)
    return plain and error.__cause__ is None and error.__context__ is None and not hasattr(error, "errors")


def test_a_start_up_error_holds_its_words_and_nothing_to_follow(tmp_path, monkeypatch):
    for name in (*_VARIABLES, "LATET_MASTER_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings_file, password_file, key_file = tmp_path / "server.yaml", tmp_path / "db.pw", tmp_path / "master.key"
    errors = []
    settings_file.write_text(
        f"url: {URL}\ndatabase_url: postgre://latet:s3cret-pw@db/l\nvalkey_url: {VALKEY}\n", "utf-8"
    )
    errors.append(_raised(lambda: load_server_settings(settings_file)))  # pydantic's error keeps what it refused
    settings_file.write_text(f"url: {URL}\ndatabase_url: {DATABASE}: oops\n", "utf-8")
    errors.append(_raised(lambda: load_server_settings(settings_file)))  # PyYAML's error quotes the line
    settings_file.write_bytes(f"database_url: {DATABASE}\nurl: ".encode() + b"\xff\n")
    errors.append(_raised(lambda: load_server_settings(settings_file)))  # the decoding error holds the file
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", "postgresql://latet@db.acme.lan:5432/latet")
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", VALKEY)
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", "s3cret-pw")
    errors.append(_raised(load_server_settings))  # the read's error holds the file's name: here, the password
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(password_file))
    password_file.write_bytes(b"s3cret-pw\xff\n")
    errors.append(_raised(load_server_settings))
    password_file.write_text("v4lkey-pw\n", "utf-8")
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", f"postgresql://latet:s3cret{ODD}pw@db.acme.lan:5432/latet")
    errors.append(_raised(load_server_settings))  # urlsplit's error quotes the address
    errors.append(_raised(lambda: with_password("postgresql://latet:s3cret/pw@db.acme.lan/latet", "v4lkey-pw")))
    settings = _settings(master_key_file=key_file)
    errors.append(_raised(lambda: MasterKey.load(settings)))  # no such file: the read's error holds its name
    key_file.write_bytes(bytes(range(0xC0, 0xE0)))
    errors.append(_raised(lambda: MasterKey.load(settings)))  # the decoding error holds the key's bytes
    key_file.write_text("s3cret-pw is not a key\n", "utf-8")
    errors.append(_raised(lambda: MasterKey.load(settings)))
    errors.append(_raised(lambda: MasterKey([ROOT_KEY]).open(b"not a sealed value, whatever its length", "a")))
    assert len(errors) == 11
    followed = [n for n, error in enumerate(errors) if not _holds_its_words_alone(error)]
    assert not followed, [(n, type(errors[n]).__name__, type(errors[n].__context__).__name__) for n in followed]
    assert all(_holds_no_password(error) for error in errors)


def test_no_setting_is_quoted_by_the_error_that_refuses_it(tmp_path, monkeypatch):
    for name in server_settings._VARIABLES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LATET_SERVER_CONFIG", raising=False)
    path, refused = tmp_path / "server.yaml", set()
    for setting in ServerSettings.model_fields:  # each in turn holds what could be a secret put in the wrong place
        given = {"url": URL, "database_url": DATABASE, "valkey_url": VALKEY} | {setting: "s3cret-canary"}
        path.write_text("".join(f"{key}: {value}\n" for key, value in given.items()), "utf-8")
        try:
            load_server_settings(path)
        except ValueError as error:
            refused.add(setting)
            shown = _shown(error)
        else:
            shown = ""
        assert "canary" not in shown, setting
    assert refused >= {"url", "database_url", "valkey_url", "port", "certificate", "log_format", "max_request_mb"}
    assert refused >= {
        "database_password_file",
        "valkey_password_file",
        "valkey_unauthenticated",
        "forwarded_allow_ips",
    }


def _passwords_in_files(monkeypatch, folder, *, database="postgresql://latet@db.acme.lan:5432/latet" + TLS, valkey=""):
    """The variables of a server whose two passwords are in ``folder``'s db.pw and valkey.pw, which the test writes."""
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", database)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", valkey or "rediss://cache.acme.lan:6379/0")
    monkeypatch.setenv("LATET_SERVER_DATABASE_PASSWORD_FILE", str(folder / "db.pw"))
    monkeypatch.setenv("LATET_SERVER_VALKEY_PASSWORD_FILE", str(folder / "valkey.pw"))


@pytest.mark.parametrize(
    ("empty", "setting"), [("db.pw", "database_password_file"), ("valkey.pw", "valkey_password_file")]
)
def test_an_empty_password_file_stops_the_start(tmp_path, monkeypatch, empty, setting):
    _passwords_in_files(monkeypatch, tmp_path)
    (tmp_path / "db.pw").write_text("s3cret-pw\n", "utf-8")
    (tmp_path / "valkey.pw").write_text("v4lkey-pw\n", "utf-8")
    (tmp_path / empty).write_text(" \n", "utf-8")
    with pytest.raises(ValueError, match=f"{setting} names a file that holds no password") as refused:
        load_server_settings()  # not a start without the password that was meant
    assert _holds_its_words_alone(refused.value)


@pytest.mark.parametrize(
    ("setting", "address"),
    [
        ("database", DATABASE),
        (
            "database",
            "postgresql://latet@db.acme.lan:5432/latet?password=s3cret-pw",
        ),  # which libpq takes over the file's
        ("database", "postgresql://latet@db.acme.lan:5432/latet?password="),  # an empty one too
        ("valkey", VALKEY),
        ("valkey", "redis://cache.acme.lan:6379/0?password=v4lkey-pw"),  # which redis-py drops for the file's
    ],
)
def test_a_password_in_the_address_and_in_a_file_stops_the_start(tmp_path, monkeypatch, setting, address):
    _passwords_in_files(monkeypatch, tmp_path, **{setting: address})
    (tmp_path / "db.pw").write_text("another\n", "utf-8")
    (tmp_path / "valkey.pw").write_text("another\n", "utf-8")
    with pytest.raises(ValueError, match=f"{setting}_url holds a password and {setting}_password_file") as refused:
        load_server_settings()  # which of the two is meant is not guessed
    assert _holds_no_password(refused.value) and "another" not in _shown(refused.value)
    assert _holds_its_words_alone(refused.value)


MARK = b"\xef\xbb\xbf"  # the byte order mark that Windows PowerShell 5.1 puts first in a file it writes as UTF-8


def test_a_master_key_file_may_start_with_a_byte_order_mark(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    key = MasterKey.generate()
    (tmp_path / "master.key").write_bytes(MARK + key.encode() + b"\r\n")
    master = MasterKey.load(_settings(master_key_file=tmp_path / "master.key"))
    sealed = MasterKey([base64.b64decode(key)]).seal(b"x", "root-key:u_emilycarte")
    assert master.open(sealed, "root-key:u_emilycarte") == b"x"


def test_a_password_file_may_start_with_a_byte_order_mark(tmp_path, monkeypatch):
    _passwords_in_files(monkeypatch, tmp_path)
    (tmp_path / "db.pw").write_bytes(MARK + b"s3cret-pw\r\n")
    (tmp_path / "valkey.pw").write_bytes(MARK + b"v4lkey-pw\r\n")
    found = load_server_settings()
    assert (found.database_url, found.valkey_url) == (DATABASE, VALKEY)  # the passwords, and no mark in them
    (tmp_path / "server.yaml").write_bytes(MARK + b"port: 6000\n")  # in server.yaml too, where PyYAML drops it
    assert load_server_settings(tmp_path / "server.yaml").port == 6000


def test_a_password_file_that_is_not_plain_text_is_said_to_be_the_file(tmp_path, monkeypatch):
    _passwords_in_files(monkeypatch, tmp_path)
    (tmp_path / "valkey.pw").write_text("v4lkey-pw\n", "utf-8")
    (tmp_path / "db.pw").write_bytes("s3cret-pw".encode("utf-16-le"))  # UTF-16 without its mark: a zero byte a letter
    error = _raised(load_server_settings)
    assert re.search("database_password_file names a file .*zero byte", str(error)), str(error)
    assert "database_url" not in str(error)  # not the address, which holds no escape that anyone wrote
    assert _holds_its_words_alone(error) and _holds_no_password(error), _shown(error)
    (tmp_path / "db.pw").write_text("s3cret-pw\nand a second line\n", "utf-8")
    error = _raised(load_server_settings)
    assert re.search("database_password_file names a file .*one line", str(error)), str(error)
    assert _holds_its_words_alone(error) and _holds_no_password(error), _shown(error)


def _start(monkeypatch, *, database=DATABASE, valkey=VALKEY) -> Exception:
    """What the start raises for these two addresses, given as its variables give them."""
    for name in server_settings._VARIABLES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LATET_SERVER_CONFIG", raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_DATABASE_URL", database)
    monkeypatch.setenv("LATET_SERVER_VALKEY_URL", valkey)
    return _raised(load_server_settings)


LONE = chr(0xDCE9)  # the byte e9 of a variable that is not UTF-8, as Python reads it on Linux: a lone surrogate
# Addresses that a driver reads otherwise than Latet, or refuses with an error that holds them: the setting, the
# address, and words of the sentence that refuses it.
READ_OTHERWISE = [
    # A capital in the scheme, or one slash: Python reads it in small letters, a driver as it is written, and libpq
    # then takes the whole for another kind of text and quotes it.
    ("database", "PostgreSQL://latet:s3cret-pw@db.acme.lan:5432/latet", "starts with postgresql:// or postgres://"),
    ("database", "POSTGRES://latet:s3cret-pw@db.acme.lan:5432/latet", "starts with postgresql:// or postgres://"),
    ("database", "postgresql:/latet:s3cret-pw@db.acme.lan:5432/latet", "starts with postgresql:// or postgres://"),
    ("valkey", "Redis://:v4lkey-pw@cache.acme.lan:6379/0", "starts with redis://"),
    ("valkey", "VALKEY://:v4lkey-pw@cache.acme.lan:6379/0", "starts with redis://"),
    # An @ of the password and then / ? or #: the address ends there for both readers, which then send what is before
    # that @ to the host named after it.
    ("valkey", "redis://:v4lkey@pw#2026@cache.acme.lan:6379/0", "@ after its host"),
    ("valkey", "redis://:v4lkey@pw/2026@cache.acme.lan:6379/0", "@ after its host"),
    ("valkey", "redis://:v4lkey@pw?2026@cache.acme.lan:6379/0", "@ after its host"),
    ("valkey", "redis://:v4lkey-pw@cache.acme.lan:6379/0?client_name=latet@acme", "@ after its host"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?application_name=latet@acme", "@ after its host"),
    # What follows a # is nothing to urlsplit, and the database's name and its parameters to libpq.
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet#?password=an0ther", "holds #"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet#?host=other.example", "holds #"),
    ("database", "postgresql://latet:s3cret-pw@[2001:db8::5]#:5432/latet", "holds #"),
    # A bracket of the password, which closes or opens the host's for one reader and not for the other.
    ("database", "postgresql://latet:s3cret]pw@[::1/latet", "%5B and %5D"),
    ("database", "postgresql://latet:s3cret[pw@db.acme.lan:5432/latet", "%5B and %5D"),
    ("database", "postgresql://la[tet:s3cret-pw@db.acme.lan:5432/latet", "%5B and %5D"),
    ("valkey", "redis://:v4lkey]pw@[::1/0", "%5B and %5D"),
    # A host in brackets that is not an address between them, and no more.
    ("database", "postgresql://latet:s3cret-pw@[::1]x:5432/latet", "in brackets"),
    ("database", "postgresql://latet:s3cret-pw@[]:5432/latet", "in brackets"),
    ("database", "postgresql://latet:s3cret-pw@[db.acme.lan]:5432/latet", "in brackets"),
    ("database", "postgresql://latet:s3cret-pw@[::1:5432/latet", "in brackets"),
    ("valkey", "redis://:v4lkey-pw@[::1]6379/0", "in brackets"),
    # An escape in the host, which a driver reads as its character and urlsplit as it is written.
    ("database", "postgresql://latet:s3cret-pw@db.acme.%6can:5432/latet", "% in its host"),
    ("valkey", "redis://:v4lkey-pw@cache.acme.%6can:6379/0", "% in its host"),
    # A parameter that is not name=value, which libpq quotes whole to say so: a colon typed for the equals sign.
    ("database", "postgresql://latet@db.acme.lan:5432/latet?password:s3cret-pw", "not name=value"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=require&&connect_timeout=5", "not name=value"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?SSLMode=require", "not name=value"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=verify-full&%73sl=true", "not name=value"),
    # libpq writes requiressl, and in a URI ssl=true, into sslmode, the last parameter written winning: the sslmode of
    # such an address is not the one written (sslmode=verify-full&requiressl=0 is prefer, &ssl=true is require).
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=verify-full&requiressl=0", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=verify-full&requiressl=1", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=verify-full&ssl=true", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?ssl=true&sslmode=verify-full", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system&ssl=true", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet?ssl=1", "ssl or requiressl"),
    ("database", "postgresql://latet:s3cret-pw@127.0.0.1:5432/latet?requiressl=0", "ssl or requiressl"),
    # Two hex digits after a % that are no UTF-8 text: psycopg reads the address with an error that holds what the
    # escape was in, UnicodeDecodeError('utf-8', b'<the password>', ...); redis-py puts another character there.
    ("database", "postgresql://latet:s3cret%dbpw@db.acme.lan:5432/latet", "a % escape that is not UTF-8 text"),
    ("database", "postgresql://latet:s3cr%e9t-pw@db.acme.lan:5432/latet", "a % escape that is not UTF-8 text"),
    ("database", "postgresql://latet@db.acme.lan:5432/latet?password=s3cret%dbpw", "a % escape that is not UTF-8 text"),
    ("valkey", "redis://:v4lkey%dbpw@cache.acme.lan:6379/0", "a % escape that is not UTF-8 text"),
    # A variable set in another encoding: a driver's error for what it cannot write as UTF-8 holds the address.
    ("database", f"postgresql://latet:s3cr{LONE}t-pw@db.acme.lan:5432/latet", "is not UTF-8 text: what sets it"),
    ("valkey", f"redis://:v4lk{LONE}y-pw@cache.acme.lan:6379/0", "is not UTF-8 text: what sets it"),
    # Where Valkey is said twice: the address's host decides whether a password is asked, no parameter after it.
    ("valkey", "unix:///run/valkey.sock?host=cache.acme.lan", "among its parameters"),
    ("valkey", "redis://127.0.0.1:6379/0?host=cache.acme.lan&port=6380", "among its parameters"),
    ("valkey", "redis://:v4lkey-pw@cache.acme.lan/0?path=/run/valkey.sock", "among its parameters"),
    # The colon before a password left out: the password is then a user's name, which no password follows.
    ("valkey", "redis://v4lkey-pw@127.0.0.1:6379/0", "a user's name and no password"),
    ("valkey", "redis://default:@127.0.0.1:6379/0", "a user's name and no password"),
]  # fmt: skip
# Addresses that hold no password, as they are written beside a password file. Put together again around the file's
# password, one whose scheme lacks its two slashes came out with them and no host (so this machine, which would have
# been sent that password), and one that ends in # came out without it: each is judged before, as it is written.
BESIDE_A_FILE = [
    ("database", "PostgreSQL://latet@db.acme.lan:5432/latet", "starts with postgresql:// or postgres://"),
    ("database", " postgresql://latet@db.acme.lan:5432/latet", "a space"),
    ("database", "postgresql://latet@db.acme.lan:5432/latet#?password=an0ther", "holds #"),
    ("database", "postgresql:/db.acme.lan/latet", "starts with postgresql:// or postgres://"),
    ("database", "postgresql:db.acme.lan/latet", "starts with postgresql:// or postgres://"),
    ("database", "postgresql:/latet@db.acme.lan:5432/latet", "starts with postgresql:// or postgres://"),
    ("database", "postgresql://latet@db.acme.lan:5432/latet#", "holds #"),
    ("valkey", "redis:/cache.acme.lan:6379/0", "starts with redis://"),
    ("valkey", "rediss:/default@cache.acme.lan:6380/2", "starts with redis://"),
    ("valkey", "unix:/run/valkey.sock", "starts with redis://"),
]
# No part of a refused address in what refuses it: of its password, of its host, of what it holds after a #.
PARTS = ("s3cr", "v4lk", "-pw", "f1le", "2026", "an0ther", "other.example", "acme", "::1", "2001")


@pytest.mark.parametrize(("setting", "address", "words"), READ_OTHERWISE)
def test_an_address_its_driver_reads_otherwise_stops_the_start_in_words_alone(monkeypatch, setting, address, words):
    error = _start(monkeypatch, **{setting: address})
    assert re.search(rf"{setting}_url [^\n]*{re.escape(words)}", str(error)), str(error)
    assert _holds_its_words_alone(error) and _holds_no_password(error), _shown(error)
    assert not any(part in _shown(error) for part in PARTS)


@pytest.mark.parametrize(
    ("setting", "address", "words"),
    # With a file, a user's name may stand without its password: the file's follows it.
    [*BESIDE_A_FILE, *(case for case in READ_OTHERWISE if "no password" not in case[2])],
)
def test_an_address_beside_a_password_file_is_judged_as_it_is_written(tmp_path, monkeypatch, setting, address, words):
    _passwords_in_files(monkeypatch, tmp_path, **{setting: address})  # the password put in must not put the rest right
    (tmp_path / "db.pw").write_text("f1le-pw\n", "utf-8")
    (tmp_path / "valkey.pw").write_text("f1le-pw\n", "utf-8")
    error = _raised(load_server_settings)
    assert re.search(rf"{setting}_url [^\n]*{re.escape(words)}", str(error)), str(error)  # the words it has alone
    assert _holds_its_words_alone(error) and _holds_no_password(error), _shown(error)
    assert not any(part in _shown(error) for part in PARTS)


def test_an_address_in_its_usual_forms_is_taken_as_it_is(monkeypatch):
    for database, valkey in (
        (DATABASE, VALKEY),
        ("postgres://latet:s3cret-pw@[2001:db8::5]:5432/latet?sslmode=verify-full", "rediss://default:v4lkey-pw@[::1]:6380/2"),
        ("postgresql://latet:s3cret%40pw%23%5B%5D%2F@db1.acme.lan,db2.acme.lan/latet?sslmode=verify-ca", "valkeys://:v4lkey%40pw%23@cache.acme.lan"),
        ("postgresql:///latet?host=/var/run/postgresql", "unix:///run/valkey.sock?db=0"),
        ("postgresql://latet@/latet?host=db1.acme.lan,db2.acme.lan&port=5432,5433&sslmode=verify-full", "redis://127.0.0.1"),
    ):  # fmt: skip
        for name in server_settings._VARIABLES.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("LATET_SERVER_URL", URL)
        monkeypatch.setenv("LATET_SERVER_DATABASE_URL", database)
        monkeypatch.setenv("LATET_SERVER_VALKEY_URL", valkey)
        found = load_server_settings()
        assert (found.database_url, found.valkey_url) == (database, valkey)
    letter = "\N{LATIN SMALL LETTER E WITH ACUTE}"  # a letter as it is, and as the escapes of its two UTF-8 bytes
    database, valkey = (
        f"postgresql://latet:s3cr{letter}t-pw@db.acme.lan/latet" + TLS,
        "rediss://:v4lk%C3%A9y-pw@cache.acme.lan/0",
    )
    found = _settings(database_url=database, valkey_url=valkey)
    assert (found.database_url, found.valkey_url) == (database, valkey)


def test_how_long_the_database_is_waited_for_is_a_start_up_setting_with_a_short_default(tmp_path, monkeypatch):
    for name in (*server_settings._VARIABLES.values(), "LATET_SERVER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    assert _settings().database_connect_timeout == 5
    for wrong in (0, 1, 121):  # libpq takes one second for two, and psycopg takes none for 130
        with pytest.raises(ValidationError):
            _settings(database_connect_timeout=wrong)
    path = tmp_path / "server.yaml"
    path.write_text(
        f"url: {URL}\ndatabase_url: {DATABASE}\nvalkey_url: {VALKEY}\ndatabase_connect_timeout: 8\n", "utf-8"
    )
    assert load_server_settings(path).database_connect_timeout == 8
    monkeypatch.setenv("LATET_SERVER_DATABASE_CONNECT_TIMEOUT", "3")
    assert load_server_settings(path).database_connect_timeout == 3


def test_how_much_one_identitys_history_may_keep_is_a_start_up_setting_of_a_gibibyte(tmp_path, monkeypatch):
    for name in (*server_settings._VARIABLES.values(), "LATET_SERVER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    assert _settings().history_limit_mb == 1024
    for wrong in (0, -1, 1_048_577):
        with pytest.raises(ValidationError):
            _settings(history_limit_mb=wrong)
    path = tmp_path / "server.yaml"
    path.write_text(f"url: {URL}\ndatabase_url: {DATABASE}\nvalkey_url: {VALKEY}\nhistory_limit_mb: 64\n", "utf-8")
    assert load_server_settings(path).history_limit_mb == 64
    monkeypatch.setenv("LATET_SERVER_HISTORY_LIMIT_MB", "256")
    assert load_server_settings(path).history_limit_mb == 256


def test_the_requests_one_identity_has_in_flight_on_an_instance_are_a_start_up_setting_of_sixteen(
    tmp_path, monkeypatch
):
    for name in (*server_settings._VARIABLES.values(), "LATET_SERVER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    assert _settings().requests_in_flight == 16
    for wrong in (0, -1, 1_025):
        with pytest.raises(ValidationError):
            _settings(requests_in_flight=wrong)
    path = tmp_path / "server.yaml"
    path.write_text(f"url: {URL}\ndatabase_url: {DATABASE}\nvalkey_url: {VALKEY}\nrequests_in_flight: 4\n", "utf-8")
    assert load_server_settings(path).requests_in_flight == 4
    monkeypatch.setenv("LATET_SERVER_REQUESTS_IN_FLIGHT", "32")
    assert load_server_settings(path).requests_in_flight == 32


# S6: off this machine PostgreSQL and Valkey are reached over TLS that checks the server, or the settings say that the
# network is the instances' alone. A name that has no part in the sentences, so that a part of the address shows.
OFF = "pg-7f3a.corp.example"
OFF_PARTS = ("pg-7f3a", "corp", "example", "s3cret", "v4lkey", "cache-9c1d")
CHECKED = "?sslmode=verify-full&sslrootcert=system"


def _refusal_of(**changes) -> str:
    """The words of the refusal, after checking they hold no part of the address and no chain to follow back to it."""
    with pytest.raises(ValidationError) as refused:
        _settings(**changes)
    shown = _shown(refused.value)
    assert not any(part in shown for part in OFF_PARTS), shown
    return str(refused.value)


def test_here_is_one_test_for_every_address_of_a_server():
    here = ("localhost", "LocalHost", "127.0.0.1", "::1", "/var/run/postgresql", "/tmp")
    elsewhere = ("db.acme.lan", "127.0.0.2", "0.0.0.0", "", "localhost.evil.example", "::2", "10.0.0.5")
    assert all(server_settings._here(host) for host in here)
    assert not any(server_settings._here(host) for host in elsewhere)


@pytest.mark.parametrize(
    "address",
    [
        f"postgresql://latet:s3cret-pw@{OFF}:5432/latet",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=require",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=prefer",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=allow",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=disable",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=Verify-Full",  # libpq reads the words as they are written
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=verify_full",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=verify-full&sslmode=disable",  # the last of them is read
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=System",  # only system is system
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=system&sslrootcert=%2Fetc%2Fssl%2Fca.pem",  # the last is read
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=%2Fetc%2Fssl%2Fca.pem",  # a file: the mode is still prefer
        "postgresql://latet:s3cret-pw@[2001:db8::5]:5432/latet?application_name=pg-7f3a",
        f"postgresql://latet:s3cret-pw@127.0.0.1,{OFF}/latet",  # one place of the list is elsewhere
        "postgresql://latet:s3cret-pw@localhost/latet?hostaddr=10.0.0.5",  # it connects to the address it is given
        f"postgresql://latet:s3cret-pw@/latet?host=127.0.0.1,{OFF}",
        f"postgresql://latet:s3cret-pw@127.0.0.1/latet?host={OFF}",  # the parameter replaces the host
        "postgresql://latet:s3cret-pw@/latet?hostaddr=10.0.0.5",
    ],
)
def test_postgresql_off_this_machine_needs_a_tls_that_checks_the_server_or_the_networks_word(address):
    words = _refusal_of(database_url=address)
    assert "database_url" in words and "LATET_SERVER_DATABASE_PRIVATE_NETWORK" in words
    assert _settings(database_url=address, database_private_network=True).database_private_network  # said so


@pytest.mark.parametrize(
    "address",
    [
        "postgresql://latet:s3cret-pw@127.0.0.1:5432/latet",
        "postgresql://latet:s3cret-pw@[::1]:5432/latet",
        "postgresql://latet:s3cret-pw@localhost/latet",
        "postgresql://latet:s3cret-pw@LOCALHOST/latet",
        "postgresql://latet:s3cret-pw@/latet?host=/var/run/postgresql",
        "postgresql://latet:s3cret-pw@127.0.0.1,localhost:5433/latet",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?hostaddr=127.0.0.1",  # the name, and the connection goes to loopback
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=verify-full",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=verify-ca&sslrootcert=%2Fetc%2Fssl%2Fca.pem",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslmode=disable&sslmode=verify-full",  # the last of them is read
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=system",  # libpq makes verify-full of it, as it is written
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=%73ystem",
        f"postgresql://latet:s3cret-pw@{OFF}/latet?sslrootcert=%2Fetc%2Fssl%2Fca.pem&sslrootcert=system",  # the last
        f"postgresql://latet:s3cret-pw@{OFF},{OFF}/latet?sslmode=verify-full",
        f"postgresql://latet:s3cret-pw@/latet?host={OFF}&sslmode=verify%2Dfull",
        "postgresql://latet:s3cret-pw@[2001:db8::5]/latet?sslmode=verify-full",
    ],
)
def test_postgresql_on_this_machine_or_over_a_checked_tls_needs_nothing_more(address):
    assert _settings(database_url=address).database_url == address


@pytest.mark.parametrize(
    "address",
    [
        "redis://:v4lkey-pw@cache-9c1d.corp.example:6379/0",
        "valkey://:v4lkey-pw@cache-9c1d.corp.example",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=none",
        "valkeys://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=optional",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=Required",  # an error to redis-py, at connect
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=none&ssl_cert_reqs=required",  # the first is read
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=false",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=0",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=No",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=f",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=n",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=FALSE&ssl_check_hostname=true",
    ],
)
def test_valkey_off_this_machine_needs_a_tls_that_checks_the_server_or_the_networks_word(address):
    words = _refusal_of(valkey_url=address)
    assert "valkey_url" in words and "LATET_SERVER_VALKEY_PRIVATE_NETWORK" in words
    assert _settings(valkey_url=address, valkey_private_network=True).valkey_private_network  # said so


@pytest.mark.parametrize(
    "address",
    [
        "redis://:v4lkey-pw@127.0.0.1:6379/0",
        "redis://localhost",
        "valkey://:v4lkey-pw@[::1]:6379/0",
        "unix:///run/valkey.sock?db=0",
        "rediss://:v4lkey-pw@localhost/0?ssl_cert_reqs=none&ssl_check_hostname=false",  # on this machine: its own affair
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=required&ssl_check_hostname=true",
        "valkeys://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=1&db=2",
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=",  # blank: redis-py leaves it out
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_cert_reqs=required&ssl_cert_reqs=none",  # the first is read
        "rediss://:v4lkey-pw@cache-9c1d.corp.example/0?ssl_check_hostname=yes&ssl_ca_certs=%2Fetc%2Fssl%2Fca.pem",
    ],
)
def test_valkey_on_this_machine_or_over_a_checked_tls_needs_nothing_more(address):
    assert _settings(valkey_url=address).valkey_url == address


@pytest.mark.parametrize(
    "query",
    [
        "", "ssl_cert_reqs=required", "ssl_cert_reqs=none", "ssl_cert_reqs=optional", "ssl_cert_reqs=bogus",
        "ssl_cert_reqs=Required", "ssl_cert_reqs=", "ssl_cert_reqs=none&ssl_cert_reqs=required",
        "ssl_cert_reqs=required&ssl_cert_reqs=none", "ssl_check_hostname=true", "ssl_check_hostname=1",
        "ssl_check_hostname=yes", "ssl_check_hostname=false", "ssl_check_hostname=False", "ssl_check_hostname=0",
        "ssl_check_hostname=no", "ssl_check_hostname=NO", "ssl_check_hostname=f", "ssl_check_hostname=n",
        "ssl_check_hostname=", "ssl_check_hostname=off", "ssl_check_hostname=2", "ssl_check_hostname=false&ssl_check_hostname=true",
        "ssl_check_hostname=true&ssl_check_hostname=false", "ssl_cert_reqs=none&ssl_check_hostname=true",
    ],
)  # fmt: skip
def test_what_the_settings_call_a_checked_tls_is_what_redis_py_does(query):
    """The settings and the driver read the parameters alike: the driver's own connection object, made from the address
    as redis-py reads it, checks the certificate and the host's name exactly when the settings let the address through."""
    connection_module = pytest.importorskip("redis.connection")
    import ssl

    address = f"rediss://:v4lkey-pw@cache-9c1d.corp.example:6379/0?{query}"
    try:
        kwargs = connection_module.parse_url(address)
        kwargs.pop("connection_class")
        driver = connection_module.SSLConnection(**kwargs)
        checks = driver.cert_reqs == ssl.CERT_REQUIRED and driver.check_hostname is True
    except Exception:  # an error to the driver (a word it does not know) is no connection at all
        checks = False
    try:
        _settings(valkey_url=address)
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted == checks, (query, accepted, checks)


def test_a_valkey_address_is_read_as_redis_py_reads_one():
    assert redis_url("valkey://cache.acme.lan:6379/0") == "redis://cache.acme.lan:6379/0"
    assert redis_url("valkeys://cache.acme.lan:6380") == "rediss://cache.acme.lan:6380"
    assert redis_url("redis://cache.acme.lan") == "redis://cache.acme.lan"
    assert redis_url("rediss://:v4lkey-pw@cache.acme.lan/0?ssl_cert_reqs=required") == (
        "rediss://:v4lkey-pw@cache.acme.lan/0?ssl_cert_reqs=required"
    )
    assert redis_url("unix:///run/valkey.sock?db=0") == "unix:///run/valkey.sock?db=0"
    # only the scheme is read: a password that holds a scheme, a text that has none
    assert redis_url("valkeys://:valkey://x@cache.acme.lan") == "rediss://:valkey://x@cache.acme.lan"
    assert redis_url("valkey") == "valkey" and redis_url("cache.acme.lan") == "cache.acme.lan"
    # and only as it is written in small letters, which is how the settings take it: redis-py refuses the rest
    assert redis_url("VALKEYS://cache.acme.lan") == "VALKEYS://cache.acme.lan"


# The names anyone might give a Valkey address' scheme. The settings accept some; the bus connects with the address as
# redis-py reads it, and TLS the settings accepted must be TLS made. They are tried here, not taken from the settings'
# table, so that a scheme added to the settings and not read by redis-py is found.
SCHEMES = (
    "redis", "rediss", "valkey", "valkeys", "unix", "unixs", "redis+unix", "rediss+unix", "valkey+unix", "tcp", "tls",
    "ssl", "http", "https", "valkeyss", "redis-sentinel", "sentinel", "REDIS", "Valkeys",
)  # fmt: skip


def _valkey_address(scheme: str, host: str) -> str:
    return "unix:///run/valkey.sock?db=0" if scheme == "unix" else f"{scheme}://:v4lkey-pw@{host}:6379/0"


def _valkey_accepted(scheme: str, host: str) -> bool:
    try:
        _settings(valkey_url=_valkey_address(scheme, host))
    except ValidationError:
        return False
    return True


def _valkey_tls(scheme: str) -> bool:
    """Whether the settings call a scheme TLS: they accept it off this machine, with nothing said of the network, and
    that is only a TLS address that checks the server. (A socket is on this machine by its scheme, whatever its host.)"""
    return scheme != "unix" and _valkey_accepted(scheme, "cache.acme.lan")


@pytest.mark.parametrize("scheme", sorted({*SCHEMES, *server_settings.VALKEY_SCHEMES}))
def test_every_scheme_the_settings_accept_is_one_redis_py_connects_with_and_tls_stays_tls(scheme):
    on_machine, tls = _valkey_accepted(scheme, "127.0.0.1"), _valkey_tls(scheme)
    assert on_machine or not tls
    if not on_machine:
        return
    address = redis_url(_valkey_address(scheme, "cache.acme.lan" if tls else "127.0.0.1"))
    for driver in (redis.connection, redis.asyncio.connection):  # the sync client and the one of the listening call
        kwargs = driver.parse_url(address)  # a ValueError: a scheme the settings accept and redis-py does not read
        assert (kwargs.get("connection_class") is driver.SSLConnection) == tls, (scheme, address.partition(":")[0])


def test_the_schemes_the_settings_accept_include_the_five_a_valkey_address_is_written_with():
    accepted = {scheme for scheme in SCHEMES if _valkey_accepted(scheme, "127.0.0.1")}
    assert accepted >= {"redis", "rediss", "valkey", "valkeys", "unix"}
    assert {scheme for scheme in SCHEMES if _valkey_tls(scheme)} == {"rediss", "valkeys"}


# Fix round 2. The PostgreSQL half of the same test: the settings let an address off this machine through exactly when
# the sslmode libpq will use, read by libpq itself, checks the server. What libpq will use is not always what is
# written: requiressl and ssl=true are written into sslmode, the last parameter winning, and sslrootcert=system with no
# sslmode makes it verify-full.
SSLMODE_QUERIES = [
    "", "sslmode=disable", "sslmode=allow", "sslmode=prefer", "sslmode=require", "sslmode=verify-ca",
    "sslmode=verify-full", "sslmode=verify-full&sslmode=disable", "sslmode=disable&sslmode=verify-full",
    "sslmode=verify-ca&sslmode=require", "sslmode=Verify-Full", "sslmode=VERIFY-CA", "sslmode=verify_full",
    "sslmode=", "sslmode=verify%2Dfull", "sslmode=verify-ful%6C", "sslmode=%20verify-full", "sslmode=verify-full%20",
    "sslrootcert=system", "sslrootcert=%73ystem", "sslrootcert=system&sslmode=verify-full",
    "sslmode=verify-full&sslrootcert=system", "sslrootcert=system&sslmode=verify-ca",
    "sslrootcert=system&sslmode=require", "sslrootcert=system&sslmode=disable", "sslmode=disable&sslrootcert=system",
    "sslrootcert=System", "sslrootcert=system%20", "sslrootcert=", "sslrootcert=%2Fetc%2Fssl%2Fca.pem",
    "sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fca.pem", "sslrootcert=system&sslmode=", "sslmode=&sslrootcert=system",
    "sslrootcert=system&sslrootcert=", "sslrootcert=&sslrootcert=system",
    "sslrootcert=system&sslrootcert=%2Fetc%2Fca.pem", "sslrootcert=%2Fetc%2Fca.pem&sslrootcert=system",
    "application_name=sslmode%3Dverify-full", "application_name=system&sslrootcert=%2Fetc%2Fca.pem",
    # the two names libpq writes into sslmode, before and after it, as the last parameter wins
    "sslmode=verify-full&ssl=true", "ssl=true&sslmode=verify-full", "sslmode=verify-full&requiressl=0",
    "sslmode=verify-full&requiressl=1", "requiressl=0&sslmode=verify-full", "requiressl=1&sslmode=verify-full",
    "requiressl=1", "requiressl=0", "ssl=true", "sslmode=verify-ca&requiressl=0", "sslmode=verify-full&ssl=false",
    "sslmode=verify-full&sslrootcert=system&ssl=true", "sslrootcert=system&requiressl=1",
    "sslrootcert=system&requiressl=0", "requiressl=0&sslrootcert=system", "ssl=true&sslrootcert=system",
    "sslmode=verify-full&requiressl=", "sslmode=verify-full&requiressl=true", "sslmode=verify-full&%73sl=true",
    "sslmode=verify-full&requires%73l=0", "sslmode=verify-full&SSL=true", "sslmode=verify-full&Requiressl=0",
]  # fmt: skip


def _names_of(query: str) -> list[str]:
    return [parameter.partition("=")[0] for parameter in query.split("&")]


@pytest.fixture
def libpq_alone(monkeypatch):
    """What libpq makes of an address with nothing of this computer's in it: no PGSSLMODE, no PGSSLROOTCERT..."""
    for name in [name for name in os.environ if name.startswith("PG")]:
        monkeypatch.delenv(name)


def _libpq_sslmode(query: str) -> str | None:
    """The sslmode libpq will use for an address with this query, as it holds it before it connects anywhere (to a port
    nothing listens on), or None for an address it refuses to read."""
    with socket.socket() as one:
        one.bind(("127.0.0.1", 0))
        port = one.getsockname()[1]
    conn = pq.PGconn.connect_start(f"postgresql://latet:s3cret-pw@127.0.0.1:{port}/latet?{query}".encode())
    try:
        return {option.keyword.decode(): option.val.decode() for option in conn.info if option.val is not None}.get(
            "sslmode"
        )
    finally:
        conn.finish()


def _libpq_refuses_as_weak(query: str) -> bool:
    """Whether libpq refuses the sslmode of an address with this query beside its root certificates before it connects
    anywhere: ``sslrootcert=system`` with any mode but verify-full ("weak sslmode")."""
    conn = pq.PGconn.connect_start(f"postgresql://latet:s3cret-pw@127.0.0.1:1/latet?{query}".encode())
    try:
        return conn.status == pq.ConnStatus.BAD and b"weak sslmode" in conn.error_message
    finally:
        conn.finish()


def _accepts_off_this_machine(query: str) -> bool:
    try:
        _settings(database_url=f"postgresql://latet:s3cret-pw@{OFF}/latet?{query}")
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("query", SSLMODE_QUERIES)
def test_what_the_settings_call_a_checked_tls_is_what_libpq_will_do(libpq_alone, query):
    """The settings and the driver read the parameters alike: the settings let an address off this machine through
    exactly when the sslmode that libpq itself will use verifies the server (verify-ca or verify-full), and libpq does
    not refuse it beside sslrootcert=system. An address that writes ssl or requiressl is refused whatever else it says:
    libpq keeps the last parameter's mode."""
    held = _libpq_sslmode(query)  # None: libpq refuses to read the address
    accepted = _accepts_off_this_machine(query)
    if {"ssl", "requiressl"} & set(_names_of(query)):
        assert not accepted, (query, held)
        return
    expected = held in ("verify-full", "verify-ca") and not _libpq_refuses_as_weak(query)
    assert accepted == expected, (query, held, accepted)
    address = f"postgresql://latet:s3cret-pw@{OFF}/latet?{query}"
    if server_settings._unreadable("database_url", address, libpq=True) is None:  # one the start reads at all
        assert server_settings._postgres_sslmode(address) == held, (query, held)


def test_no_address_that_the_settings_let_through_is_one_whose_sslmode_libpq_makes_weak(libpq_alone):
    """A seeded corpus of queries made of what an address can say about TLS, as written and as escapes, with the two
    names libpq writes into sslmode among them: whatever the settings let through off this machine, libpq verifies."""
    rng = random.Random(20261014)
    pieces = [
        "sslmode=verify-full", "sslmode=verify-ca", "sslmode=require", "sslmode=prefer", "sslmode=disable",
        "sslmode=verify%2Dfull", "sslmode=", "sslrootcert=system", "sslrootcert=%73ystem", "sslrootcert=",
        "sslrootcert=%2Fetc%2Fca.pem", "ssl=true", "ssl=false", "requiressl=0", "requiressl=1", "requiressl=",
        "%73sl=true", "requires%73l=0", "SSL=true", "application_name=ssl%3Dtrue", "application_name=requiressl",
        "connect_timeout=5", "options=-c%20ssl%3Don",
    ]  # fmt: skip
    verified, refused = 0, 0
    for _ in range(1500):
        query = "&".join(rng.choices(pieces, k=rng.randint(0, 4)))
        accepted = _accepts_off_this_machine(query)
        held = _libpq_sslmode(query)
        assert not accepted or held in ("verify-full", "verify-ca"), (query, held)
        verified += accepted
        refused += not accepted
    assert verified > 50 and refused > 50  # the corpus holds both


@pytest.mark.parametrize("query", ["ssl=true", "requiressl=1&sslmode=verify-full", "sslmode=verify-full&requiressl=0"])
def test_ssl_and_requiressl_are_refused_in_words_that_say_to_write_sslmode(monkeypatch, query):
    """On this machine and off it, from the variable of the start: the sentence names the parameters and what to write
    instead, and holds no part of the address."""
    for host in ("127.0.0.1", "db.acme.lan"):
        error = _start(monkeypatch, database=f"postgresql://latet:s3cret-pw@{host}:5432/latet?{query}")
        assert re.search(r"database_url [^\n]*ssl or requiressl[^\n]*write sslmode", str(error)), str(error)
        assert _holds_its_words_alone(error) and not any(part in _shown(error) for part in ("s3cret", "acme", "5432"))


@pytest.mark.parametrize(
    "query",
    [
        "application_name=requiressl",
        "application_name=ssl",
        "application_name=ssl%3Dtrue&sslmode=verify-full",
        "options=-c%20ssl%3Don",
        "sslcert=%2Fssl&sslkey=%2Frequiressl&sslmode=verify-full&sslrootcert=system",
    ],
)
def test_a_text_that_is_not_the_name_of_a_parameter_is_not_taken_for_ssl_or_requiressl(query):
    address = f"postgresql://latet:s3cret-pw@127.0.0.1:5432/latet?{query}"
    assert _settings(database_url=address).database_url == address
    userinfo = "postgresql://ssl%3Dtrue:requiressl%3D1@127.0.0.1:5432/requiressl%3D1"  # in the user and the database
    assert _settings(database_url=userinfo).database_url == userinfo


@pytest.mark.parametrize(
    "query",
    [
        "hostaddr=10.0.0.5&sslmode=verify-full&sslrootcert=system",
        "hostaddr=10.0.0.5&sslrootcert=system",  # verify-full, as libpq makes it
        "host=,db.acme.lan&hostaddr=10.0.0.5,10.0.0.6&sslmode=verify-full&sslrootcert=system",  # the first place
        "hostaddr=127.0.0.1&sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fca.pem",  # on this machine as well
    ],
)
def test_a_verify_full_address_with_a_hostaddr_and_no_host_is_refused_in_words(monkeypatch, query):
    """libpq checks the certificate against the host's name and says only that a host name must be specified, which
    the operator would see as the type of the error at the first connection."""
    error = _start(monkeypatch, database=f"postgresql://latet:s3cret-pw@/latet?{query}")
    assert re.search(r"database_url [^\n]*hostaddr[^\n]*host", str(error)), str(error)
    assert _holds_its_words_alone(error) and not any(part in _shown(error) for part in ("s3cret", "10.0.0", "acme"))


def test_verify_ca_and_a_host_beside_the_hostaddr_are_not_refused_for_it():
    """verify-ca checks no name, and libpq connects with a hostaddr alone (tried against a PostgreSQL with TLS); with a
    host beside it, the name is there to check."""
    for address in (
        "postgresql://latet:s3cret-pw@/latet?hostaddr=10.0.0.5&sslmode=verify-ca&sslrootcert=%2Fetc%2Fssl%2Fca.pem",
        "postgresql://latet:s3cret-pw@db.acme.lan/latet?hostaddr=10.0.0.5&sslmode=verify-full&sslrootcert=system",
        "postgresql://latet:s3cret-pw@/latet?host=db.acme.lan&hostaddr=10.0.0.5&sslrootcert=system",
        "postgresql://latet:s3cret-pw@/latet?host=db.acme.lan,db.acme.lan&hostaddr=10.0.0.5,10.0.0.6&sslrootcert=system",
    ):
        assert _settings(database_url=address).database_url == address


def test_the_two_private_network_settings_are_off_unless_said_and_come_from_their_variables(tmp_path, monkeypatch):
    for name in (*server_settings._VARIABLES.values(), "LATET_SERVER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    found = _settings()
    assert (found.database_private_network, found.valkey_private_network) == (False, False)
    off = {
        "LATET_SERVER_URL": URL,
        "LATET_SERVER_DATABASE_URL": f"postgresql://latet:s3cret-pw@{OFF}/latet",
        "LATET_SERVER_VALKEY_URL": "redis://:v4lkey-pw@cache-9c1d.corp.example",
    }
    for name, value in off.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="LATET_SERVER_DATABASE_PRIVATE_NETWORK") as refused:
        load_server_settings()  # the start's path: the words alone
    assert _holds_its_words_alone(refused.value) and not any(part in _shown(refused.value) for part in OFF_PARTS)
    monkeypatch.setenv("LATET_SERVER_DATABASE_PRIVATE_NETWORK", "1")
    with pytest.raises(ValueError, match="LATET_SERVER_VALKEY_PRIVATE_NETWORK") as refused:
        load_server_settings()
    assert _holds_its_words_alone(refused.value) and not any(part in _shown(refused.value) for part in OFF_PARTS)
    monkeypatch.setenv("LATET_SERVER_VALKEY_PRIVATE_NETWORK", "1")
    found = load_server_settings()
    assert (found.database_private_network, found.valkey_private_network) == (True, True)
    (tmp_path / "server.yaml").write_text(
        "".join(f"{key.removeprefix('LATET_SERVER_').lower()}: {value}\n" for key, value in off.items())
        + "database_private_network: true\nvalkey_private_network: true\n",
        "utf-8",
    )
    for name in off:
        monkeypatch.delenv(name)
    monkeypatch.delenv("LATET_SERVER_DATABASE_PRIVATE_NETWORK")
    monkeypatch.delenv("LATET_SERVER_VALKEY_PRIVATE_NETWORK")
    found = load_server_settings(tmp_path / "server.yaml")  # server.yaml says it too
    assert (found.database_private_network, found.valkey_private_network) == (True, True)
