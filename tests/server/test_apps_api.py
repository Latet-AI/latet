"""The applications API on a Latet server (``/_latet/apps/v1/{scan,mask,unmask}``): an API key's token in
``Authorization: Bearer``, each operation held to the token's permissions; a text masked with the key's fakes, the same
on every instance, and unmasked from the key's whole vault, every unmask written in the administration log before its
answer leaves; positions in Unicode code points; nothing of a text in an error, a log entry or a metric; the local
reviewer on mask, within its time, and never the Live Reviewer."""

import asyncio
import contextlib
import json
import threading
import time

import httpx
import psycopg
import pytest

from latet.bridge import apps as apps_module
from latet.bridge.apps import APPS_API, MOST_TEXTS
from latet.config import LiveReview, LlmReview
from latet.engine.detectors.base import DetectorSkipped
from latet.engine.pipeline import Session
from latet.engine.surrogates.generators import Drbg, shape
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server import app as app_module
from latet.server.app import create_server_app
from latet.server.machines import STORE_THREADS, StoresUnavailable, in_store
from latet.server.operations import server_app
from latet.server.settings import ServerSettings
from latet.server.tokens import route_key

from ..unit.test_reviewer import FakeServer, reviewer
from ..unit.test_short_secrets import INSIDE, INSIDE_TEXT, OLD, PAIR, TOKEN, ids_holding, inside_unmasked
from .support import DATABASE, URL, captured_logs, listening, until

pytestmark = pytest.mark.server

CANARY = "mario.rossi@acme.it"
IBAN = "GB82WEST12345698765432"
PHONE = "+39 347 123 4567"
WHERE = ("10.0.3.9", 40000)  # the client's address, as the administration log records it


def _token(server, key_id, name, permissions):
    """Another token of the API key ``key_id``, with ``permissions``: its id and its secret."""
    with server.identities.changing() as changes:
        info, secret = server.identities.add_token(key_id, name, permissions, within=changes)
    return info.id, secret


def _bearer(secret):
    return {"authorization": f"Bearer {secret}"}


@contextlib.asynccontextmanager
async def _calling(*servers, app=None):
    """A client for each instance, its ear on Valkey while it lasts, its spaces closed after."""
    async with listening(*servers), contextlib.AsyncExitStack() as stack:
        clients = []
        for server in servers:
            transport = httpx.ASGITransport(app=app or create_server_app(server), client=WHERE)
            clients.append(await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url=URL)))
        yield clients[0] if len(clients) == 1 else clients


async def _post(client, secret, operation, body):
    return await client.post(f"{APPS_API}/{operation}", json=body, headers=_bearer(secret))


def _values_of(text, items):
    """What ``items`` (findings or replacements) cover in ``text``."""
    return [text[item["start"] : item["end"]] for item in items]


async def test_mask_then_unmask_gives_the_texts_back(server):
    _, secret = server.identities.add_key("Billing CI")
    texts = [f"write to {CANARY}, IBAN {IBAN}, call {PHONE}", f"from {CANARY}"]
    async with _calling(server) as client:
        masked = await _post(client, secret, "mask", {"texts": texts})
        assert masked.status_code == 200 and masked.headers["cache-control"] == "no-store"  # kept by no cache
        results = masked.json()["results"]
        assert len(results) == 2
        for result in results:
            assert CANARY not in result["text"] and IBAN not in result["text"] and PHONE not in result["text"]
        first = results[0]
        assert [r["type"] for r in first["replacements"]] == ["EMAIL", "IBAN", "PHONE"]
        assert {r["class"] for r in first["replacements"]} == {"pii"}
        assert first["counts"] == {"EMAIL": 1, "IBAN": 1, "PHONE": 1}
        assert _values_of(results[1]["text"], results[1]["replacements"]) == _values_of(
            first["text"], first["replacements"][:1]
        )  # the same value, the same fake
        unmasked = await _post(client, secret, "unmask", {"texts": [r["text"] for r in results]})
    assert unmasked.status_code == 200
    assert unmasked.json() == {"results": [{"text": texts[0], "restored": 3}, {"text": texts[1], "restored": 1}]}


async def test_a_value_a_later_text_teaches_is_masked_in_the_earlier_ones(server):
    """A call's texts are masked as one: the domain an address in the second text teaches the vault is masked where the
    first one names it bare."""
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        masked = await _post(client, secret, "mask", {"texts": ["see acme.it for the terms", f"to {CANARY}"]})
    first, second = masked.json()["results"]
    assert "acme.it" not in first["text"] and first["counts"] == {"DOMAIN": 1}
    assert first["text"].split()[1] == second["text"].split("@")[1]  # the domain's fake, in both


async def test_scan_says_where_and_keeps_nothing(server):
    key, secret = server.identities.add_key("Billing CI")
    text = f"write to {CANARY}, IBAN {IBAN}, again {CANARY}"

    def count():
        with server.db.connection() as conn:
            return conn.execute("SELECT count(*) FROM vault_entities WHERE identity_id = %s", (key.id,)).fetchone()[0]

    async with _calling(server) as client:
        scanned = await _post(client, secret, "scan", {"texts": [text]})
    assert scanned.status_code == 200
    [result] = scanned.json()["results"]
    assert _values_of(text, result["findings"]) == [CANARY, IBAN, CANARY]
    assert [(f["type"], f["class"]) for f in result["findings"]] == [
        ("EMAIL", "pii"),
        ("IBAN", "pii"),
        ("EMAIL", "pii"),
    ]
    assert result["counts"] == {"EMAIL": 2, "IBAN": 1}
    assert count() == 0  # no fake was made


async def test_the_same_value_gets_the_same_fake_across_calls_and_instances(instances):
    a, b = instances
    _, secret = a.identities.add_key("Billing CI")
    async with _calling(a, b) as (first, second):
        one = (await _post(first, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        again = (await _post(first, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        there = (await _post(second, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        back = await _post(second, secret, "unmask", {"texts": [one]})
    assert one == again == there and CANARY not in one
    assert back.json()["results"] == [{"text": f"to {CANARY}", "restored": 1}]


async def test_two_instances_mask_one_new_value_at_once(instances):
    a, b = instances
    _, secret = a.identities.add_key("Billing CI")
    async with _calling(a, b) as (first, second):
        await _post(first, secret, "scan", {"texts": ["warm up"]})  # both spaces built before the race
        await _post(second, secret, "scan", {"texts": ["warm up"]})
        both = await asyncio.gather(
            _post(first, secret, "mask", {"texts": [f"IBAN {IBAN}"]}),
            _post(second, secret, "mask", {"texts": [f"IBAN {IBAN}"]}),
        )
    assert [answer.status_code for answer in both] == [200, 200]
    one, other = (answer.json()["results"][0]["text"] for answer in both)
    assert one == other and IBAN not in one


async def test_a_fake_of_the_space_is_masked_as_a_value(server):
    """Already masked text is not recognised: a fake the space made is a value like any other, masked again, and one
    unmask takes the text one step back."""
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        once = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        twice = (await _post(client, secret, "mask", {"texts": [once]})).json()["results"][0]
        back = (await _post(client, secret, "unmask", {"texts": [twice["text"]]})).json()["results"][0]
    assert twice["text"] != once and twice["counts"] == {"EMAIL": 1}
    assert back == {"text": once, "restored": 1}


def _first_fake(vault, etype, value):
    """The fake 0.10.0 gave ``value`` in ``vault`` at its first draw: its shape, as long as it is."""
    return shape(value, Drbg(vault._k_sur, vault._seed(etype, value, 0)))


def _entries(server, key):
    with server.db.connection() as conn:
        return conn.execute("SELECT count(*) FROM vault_entities WHERE identity_id = %s", (key.id,)).fetchone()[0]


async def test_a_short_secret_gets_a_long_fake_and_no_id_comes_back_changed(server):
    """0.10.1, the finding of 0.11.0's Task 1: a key's short secrets (`password=1234`, `token=ab12f3`) get long fakes
    (fifteen digits, twelve hexadecimal digits); a log line whose id holds the digits or the hexadecimal digits their
    fakes had in 0.10.0 comes back from unmask as it was (`request_id=347bf309420a2290` came back `347bf573020a2290`),
    and mask keeps no value of it (it kept the fake found inside the id as a value of its own)."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        masked = (await _post(client, secret, "mask", {"texts": [PAIR, TOKEN]})).json()["results"]
        vault = (await server.spaces.runtime(key)).built.vault
        lines = ids_holding(*(_first_fake(vault, etype, value) for etype, value, _ in OLD))
        count = _entries(server, key)
        unmasked = await _post(client, secret, "unmask", {"texts": lines})
        again = await _post(client, secret, "mask", {"texts": lines})
    assert unmasked.json()["results"] == [{"text": line, "restored": 0} for line in lines]
    assert [result["text"] for result in again.json()["results"]] == lines and _entries(server, key) == count
    fakes = [fake for result in masked for fake in _values_of(result["text"], result["replacements"])]
    assert [len(fake) for fake in fakes] == [15, 12] and fakes[0].isdigit()


async def test_a_short_fake_given_before_is_restored_only_as_a_whole_word(server):
    """R7: the short fakes 0.10.0 gave a key's short secrets stay, read from PostgreSQL by an instance built after:
    unmask restores them where they stand as whole words and never inside an id or a number, and mask masks their
    values with them as always and keeps no value of an id that holds one."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        await _post(client, secret, "scan", {"texts": ["warm up"]})  # the key's space built
        vault = (await server.spaces.runtime(key)).built.vault
        old = [
            Entity(
                vault.entity_id(etype, value), etype, EntityClass.SECRET, value, _first_fake(vault, etype, value), 0,
                time.time(), rule,
            )
            for etype, value, rule in OLD
        ]  # fmt: skip
        for entity in old:  # as 0.10.0 kept them
            assert await asyncio.to_thread(vault._store.save_entity, entity) is None
        await server.spaces.drop(key.id)  # built again, from the store
        digits, hexadecimal = (entity.surrogate for entity in old)
        assert len(digits) == 4 and len(hexadecimal) == 6
        lines = ids_holding(digits, hexadecimal)
        answer = f'password={digits} and the token "{hexadecimal}".'
        unmasked = await _post(client, secret, "unmask", {"texts": [answer, *lines]})
        count = _entries(server, key)
        again = await _post(client, secret, "mask", {"texts": [*lines, PAIR]})
    assert unmasked.json()["results"] == [
        {"text": 'password=1234 and the token "ab12f3".', "restored": 2},
        *({"text": line, "restored": 0} for line in lines),
    ]
    assert [result["text"] for result in again.json()["results"]] == [*lines, PAIR.replace("1234", digits)]
    assert _entries(server, key) == count


async def test_short_fakes_given_before_inside_a_word_come_back_where_they_stand_whole(server):
    """The review of 0.10.1 (C1), through the applications API: values of short secrets of 0.10.0 inside longer words
    and numbers (`postgres` in `postgresql://`, `1234` in `order_id=9912343`, `admin1` in `admin12`) are masked there
    with their short fakes, as always; unmask, an unmask of the whole vault, restores the one standing as a whole word
    and leaves the others as their fakes, the documented cost. A fake of 9 to 11 characters (`Winter2024`'s) is
    restored inside a word, as 0.10.0 restored it."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        await _post(client, secret, "scan", {"texts": ["warm up"]})  # the key's space built
        vault = (await server.spaces.runtime(key)).built.vault
        old = [
            Entity(
                vault.entity_id(etype, value), etype, EntityClass.SECRET, value, _first_fake(vault, etype, value), 0,
                time.time(), rule,
            )
            for etype, value, rule in (*INSIDE, ("PASSWORD", "Winter2024", None))
        ]  # fmt: skip
        for entity in old:  # as 0.10.0 kept them
            assert await asyncio.to_thread(vault._store.save_entity, entity) is None
        await server.spaces.drop(key.id)  # built again, from the store
        texts = [INSIDE_TEXT, "ref xWinter2024y end"]
        masked = [
            result["text"] for result in (await _post(client, secret, "mask", {"texts": texts})).json()["results"]
        ]
        unmasked = await _post(client, secret, "unmask", {"texts": masked})
    winter = old[-1].surrogate
    assert len(winter) == 10 and masked[1] == f"ref x{winter}y end"
    assert unmasked.json()["results"] == [
        {"text": inside_unmasked(masked[0], old[:-1]), "restored": 1},
        {"text": texts[1], "restored": 1},
    ]


async def test_the_space_gets_the_grant_and_not_the_token(server, monkeypatch):
    key, _ = server.identities.add_key("Billing CI")
    token, secret = _token(server, key.id, "logs", ["mask"])
    seen: list[dict] = []
    real = server.spaces.app

    async def spied(identity):
        app = await real(identity)

        async def seeing(scope, receive, send):
            seen.append(scope)
            await app(scope, receive, send)

        return seeing

    monkeypatch.setattr(server.spaces, "app", spied)
    async with _calling(server) as client:
        assert (await _post(client, secret, "mask", {"texts": ["hello"]})).status_code == 200
    [scope] = seen
    assert scope["latet.grant"] == {"token": token, "permissions": ["mask", "scan"]}
    assert scope["latet.identity"] == key.id
    assert all(name != b"authorization" for name, _ in scope["headers"])
    assert secret.encode() not in b"".join(value for _, value in scope["headers"])


async def test_offsets_are_code_points(server):
    _, secret = server.identities.add_key("Billing CI")
    text = f"👩‍💻 ✉ {CANARY}"
    async with _calling(server) as client:
        scanned = (await _post(client, secret, "scan", {"texts": [text]})).json()["results"][0]
        masked = (await _post(client, secret, "mask", {"texts": [text]})).json()["results"][0]
    assert _values_of(text, scanned["findings"]) == [CANARY]
    [fake] = _values_of(masked["text"], masked["replacements"])
    assert masked["text"] == f"👩‍💻 ✉ {fake}" and "@" in fake and fake != CANARY


async def test_empty_texts_come_back_as_they_are(server):
    _, secret = server.identities.add_key("Billing CI")
    texts = ["", "   "]
    async with _calling(server) as client:
        scanned = await _post(client, secret, "scan", {"texts": texts})
        masked = await _post(client, secret, "mask", {"texts": texts})
        unmasked = await _post(client, secret, "unmask", {"texts": texts})
    assert scanned.json() == {"results": [{"findings": [], "counts": {}}] * 2}
    assert masked.json() == {"results": [{"text": text, "replacements": [], "counts": {}} for text in texts]}
    assert unmasked.json() == {"results": [{"text": text, "restored": 0} for text in texts]}


async def test_prose_and_data(server):
    key, secret = server.identities.add_key("Billing CI")
    text = "chiamami al 3471234567"
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(
            runtime.update_config, lambda current: current.model_copy(update={"phone_regions": ["IT"]})
        )
        # As data first: once masked as prose, the vault knows the number, and masks it wherever it is from then on.
        data = (await _post(client, secret, "mask", {"texts": [text], "kind": "data"})).json()["results"][0]
        scanned = (await _post(client, secret, "scan", {"texts": [text], "kind": "data"})).json()["results"][0]
        prose = (await _post(client, secret, "mask", {"texts": [text]})).json()["results"][0]
        again = (await _post(client, secret, "mask", {"texts": [text], "kind": "data"})).json()["results"][0]
    assert data == {"text": text, "replacements": [], "counts": {}} and scanned["findings"] == []
    assert "3471234567" not in prose["text"] and prose["counts"] == {"PHONE": 1}
    assert again["text"] == prose["text"]  # a value found once is masked everywhere


async def test_a_json_document_is_masked_as_a_text(server):
    """R10: a JSON document sent as a text is masked as text, its layout kept, its positions in the text it answers."""
    _, secret = server.identities.add_key("Billing CI")
    document = json.dumps({"customer": {"email": CANARY, "note": "ok"}}, indent=2)
    async with _calling(server) as client:
        masked = (await _post(client, secret, "mask", {"texts": [document], "kind": "data"})).json()["results"][0]
    [fake] = _values_of(masked["text"], masked["replacements"])
    assert masked["text"] == document.replace(CANARY, fake) and CANARY not in masked["text"]


CODE = "latet_code_Q7mZ2xK9pL4vN8rT1wY6bH3cJ5dF0gSa"  # a person's one-time code: its prefix and 32 characters
SCIM = "latet_scim_Xk3vB9qL2mN7pR4tW8yZ1aC6dE0fG5hJ-kM_nP3sT7u"  # an Enterprise server's SCIM token: 43 after it
BRIDGE = "latet_brg_h2Jd8Kq-Lp4Zx9Vb_3Nm7Rt1Wy5Ce0Fg6Hs2Ua8Ok4Q"  # a computer's bridge token: 43 after it


async def test_latet_credentials_and_secret_pairs_are_masked_with_the_keys_vault(instances):
    """The main line's fixes reach the applications API and a key's vault: Latet's newer credentials by their shape, a
    secret-named pair in the middle of a log line, and a line that ends right after a secret's name, which made masking
    fail, masked on one instance and restored on another from the key's vault."""
    a, b = instances
    _, secret = a.identities.add_key("Billing CI")
    texts = [
        f"enrol it with `latet connect --code {CODE}` today",
        "INFO login ok password=Winter2024x user=ada",
        f"DB_PASSWORD=\nnotify {CANARY}",
        f"SCIM {SCIM} and the bridge's {BRIDGE}",
    ]
    async with _calling(a, b) as (first, second):
        scanned = await _post(first, secret, "scan", {"texts": texts})
        masked = await _post(first, secret, "mask", {"texts": texts})
        unmasked = await _post(second, secret, "unmask", {"texts": [r["text"] for r in masked.json()["results"]]})
    assert scanned.status_code == masked.status_code == unmasked.status_code == 200
    found = scanned.json()["results"]
    assert _values_of(texts[0], found[0]["findings"]) == [CODE]
    assert _values_of(texts[1], found[1]["findings"]) == ["Winter2024x"]
    assert _values_of(texts[2], found[2]["findings"]) == [CANARY]
    assert _values_of(texts[3], found[3]["findings"]) == [SCIM, BRIDGE]
    code, pair, ended, tokens = masked.json()["results"]
    assert [r["type"] for r in code["replacements"]] == ["LATET_ONE_TIME_CODE"]
    assert [r["class"] for r in pair["replacements"]] == ["secret"]
    assert [r["type"] for r in tokens["replacements"]] == ["LATET_SCIM_TOKEN", "LATET_BRIDGE_TOKEN"]
    for result, values in ((code, [CODE]), (tokens, [SCIM, BRIDGE])):
        fakes = _values_of(result["text"], result["replacements"])
        for fake, value in zip(fakes, values, strict=True):
            prefix = value[: value.index("_", len("latet_")) + 1]
            assert fake != value and fake.startswith(prefix) and len(fake) == len(value)  # prefix and length kept
    assert pair["text"].startswith("INFO login ok password=") and "Winter2024x" not in pair["text"]
    assert pair["text"].endswith(" user=ada")
    assert ended["text"].startswith("DB_PASSWORD=\nnotify ") and CANARY not in ended["text"]
    assert ended["counts"] == {"EMAIL": 1}
    assert unmasked.json()["results"] == [
        {"text": text, "restored": restored} for text, restored in zip(texts, [1, 1, 1, 2], strict=True)
    ]


@pytest.mark.parametrize(
    ("given", "allowed"),
    [(["scan"], {"scan"}), (["mask"], {"scan", "mask"}), (["unmask"], {"unmask"})],
    ids=["scan-only", "mask-only", "unmask-only"],
)
async def test_permissions(server, given, allowed):
    key, _ = server.identities.add_key("Billing CI")
    _, secret = _token(server, key.id, "restricted", given)
    async with _calling(server) as client:
        for operation in ("scan", "mask", "unmask"):
            answer = await _post(client, secret, operation, {"texts": [f"to {CANARY}"]})
            if operation in allowed:
                assert answer.status_code == 200, operation
            else:
                assert answer.status_code == 403, operation
                assert answer.json() == {"error": f"this token lacks the {operation} permission"}


async def test_a_persons_token_is_refused(server):
    _, code = server.identities.add_person("Emily Carter")
    _, laptop, _ = server.identities.enrol(code, "laptop")
    async with _calling(server) as client:
        refused = await _post(client, laptop, "mask", {"texts": [f"to {CANARY}"]})
    assert refused.status_code == 403
    assert refused.json() == {"error": "latet: the applications API takes an API key's token"}


def _unauthorized(answer):
    return (
        answer.status_code == 401 and answer.headers.get("www-authenticate") == "Bearer" and CANARY not in answer.text
    )


async def test_no_token(server):
    async with _calling(server) as client:
        answer = await client.post(f"{APPS_API}/mask", json={"texts": [CANARY]})
    assert _unauthorized(answer)
    assert answer.json() == {"error": "latet: the applications API takes an API key's token: Authorization: Bearer"}


async def test_a_wrong_token(server):
    server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answer = await _post(client, "latet_key_" + "x" * 43, "mask", {"texts": [CANARY]})
        other = await _post(client, "not a token at all", "mask", {"texts": [CANARY]})
    assert _unauthorized(answer) and answer.json() == {"error": "latet: unauthorized"}
    assert _unauthorized(other)


async def test_two_authorization_headers(server):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        headers = [("authorization", f"Bearer {secret}"), ("authorization", f"Bearer {secret}")]
        answer = await client.post(f"{APPS_API}/mask", json={"texts": [CANARY]}, headers=headers)
    assert _unauthorized(answer)


async def test_basic_scheme(server):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answer = await client.post(f"{APPS_API}/mask", json={"texts": [CANARY]}, headers={"authorization": secret})
        basic = await client.post(
            f"{APPS_API}/mask", json={"texts": [CANARY]}, headers={"authorization": f"Basic {secret}"}
        )
    assert _unauthorized(answer) and _unauthorized(basic)


async def test_one_or_more_spaces_after_the_scheme(server):
    """RFC 7235: the scheme, one space or more, the token."""
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answers = [
            await client.post(f"{APPS_API}/scan", json={"texts": ["hello"]}, headers={"authorization": written})
            for written in (f"Bearer  {secret}", f"bearer    {secret}")
        ]
    assert [answer.status_code for answer in answers] == [200, 200]


async def test_a_persons_space_has_no_applications_api(server):
    """Only an API key's space mounts the applications API: a person's has none, whatever reaches it (the gate refuses
    a person's token before; this is what would answer were it to let one through)."""
    key, _ = server.identities.add_key("Billing CI")
    person, _ = server.identities.add_person("Emily Carter")
    granted = {"token": "t", "permissions": ["mask", "scan", "unmask"]}
    answers = {}
    async with listening(server):
        for identity in (key, person):
            space = await server.spaces.app(identity)

            async def through_the_gate(scope, receive, send, space=space, identity=identity):
                await space({**scope, "latet.identity": identity.id, "latet.grant": granted}, receive, send)

            transport = httpx.ASGITransport(app=through_the_gate, client=WHERE)
            async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
                answers[identity.kind] = await client.post(f"{APPS_API}/scan", json={"texts": [f"to {CANARY}"]})
    assert answers["key"].status_code == 200
    assert answers["person"].status_code == 404 and answers["person"].json() == {"error": "there is no such route"}


async def test_from_a_web_page(server, monkeypatch):
    _, secret = server.identities.add_key("Billing CI")
    looked_up = []
    real = app_module.secret_grant

    async def spied(*args):
        looked_up.append(args[1:])
        return await real(*args)

    monkeypatch.setattr(app_module, "secret_grant", spied)
    async with _calling(server) as client:
        page = {**_bearer(secret), "origin": "https://evil.example"}
        refused = await client.post(f"{APPS_API}/unmask", json={"texts": [CANARY]}, headers=page)
        fetched = await client.post(
            f"{APPS_API}/unmask", json={"texts": [CANARY]}, headers={**_bearer(secret), "sec-fetch-site": "none"}
        )
    assert refused.status_code == fetched.status_code == 403
    assert refused.json() == {"error": "latet: browser requests are not accepted on the applications API"}
    assert looked_up == []  # refused before the token was looked up: nothing counted against its visitor's address


async def test_not_reachable_through_an_address(server):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        headers = {**_bearer(secret), "x-latet-token": secret}
        answer = await client.post(f"/u/{route_key(secret)}{APPS_API}/mask", json={"texts": [CANARY]}, headers=headers)
    assert answer.status_code == 404 and CANARY not in answer.text


async def test_another_path_and_another_method(server):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        nowhere = await _post(client, secret, "remask", {"texts": [CANARY]})
        bare = await client.post(APPS_API, json={"texts": [CANARY]}, headers=_bearer(secret))
        got = await client.get(f"{APPS_API}/mask", headers=_bearer(secret))
        stranger = await client.get(f"{APPS_API}/mask")
    assert nowhere.status_code == bare.status_code == 404
    assert nowhere.json() == {"error": "there is no such route in the applications API"}
    assert got.status_code == 405 and got.headers["allow"] == "POST"
    assert got.json() == {"error": "the applications API takes POST"}
    assert _unauthorized(stranger)  # nothing is said of a route before its token


async def test_a_call_that_did_not_come_through_the_gate_is_refused(server):
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        space = await server.spaces.app(key)

        async def identity_only(scope, receive, send):  # whose request it is, and no grant
            await space({**scope, "latet.identity": key.id}, receive, send)

        answers = []
        for app in (identity_only, space):  # whose it is and no grant; nothing of the gate's at all
            transport = httpx.ASGITransport(app=app, client=WHERE)
            async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
                answers.append(await client.post(f"{APPS_API}/unmask", json={"texts": ["x"]}))
        refused, bare = answers
    assert refused.status_code == bare.status_code == 403
    assert refused.json() == {"error": "the applications API takes an API key's token"}


async def test_a_websocket_is_refused(server):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "wss",
        "path": f"{APPS_API}/mask", "raw_path": f"{APPS_API}/mask".encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"latet.acme.lan")], "client": WHERE, "server": ("latet.acme.lan", 443),
        "subprotocols": [], "extensions": {},
    }  # fmt: skip
    await create_server_app(server)(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 4404, "reason": "there is no such route"}]


BAD = [
    pytest.param(b"[]", id="not-an-object"),
    pytest.param(json.dumps([CANARY]).encode(), id="a-list"),
    pytest.param(b"not json " + CANARY.encode(), id="not-json"),
    pytest.param(json.dumps({"texts": CANARY}).encode(), id="texts-a-string"),
    pytest.param(json.dumps({"texts": [CANARY, 1]}).encode(), id="texts-not-all-strings"),
    pytest.param(json.dumps({"texts": []}).encode(), id="texts-empty"),
    pytest.param(json.dumps({"texts": [CANARY] * (MOST_TEXTS + 1)}).encode(), id="too-many-texts"),
    pytest.param(json.dumps({"texts": [CANARY], "kind": "poem"}).encode(), id="unknown-kind"),
    pytest.param(json.dumps({"texts": [CANARY], "kind": CANARY}).encode(), id="kind-a-value"),
]


@pytest.mark.parametrize("body", BAD)
async def test_bad_bodies(server, body):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answers = [
            await client.post(f"{APPS_API}/{operation}", content=body, headers=_bearer(secret))
            for operation in ("scan", "mask")
        ]
    for answer in answers:
        assert answer.status_code == 400
        assert set(answer.json()) == {"error"} and CANARY not in answer.text and "poem" not in answer.text


async def test_body_too_large(server):
    server.settings = server.settings.model_copy(update={"max_request_mb": 1})
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answer = await _post(client, secret, "mask", {"texts": ["x" * (1024 * 1024)]})
    assert answer.status_code == 413


async def test_a_call_is_bounded_by_its_own_body_limit_below_the_servers(server):
    """The security review's L2 (Ruling F2): a call's body is held in memory while it waits for its space's thread, up to
    16 calls of a key on each instance: the applications API takes at most ``apps_request_mb`` (8 MiB unless the
    settings say otherwise, what the SDK sends in a call at most), refused 413 before it is read past it, whatever
    ``max_request_mb`` takes on the server's other routes. Past it, as before; below it, the call is answered."""
    assert ServerSettings.model_fields["apps_request_mb"].default == 8
    server.settings = server.settings.model_copy(update={"apps_request_mb": 1})  # max_request_mb stays 32
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        past = await _post(client, secret, "mask", {"texts": ["x" * (1024 * 1024)]})
        declared = await client.post(
            f"{APPS_API}/scan", content=b"{" + b" " * (1024 * 1024) + b"}", headers=_bearer(secret)
        )
        within = await _post(client, secret, "scan", {"texts": ["x" * (1000 * 1000)]})
    assert past.status_code == declared.status_code == 413
    assert "apps_request_mb" in past.text and "apps_request_mb" in declared.text
    assert within.status_code == 200


def test_a_call_is_never_bounded_above_the_servers_limit():
    """``apps_request_mb`` lowers the bound of the applications API: above ``max_request_mb`` it changes nothing."""
    from latet.server.app import _apps_body_limit

    settings = ServerSettings.model_construct(max_request_mb=4, apps_request_mb=8)
    assert _apps_body_limit(settings) == 4 * 1024 * 1024
    assert _apps_body_limit(ServerSettings.model_construct(max_request_mb=32, apps_request_mb=8)) == 8 * 1024 * 1024


async def test_a_full_vault_answers_507(server):
    server.settings = server.settings.model_copy(update={"vault_limit": 1})  # past its bound: a test's own
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        full = await _post(client, secret, "mask", {"texts": [f"IBAN {IBAN}", f"to {CANARY}"]})
    assert full.status_code == 507
    assert full.json() == {
        "error": "this API key's vault is full: its limit is 1 values; latet server keys limit raises it"
    }


async def test_unmask_is_in_the_administration_log_without_values(server):
    key, secret = server.identities.add_key("Billing CI")
    token, reader = _token(server, key.id, "support", ["unmask"])
    async with _calling(server) as client:
        masked = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}", f"IBAN {IBAN}"]})).json()
        fakes = [result["text"] for result in masked["results"]]
        restored = await _post(client, reader, "unmask", {"texts": [*fakes, "nothing here"]})
    assert restored.status_code == 200
    [entry] = server.audit.query(action="apps.unmasked")
    assert (entry.actor, entry.target, entry.address) == (key.id, key.id, WHERE[0])
    assert entry.details == {"token": token, "texts": 3, "restored": 2}
    written = json.dumps([[e.action, e.details] for e in server.audit.query()])
    assert CANARY not in written and IBAN not in written and not any(fake in written for fake in fakes)


@pytest.mark.parametrize(
    "failure",
    [
        lambda: StoresUnavailable("OperationalError"),
        lambda: psycopg.OperationalError("connection to server at db.acme.lan failed"),
        lambda: RuntimeError("a fault of Latet's own"),
    ],
    ids=["stores", "postgresql", "own-fault"],
)
async def test_unmask_refused_when_the_log_cannot_be_written(server, monkeypatch, failure):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        fake = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]

        def unwritable(*args, **kwargs):
            raise failure()

        monkeypatch.setattr(server.audit, "record", unwritable)
        refused = await _post(client, secret, "unmask", {"texts": [fake]})
    assert refused.status_code == 503
    assert refused.json() == {
        "error": "the unmask could not be written in the administration log, so nothing was restored: try again"
    }
    assert CANARY not in refused.text and fake not in refused.text


async def test_the_unmask_entry_is_written_in_the_log_threads_before_the_answer(server, monkeypatch):
    """Ruling T5a: the entry waits on the administration log's chain (one entry at a time for the whole server) in the
    server's threads of the applications API's entries, never on the space's one bridge thread nor in the store threads,
    and is written before the answer is made."""
    _, secret = server.identities.add_key("Billing CI")
    done: list[tuple[str, str]] = []
    record, rendered = server.audit.record, apps_module._rendered

    def recording(*args, **kwargs):
        done.append(("written", threading.current_thread().name))
        return record(*args, **kwargs)

    def rendering(answer):
        done.append(("rendered", threading.current_thread().name))
        return rendered(answer)

    async with _calling(server) as client:
        fake = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        monkeypatch.setattr(server.audit, "record", recording)
        monkeypatch.setattr(apps_module, "_rendered", rendering)
        answer = await _post(client, secret, "unmask", {"texts": [fake]})
    assert answer.json() == {"results": [{"text": f"to {CANARY}", "restored": 1}]}
    [(first, writer), (then, renderer)] = done
    assert (first, then) == ("written", "rendered")
    assert writer.startswith("latet-apps-log") and renderer.startswith("latet-bridge")


def _waiting(server, lock):
    """How many connections wait for the advisory lock ``lock`` (PostgreSQL shows the bigint key as two halves)."""
    with server.db.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid::bigint = %s"
            " AND objid::bigint = %s AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            ((lock >> 32) & 0xFFFFFFFF, lock & 0xFFFFFFFF),
        ).fetchone()
    return row[0]


@pytest.mark.timing
async def test_unmasks_waiting_on_the_log_hold_no_other_call(server):
    """Ruling T5a: while the administration log's chain is busy (another entry being written, here a transaction that
    holds its lock), two unmasks of one key wait for it, and the key's other calls are answered meanwhile; once it is
    free, both unmasks answer."""
    _, secret = server.identities.add_key("Billing CI")
    lock = server.audit._lock
    async with _calling(server) as client:
        fake = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        holder = await asyncio.to_thread(psycopg.connect, DATABASE)
        unmasks: list[asyncio.Task] = []
        try:
            await asyncio.to_thread(holder.execute, "SELECT pg_advisory_xact_lock(%s)", (lock,))
            unmasks = [asyncio.create_task(_post(client, secret, "unmask", {"texts": [fake]})) for _ in range(2)]
            await until(lambda: _waiting(server, lock) >= 1, 10)
            scanned = await asyncio.wait_for(_post(client, secret, "scan", {"texts": [f"to {CANARY}"]}), 10)
            assert scanned.status_code == 200
            await until(lambda: _waiting(server, lock) == 2, 10)  # both on the chain, neither on the bridge thread
            assert not any(unmask.done() for unmask in unmasks)
        finally:
            await asyncio.to_thread(holder.close)  # its transaction rolled back: the chain goes on
            answers = await asyncio.wait_for(asyncio.gather(*unmasks, return_exceptions=True), 30)
    assert [answer.status_code for answer in answers] == [200, 200]
    assert all(answer.json() == {"results": [{"text": f"to {CANARY}", "restored": 1}]} for answer in answers)
    assert len(server.audit.query(action="apps.unmasked")) == 2


@pytest.mark.timing
async def test_unmasks_waiting_on_the_log_hold_no_store_work(server):
    """Task 8b's review, Important 1: a key's unmasks waiting on the administration log's chain, more of them than the
    server's store threads, queue in threads of their own: an enrolment's, a revocation's or the server's own work in the
    store threads is not queued behind them (here work that needs no chain at all answers at once)."""
    _, secret = server.identities.add_key("Billing CI")
    lock, many = server.audit._lock, STORE_THREADS + 2
    async with _calling(server) as client:
        fake = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
        holder = await asyncio.to_thread(psycopg.connect, DATABASE)
        unmasks: list[asyncio.Task] = []
        try:
            await asyncio.to_thread(holder.execute, "SELECT pg_advisory_xact_lock(%s)", (lock,))
            unmasks = [asyncio.create_task(_post(client, secret, "unmask", {"texts": [fake]})) for _ in range(many)]
            await until(lambda: _waiting(server, lock) >= 2, 10)
            await asyncio.sleep(0.5)  # the others restored and queued for the log meanwhile
            started = time.monotonic()
            assert await asyncio.wait_for(in_store(lambda: 1), 5) == 1
            took = time.monotonic() - started
            assert not any(unmask.done() for unmask in unmasks)
        finally:
            await asyncio.to_thread(holder.close)  # its transaction rolled back: the chain goes on
            answers = await asyncio.wait_for(asyncio.gather(*unmasks, return_exceptions=True), 30)
    assert took < 1.0
    assert [answer.status_code for answer in answers] == [200] * many
    assert len(server.audit.query(action="apps.unmasked")) == many


def _reviewed(runtime, fake, **settings):
    """The key's space with the stub local reviewer the reviewer's tests use (its server ``fake``)."""
    mediator = runtime.mediator
    mediator.llm_review = LlmReview(enabled=True, model="fake-model", **settings)
    mediator.reviewer = reviewer(fake, mediator.llm_review, memory=runtime.store)
    return mediator


async def test_the_local_reviewer_runs_on_mask(server):
    key, secret = server.identities.add_key("Billing CI")
    text = "mia moglie Maria ha la chemio giovedì"
    async with _calling(server) as client:
        fake = FakeServer()
        _reviewed(await server.spaces.runtime(key), fake)
        masked = (await _post(client, secret, "mask", {"texts": [text]})).json()["results"][0]
        scanned = (await _post(client, secret, "scan", {"texts": [text]})).json()["results"][0]
    assert "Maria" not in masked["text"] and "chemio" not in masked["text"]
    assert {r["class"] for r in masked["replacements"]} == {"llm"} and len(masked["replacements"]) == 2
    assert any("Maria" in sent for sent in fake.texts)
    # R1: scan runs no reviewer (it would teach the vault); what the reviewer taught the vault, it finds from then on.
    assert len(fake.texts) == 1 and _values_of(text, scanned["findings"]) == ["Maria", "chemio"]


@pytest.mark.parametrize(
    "narrowed",
    [{"inline": ["user_text"]}, {"inline": ["user_text"], "background": False}, {"inline": []}],
    ids=["prose-inline", "prose-inline-no-background", "background-only"],
)
async def test_every_text_is_reviewed_inline_whatever_the_gateway_waits_for(server, monkeypatch, narrowed):
    """``llm_review.inline`` narrows what the gateway's requests wait for, the rest reviewed after they leave; an
    application has no later moment: each text of its call is reviewed before the call answers, a ``data`` text too."""
    key, secret = server.identities.add_key("Billing CI")
    text = "mia moglie Maria ha la chemio giovedì"
    async with _calling(server) as client:
        fake = FakeServer()
        mediator = _reviewed(await server.spaces.runtime(key), fake, **narrowed)
        later: list = []
        monkeypatch.setattr(mediator, "submit_background", lambda *args: later.append(args))
        masked = (await _post(client, secret, "mask", {"texts": [text], "kind": "data"})).json()["results"][0]
    assert "Maria" not in masked["text"] and "chemio" not in masked["text"]
    assert {r["class"] for r in masked["replacements"]} == {"llm"}
    assert len(fake.texts) == 1 and "Maria" in fake.texts[0] and later == []


@pytest.mark.timing
async def test_a_stopped_text_is_blocked(server, monkeypatch):
    monkeypatch.setattr(apps_module, "APPS_REVIEW_SECONDS", 0.2)  # R8's time, for this test
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        fake = FakeServer(delay=30)
        mediator = _reviewed(await server.spaces.runtime(key), fake, on_failure="stop")
        later: list = []
        monkeypatch.setattr(mediator, "submit_background", lambda *args: later.append(args))
        started = time.monotonic()
        answer = await _post(client, secret, "mask", {"texts": ["mia moglie Maria ha la chemio giovedì", ""]})
        took = time.monotonic() - started
    assert answer.status_code == 200 and took < 10
    assert len(fake.texts) == 1 and later == []  # read once, inline: nothing of it goes to the background reviewer
    stopped, empty = answer.json()["results"]
    assert set(stopped) == {"blocked"} and "local reviewer" in stopped["blocked"] and "Maria" not in stopped["blocked"]
    assert empty == {"text": "", "replacements": [], "counts": {}}
    # The limit that applied is named: R8's own time, llm_review.budget_ms not being set (Task 8b's review, minor 1).
    assert "it took longer than the 0.2 seconds an application's call waits for it" in stopped["blocked"]


@pytest.mark.timing
async def test_a_review_past_budget_ms_names_that_setting(server):
    """With ``llm_review.budget_ms`` set, that is the review's time on an application's call, and the words of a review
    that did not end within it name it."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        _reviewed(await server.spaces.runtime(key), FakeServer(delay=30), on_failure="stop", budget_ms=500)
        answer = await _post(client, secret, "mask", {"texts": ["mia moglie Maria ha la chemio giovedì"]})
    [stopped] = answer.json()["results"]
    assert "it took longer than llm_review.budget_ms" in stopped["blocked"]
    assert "an application's call waits" not in stopped["blocked"]


async def test_a_mask_says_what_ran_weaker(server, monkeypatch):
    """A detector that did not read a text (here the personal data's, told to skip it, as the name model skips a text
    past its bound) is named in the answer's ``degraded``, never a value; a call where everything ran has none."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(key)
        [pii] = [d for d in runtime.mediator._masker(Session("apps", kept=False)).detectors if d.id == "pii"]
        real = pii.detect

        def skipping(text, ctx):
            if "skip me" in text:
                raise DetectorSkipped("told to skip it")
            return real(text, ctx)

        monkeypatch.setattr(pii, "detect", skipping)
        weaker = (await _post(client, secret, "mask", {"texts": [f"skip me: {PHONE}", f"to {CANARY}"]})).json()
        whole = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()
    assert weaker["degraded"] == ["pii"]  # named, the texts answered all the same
    assert [set(result) for result in weaker["results"]] == [{"text", "replacements", "counts"}] * 2
    assert "degraded" not in whole and CANARY not in json.dumps(weaker)


@pytest.mark.timing
async def test_a_review_that_did_not_end_in_time_is_said_when_its_text_is_sent(server, monkeypatch):
    """R8 with ``on_failure: send``: past its time the text is answered as the rules masked it, and the answer says the
    local reviewer did not read it."""
    monkeypatch.setattr(apps_module, "APPS_REVIEW_SECONDS", 0.2)
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        _reviewed(await server.spaces.runtime(key), FakeServer(delay=30), on_failure="send")
        answer = (await _post(client, secret, "mask", {"texts": [f"mia moglie Maria, {CANARY}"]})).json()
    assert answer["degraded"] == ["llm-review"]
    [sent] = answer["results"]
    assert "Maria" in sent["text"] and CANARY not in sent["text"] and sent["counts"] == {"EMAIL": 1}


async def test_no_live_reviewer(server):
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(
            runtime.update_config,
            lambda current: current.model_copy(update={"live_review": LiveReview(enabled=True, timeout_s=600)}),
        )
        assert runtime.mediator.live_config(Session("apps", kept=False)) is not None  # on for the key's every text
        answer = await asyncio.wait_for(_post(client, secret, "mask", {"texts": [f"to {CANARY}"]}), 10)
        approvals = runtime.mediator.approvals
    assert answer.status_code == 200 and CANARY not in answer.json()["results"][0]["text"]
    assert approvals.batches == {}  # nothing waits for a person


async def test_a_mask_keeps_nothing_of_a_conversation(server):
    """An application's call is no conversation: none of its texts is kept as it left (``given``), for anyone."""
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        assert (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).status_code == 200
    bus = server.bus
    assert bus.client.exists(bus.key(f"givens:{key.id}")) == 0
    assert list(bus.client.scan_iter(match=bus.key(f"given:{key.id}:*"))) == []


async def test_a_tool_output_past_the_dump_guard_is_blocked(server):
    key, secret = server.identities.add_key("Billing CI")
    text = "a@acme.it, b@acme.it, c@acme.it"
    async with _calling(server) as client:
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(runtime.update_config, lambda current: current.model_copy(update={"dump_guard": 2}))
        data = (await _post(client, secret, "mask", {"texts": [text], "kind": "data"})).json()["results"][0]
        prose = (await _post(client, secret, "mask", {"texts": [text]})).json()["results"][0]
    assert set(data) == {"blocked"} and "more than the 2 allowed" in data["blocked"] and "acme" not in data["blocked"]
    assert prose["counts"] == {"EMAIL": 3}  # the guard is a tool output's: prose is masked as it is


@pytest.mark.timing
async def test_application_requests_are_held_within_requests_in_flight(server, monkeypatch):
    server.settings = server.settings.model_copy(update={"requests_in_flight": 1})
    key, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        _reviewed(await server.spaces.runtime(key), FakeServer(delay=2))
        slow = asyncio.create_task(_post(client, secret, "mask", {"texts": ["mia moglie Maria ha la chemio"]}))
        await asyncio.sleep(0.5)
        crowded = await _post(client, secret, "unmask", {"texts": ["x"]})
        assert (await slow).status_code == 200
    assert crowded.status_code == 429 and crowded.headers["retry-after"] == "2"
    assert "application requests" in crowded.json()["error"]


async def test_no_value_in_the_servers_log(server, monkeypatch):
    """What the server logs of the applications API's calls, refused and failed ones too, holds no text, no value, no
    fake and no token: the path, the status, the time, and an error by its type or by words that name no value."""
    server.settings = server.settings.model_copy(update={"vault_limit": 3})  # past its bound: a test's own
    _, secret = server.identities.add_key("Billing CI")
    with captured_logs(level="debug") as logs:
        async with _calling(server, app=server_app(server)) as client:
            fake = (await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})).json()["results"][0]["text"]
            assert (await _post(client, secret, "unmask", {"texts": [fake]})).status_code == 200
            assert (await _post(client, secret, "mask", {"texts": [CANARY], "kind": CANARY})).status_code == 400
            assert (await _post(client, secret, "mask", {"texts": [f"IBAN {IBAN}, {PHONE}"]})).status_code == 507
            assert (await _post(client, "latet_key_" + "x" * 43, "scan", {"texts": [CANARY]})).status_code == 401

            def unwritable(*args, **kwargs):
                raise StoresUnavailable("OperationalError")

            monkeypatch.setattr(server.audit, "record", unwritable)
            assert (await _post(client, secret, "unmask", {"texts": [fake]})).status_code == 503
    told = logs.getvalue()
    assert f"{APPS_API}/unmask" in told and "507" in told  # the calls are logged...
    for value in (CANARY, "acme.it", IBAN, PHONE, fake, secret):
        assert value not in told, value  # ...and nothing of what they carried


async def test_metrics_count_apps_calls(server):
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server, app=server_app(server)) as client:
        await _post(client, secret, "scan", {"texts": [f"to {CANARY}", IBAN]})
        await _post(client, secret, "mask", {"texts": [f"to {CANARY}"]})
        fake = (await _post(client, secret, "mask", {"texts": [f"IBAN {IBAN}"]})).json()["results"][0]["text"]
        await _post(client, secret, "unmask", {"texts": [fake, "x", "y"]})
    text = server.metrics.render()
    assert 'latet_http_requests_total{kind="apps",code="200"} 4' in text
    for line in (
        'latet_apps_texts_total{operation="scan"} 2',
        'latet_apps_texts_total{operation="mask"} 2',
        'latet_apps_texts_total{operation="unmask"} 3',
        'latet_apps_values_total{operation="scan"} 2',
        'latet_apps_values_total{operation="mask"} 2',
        'latet_apps_values_total{operation="unmask"} 1',
    ):
        assert line in text, line
    assert CANARY not in text and IBAN not in text and secret not in text
