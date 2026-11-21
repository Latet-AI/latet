"""The three SIEM outputs, each sent to a receiver of its own: a JSON webhook (an array, or one event a line) with its
headers and signature, Splunk's HEC with its token, syslog over TLS; a refusal told apart from a receiver that cannot
take it now; a credential written where the secret belongs refused; and nothing sent where the server may not reach,
whatever the name resolves to, nor through a proxy of the environment."""

import gzip
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
import threading
import time
import tracemalloc
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from latet.ee.events import siem
from latet.ee.events.siem import NOT_REACHED, SendError, SyslogOutput, make_output, send_test
from latet.ee.settings import (
    _UNREACHED,
    EnterpriseSettings,
    EnterpriseSettingsError,
    SiemOutput,
    settings_from,
    siem_bound,
    siem_problem,
    with_section,
)
from latet.events import FIELDS, make_event
from latet.server import egress

from .. import addresses
from ..editions import MemorySecrets
from .receivers import HttpsReceiver, SyslogReceiver, certificate, trusting, until

T0 = 1_790_000_000.0  # 2026-09-21T14:13:20Z
TOKEN = "5f0c" + "9a2e-1b7d-4c3f-8e6a-2d9b0c1e7f45"


def _events(n=3):
    return [
        make_event("exchange", clock=lambda i=i: T0 + i, group="g_payments", agent="codex", masked={"EMAIL": i + 1})
        for i in range(n)
    ]


@pytest.fixture
def tls(tmp_path):
    cert, key = certificate(tmp_path)
    return cert, key, trusting(cert)


def test_a_webhook_gets_a_json_array_with_its_headers_and_a_signature(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    headers = {"Authorization": "ApiKey {secret}", "X-Source": "latet"}
    output = make_output(SiemOutput(name="elastic", kind="webhook", url=receiver.url, headers=headers,
                                    secret="siem-elastic"), TOKEN, context)  # fmt: skip
    events = _events()
    output.send(events)
    [(got, body)] = receiver.received
    assert json.loads(body) == events and got["content-type"] == "application/json"
    assert (got["authorization"], got["x-source"]) == (f"ApiKey {TOKEN}", "latet")
    assert got["x-latet-signature"] == "sha256=" + hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
    output.close()
    receiver.stop()


def test_a_webhook_in_ndjson_gets_one_event_a_line(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    output = make_output(SiemOutput(name="lines", kind="webhook", url=receiver.url, format="ndjson"), None, context)
    events = _events()
    output.send(events)
    assert receiver.events("ndjson") == events and "x-latet-signature" not in receiver.received[0][0]
    output.close()
    receiver.stop()


def test_splunk_gets_each_event_in_its_envelope_with_its_token(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    settings = SiemOutput(name="splunk", kind="splunk", url=receiver.url, secret="siem-splunk")
    output = make_output(settings, TOKEN, context, host="latet.acme.lan")
    events = _events()
    output.send(events)
    envelopes = receiver.events("splunk")
    assert [e["event"] for e in envelopes] == events
    assert envelopes[0] | {"event": None} == {"time": T0, "host": "latet.acme.lan", "source": "latet",
                                              "sourcetype": "latet:event", "event": None}  # fmt: skip
    assert receiver.received[0][0]["authorization"] == f"Splunk {TOKEN}"
    output.close()
    receiver.stop()


def test_syslog_gets_rfc_5424_messages_framed_by_their_length(tls):
    cert, key, context = tls
    receiver = SyslogReceiver(cert, key).start()
    output = make_output(
        SiemOutput(name="syslog", kind="syslog", url=receiver.url), None, context, host="latet.acme.lan"
    )
    events = [*_events(2), make_event("stopped", clock=lambda: T0, stopped=1, reason="leak")]
    output.send(events)
    assert until(lambda: len(receiver.messages) == 3)
    first, _, last = receiver.messages
    assert first.startswith("<110>1 2026-09-21T14:13:20Z latet.acme.lan latet - exchange - {")  # facility 13, info
    assert json.loads(first.split(" - ", 2)[2]) == events[0]
    assert last.startswith("<108>1 2026-09-21T14:13:20Z latet.acme.lan latet - stopped - ")  # a warning
    receiver.stop()


def test_a_receiver_whose_certificate_is_not_trusted_gets_nothing(tls, tmp_path):
    cert, key, _ = tls
    other, _ = certificate(tmp_path, "other")
    receiver = HttpsReceiver(cert, key).start()
    output = make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, trusting(other))
    with pytest.raises(SendError, match="certificate is not trusted") as caught:
        output.send(_events(1))
    assert caught.value.retry and receiver.received == []
    output.close()
    receiver.stop()


@pytest.mark.parametrize(
    ("status", "retry"),
    [(400, False), (413, False), (422, False), (401, True), (429, True), (503, True), (405, True), (410, True),
     (411, True), (415, True)],  # 405, 410, 411, 415: a receiver moved or set otherwise, never the batch itself
)  # fmt: skip
def test_a_refused_batch_and_a_receiver_that_cannot_take_it_now_are_told_apart(tls, status, retry):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    receiver.status = status
    settings = SiemOutput(name="hook", kind="webhook", url=receiver.url + "?code={secret}", secret="siem-hook")
    output = make_output(settings, "k3y-" + "e4f1", context)
    with pytest.raises(SendError) as caught:
        output.send(_events(1))
    assert caught.value.retry is retry and str(caught.value) == f"it answered {status}"
    output.close()
    receiver.stop()


def test_a_receiver_that_is_down_is_tried_again_and_its_address_never_repeated(tls):
    _, _, context = tls
    settings = SiemOutput(name="hook", kind="webhook", url="https://127.0.0.1:1/in?sig={secret}", secret="siem-hook")
    output = make_output(settings, "s1g-" + "9d2c", context)
    with pytest.raises(SendError) as caught:
        output.send(_events(1))
    assert caught.value.retry and "127.0.0.1" not in str(caught.value) and "s1g-" not in str(caught.value)
    output.close()


@pytest.mark.parametrize("kind", ["webhook", "syslog"])
def test_a_name_that_resolves_where_the_server_may_not_reach_gets_nothing(tls, monkeypatch, kind):
    _, _, context = tls
    looked: list[str] = []

    def metadata(host, port):  # a name pointed at the cloud's metadata address after the output was saved
        looked.append(host)
        return [ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(egress, "resolve", metadata)
    url = "https://siem.acme.com/in" if kind == "webhook" else "tls://siem.acme.com:6514"
    output = make_output(SiemOutput(name="hook", kind=kind, url=url), None, context)
    with pytest.raises(SendError) as caught:
        output.send(_events(1))
    assert looked == ["siem.acme.com"] and caught.value.retry and str(caught.value) == NOT_REACHED
    assert "169.254" not in str(caught.value) and "siem.acme.com" not in str(caught.value)
    output.close()


def test_a_compressed_answer_is_never_read_nor_inflated_its_status_alone_said(tls, monkeypatch):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    receiver.answer, receiver.encoding = gzip.compress(bytes(50_000_000)), "gzip"  # 50 MB of zeros in some 50 KB (C1)
    read: list[int] = []
    raw = httpx.Response.aiter_raw

    async def counted(self, chunk_size=None):
        async for chunk in raw(self, chunk_size):
            read.append(len(chunk))
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_raw", counted)
    output = make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context)
    tracemalloc.start()
    try:
        output.send(_events(1))  # taken: a receiver that compresses its answer is never sent the batch again
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 10_000_000 and read == []  # never inflated, not a byte of it read
    assert receiver.received[0][0]["accept-encoding"] == "identity"
    receiver.status = 503
    with pytest.raises(SendError) as caught:
        output.send(_events(1))
    assert caught.value.retry and str(caught.value) == "it answered 503" and read == []
    output.close()
    receiver.stop()


def test_a_receiver_that_drips_its_answer_is_cut_at_the_sends_deadline(tls, monkeypatch):
    cert, key, context = tls
    monkeypatch.setattr(siem, "SEND_SECONDS", 1.5)
    receiver = HttpsReceiver(cert, key).start()
    receiver.drip = 0.4  # each read is quick, the whole is not: a timeout of each read never ends it (C1)
    output = make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context)
    started = time.monotonic()
    with pytest.raises(SendError, match="did not answer within") as caught:
        output.send(_events(1))
    assert caught.value.retry and time.monotonic() - started < 3
    output.close()
    receiver.stop()


def test_a_token_written_secret_in_the_path_is_filled_when_sent(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    settings = SiemOutput(name="sumo", kind="webhook", url=receiver.url + "/{secret}", secret="siem-sumo")
    output = make_output(settings, "ZaVnC4dh" + "aV2x9Tn37Pq0", context)
    output.send(_events(1))
    assert receiver.paths == ["/collect/ZaVnC4dhaV2x9Tn37Pq0"]
    output.close()
    receiver.stop()


def test_syslogs_host_name_is_ascii_whatever_the_servers_name():
    settings = SiemOutput(name="syslog", kind="syslog", url="tls://siem.acme.com:6514")
    output = SyslogOutput(settings, siem.default_context(), host="latet.b" + chr(0xFC) + "ro.acme.lan")
    framed = output.frame(make_event("exchange", clock=lambda: T0)).decode("ascii")
    assert " latet.xn--bro-hoa.acme.lan latet - exchange - " in framed  # IDNA: the frame stays ASCII (I8)


def test_a_secret_is_sealed_for_its_output_and_where_it_sends():
    splunk = SiemOutput(name="splunk", kind="splunk", url="https://splunk.acme.com:8088/services/collector/event",
                        secret="siem-splunk")  # fmt: skip
    assert siem_bound(splunk) == "siem:splunk:splunk:https://splunk.acme.com:8088/services/collector/event"
    moved = splunk.model_copy(update={"url": "https://collector.other.example/services/collector/event"})
    assert siem_bound(moved) != siem_bound(splunk)  # I1
    # The path alone changed, on the same origin (a company's ingress, a collector of many tenants): set again (R2-3).
    path_only = splunk.model_copy(update={"url": "https://splunk.acme.com:8088/services/collector/raw"})
    assert siem_bound(path_only) != siem_bound(splunk)
    query_only = splunk.model_copy(update={"url": "https://splunk.acme.com:8088/services/collector/event?channel=2"})
    assert siem_bound(query_only) != siem_bound(splunk)
    same = splunk.model_copy(update={"url": "https://SPLUNK.acme.com:8088/services/collector/event"})
    assert siem_bound(same) == siem_bound(splunk)  # a host's case is no change of address


def test_a_proxy_of_the_environment_is_never_used(tls, monkeypatch):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # nothing answers there: a proxy used would fail the send
    output = make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context)
    output.send(_events(1))
    assert len(receiver.received) == 1
    output.close()
    receiver.stop()


def test_the_test_event_says_whether_it_arrived_and_holds_only_the_schema(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    settings = SiemOutput(name="hook", kind="webhook", url=receiver.url)
    assert send_test(settings, None, context) == (True, "it arrived")
    [sent] = json.loads(receiver.received[0][1])
    assert sent["type"] == "test" and set(sent) == set(FIELDS)
    receiver.status = 503
    assert send_test(settings, None, context) == (False, "it answered 503")
    receiver.stop()


@pytest.mark.parametrize(
    ("output", "why"),
    [
        ({"kind": "webhook", "url": "https://siem.acme.com/in", "headers": {"Authorization": "Splunk 5f0c9a2e"}},
         "carries a credential"),
        ({"kind": "webhook", "url": "https://x.logic.azure.com/workflows/1/triggers?sig=Abc123"}, "carries a credential"),
        ({"kind": "webhook", "url": "http://siem.acme.com/in"}, "https://"),
        ({"kind": "webhook", "url": "https://emily:pw@siem.acme.com/in"}, "no user or password"),
        ({"kind": "syslog", "url": "udp://siem.acme.com:514"}, "tls://"),
        ({"kind": "splunk", "url": "https://splunk.acme.com:8088/services/collector/event"}, "HEC token"),
        ({"kind": "webhook", "url": "https://siem.acme.com/in", "headers": {"X-Source": "{secret}"}}, "name the secret"),
        ({"kind": "syslog", "url": "tls://siem.acme.com:6514", "format": "ndjson"}, "a webhook's only"),
        # The metadata address and fe80::1 are egress's verdict, given when the section is saved (fix round 1: below).
        ({"kind": "webhook", "url": "https://127.0.0.2/in"}, "never reaches"),  # this computer, named otherwise
        ({"kind": "splunk", "url": "https://splunk.acme.com:8088/x", "secret": "sso-client-secret"}, "siem-"),
        ({"kind": "splunk", "url": "https://splunk.acme.com:8088/x", "secret": "siem-splunk"}, "named after it"),  # I1
        ({"kind": "webhook", "url": "https://siem.acme.com/in", "headers": {"X-Forwarded-For": "10.0.0.1"}},
         "a proxy or a browser sets"),  # I2
        ({"kind": "webhook", "url": "https://siem.acme.com/in", "headers": {"Host": "intranet.acme.lan"}}, "Latet sets"),
        ({"kind": "webhook", "url": "https://siem.acme.com/in", "headers": {"X-Source": "Z" + chr(0xFC) + "rich"}},
         "visible ASCII"),  # I8: httpx would raise at each send
    ],
)  # fmt: skip
def test_an_output_that_would_hold_a_credential_or_cannot_work_is_refused(output, why):
    with pytest.raises(ValidationError, match=why):
        SiemOutput.model_validate({"name": "out", **output})


TOKEN_CASES = Path(__file__).resolve().parents[3] / "dashboard" / "src" / "ee" / "lib" / "siemTokenCases.json"


@pytest.mark.parametrize("case", json.loads(TOKEN_CASES.read_text("utf-8")), ids=lambda case: case["name"])
def test_a_token_in_the_path_is_judged_as_the_dashboard_judges_it(case):
    kind = case.get("kind", "webhook")  # Splunk's channel is exempt on a Splunk output alone (fix round 2)
    output = {"name": "hook", "kind": kind, "url": case["url"], "secret": "siem-hook"}  # I3, R2-11
    if case["token"]:  # the dashboard's tests read the same cases (Task 15): the two rules stay in step
        with pytest.raises(ValidationError, match="carries a token"):
            SiemOutput.model_validate(output)
    else:
        assert SiemOutput.model_validate(output).url


def test_each_output_has_a_name_of_its_own_and_http_is_for_the_server_itself_only():
    one = {"name": "same", "kind": "syslog", "url": "tls://siem.acme.com:6514"}
    with pytest.raises(ValidationError, match="name of its own"):
        EnterpriseSettings.model_validate({"siem": {"outputs": [one, one]}})
    assert SiemOutput.model_validate({"name": "local", "kind": "webhook", "url": "http://127.0.0.1:9880/in"}).url
    # The company's own network is an administrator's to name (egress's `inside`), as for its identity provider.
    assert SiemOutput.model_validate({"name": "inside", "kind": "syslog", "url": "tls://10.0.0.5:6514"}).url
    # A token in the path is written {secret}; a Logic App's workflow id is no token (I3, R2-11: only there).
    sumo = "https://endpoint4.collection.sumologic.com/receiver/v1/http/{secret}"
    assert SiemOutput.model_validate({"name": "sumo", "kind": "webhook", "url": sumo, "secret": "siem-sumo"}).url
    workflow = ("https://prod-12.westeurope.logic.azure.com/workflows/0f3c1a2b4d5e6f708192a3b4c5d6e7f8/triggers/manual/"
                "paths/invoke?sig={secret}")  # fmt: skip
    assert SiemOutput.model_validate(
        {"name": "sentinel", "kind": "webhook", "url": workflow, "secret": "siem-sentinel"}
    )
    local = SiemOutput.model_validate({"name": "local", "kind": "webhook", "url": "http://127.0.0.1:9880/in"})
    assert local.local and not SiemOutput.model_validate({"name": "s", "kind": "syslog", "url": "tls://10.0.0.5"}).local
    dotted = SiemOutput.model_validate({"name": "dotted", "kind": "webhook", "url": "http://LocalHost.:9880/in"})
    assert dotted.local  # R2-5: `localhost.` and its case are this computer: `latet server siem set`'s alone


# --- beyond the brief: what the review of this task's code found (each a refusal or a bound of its own) ----------------


def test_this_computer_with_its_trailing_dot_is_local_over_https_too():
    dotted = SiemOutput.model_validate({"name": "dotted", "kind": "webhook", "url": "https://localhost./in"})
    assert dotted.local and dotted.destination == ("https", "localhost", 443)  # R2-5: never the dashboard's to save


@pytest.mark.parametrize(
    "url",
    [
        "https://siem.acme.com:8o88/in",  # a port that is no number: urllib's own words would quote it
        "https://siem.acme.com:99999/in",  # past 65535
        "https://[fe80::1/in",  # a bracket left open
        "https://[siem.acme.com]/in",  # brackets around what is no IPv6 address
    ],
)
def test_an_address_that_cannot_be_read_is_refused_in_words_that_never_quote_it(url):
    with pytest.raises(ValidationError, match="cannot be read") as caught:
        SiemOutput.model_validate({"name": "out", "kind": "webhook", "url": url})
    assert not any(part in str(caught.value) for part in ("8o88", "99999", "fe80", "siem.acme.com"))


@pytest.mark.parametrize(
    ("output", "why"),
    [
        ({"url": "https://{secret}.collector.acme.com/in", "secret": "siem-out"}, "never in its host"),  # to the DNS
        ({"url": "https://siem.acme.com:{secret}/in", "secret": "siem-out"}, "never in its host"),
        ({"url": "https://siem.acme.com/in#{secret}", "secret": "siem-out"}, "no fragment"),
        ({"url": "https://siem.acme.com/in" + chr(10) + "x"}, "visible ASCII"),
        ({"url": "https://siem.acme.com/in x"}, "visible ASCII"),
        ({"url": "https://b" + chr(0xFC) + "ro.acme.com/in"}, "visible ASCII"),
        ({"url": "https://siem.acme.com/in", "headers": {"Accept-Encoding": "gzip"}}, "Latet sets"),  # C1's identity
        ({"url": "https://siem.acme.com/in", "headers": {"X-HTTP-Method-Override": "DELETE"}}, "a proxy or a browser"),
        ({"url": "https://siem.acme.com/in", "headers": {"X-Source": "a", "x-source": "b"}}, "written once"),
        ({"url": "https://siem.acme.com/in", "headers": {"X-Source": "a" * 4097}}, "4096"),
        ({"url": "https://siem.acme.com/in", "headers": {"X-Api-Key": "%7Bsecret%7D"}}, "carries a credential"),
        # Fix round 1: `key` and `auth` as parts of a name carry a credential (Azure Functions, API Management, New
        # Relic's insert and license keys, a bare X-Auth), in a header or the query.
        *(({"url": "https://siem.acme.com/in", "headers": {name: "k3y-e4f1"}}, "carries a credential")
          for name in ("X-Functions-Key", "Ocp-Apim-Subscription-Key", "X-Insert-Key", "X-License-Key", "X-Auth")),
        ({"url": "https://siem.acme.com/in?subscription-key=k3y"}, "carries a credential"),
        ({"url": "https://siem.acme.com/in?auth=k3y"}, "carries a credential"),
        # Fix round 2: a credential, a password by its short names, an authentication.
        *(({"url": "https://siem.acme.com/in", "headers": {name: "k3y-e4f1"}}, "carries a credential")
          for name in ("X-Credential", "X-Api-Credentials", "X-Pwd", "X-Db-Passwd", "X-Pass", "X-Authentication",
                       "X-Authn")),
        *(({"url": f"https://siem.acme.com/in?{name}=k3y"}, "carries a credential")
          for name in ("pass", "pwd", "passwd", "credentials", "authentication")),
        # Fix round 1: more headers a proxy or a CDN sets.
        *(({"url": "https://siem.acme.com/in", "headers": {name: "10.0.0.1"}}, "a proxy or a browser")
          for name in ("CF-Connecting-IP", "Fastly-Client-IP", "X-Remote-IP", "X-Remote-Addr", "X-Originating-IP",
                       "X-Host", "X-Forwarded", "X-Envoy-Original-Path", "X-Azure-ClientIP")),
        # Fix round 1: a host is letters, digits, dots and hyphens, or an IPv6 address in brackets (no zone).
        ({"url": "https://siem_1.acme.com/in"}, "a host is"),
        ({"url": "https://[fe80::1%25eth0]/in"}, "a host is"),
    ],
)  # fmt: skip
def test_an_address_or_a_header_that_would_carry_the_secret_elsewhere_or_break_a_request_is_refused(output, why):
    with pytest.raises(ValidationError, match=why):
        SiemOutput.model_validate({"name": "out", "kind": "webhook", **output})


@pytest.mark.parametrize(
    "headers",
    [{"X-Functions-Key": "{secret}"}, {"Ocp-Apim-Subscription-Key": "{secret}"}, {"X-Auth": "Bearer {secret}"},
     {"X-Monkey": "1"}, {"X-Keyboard": "1"}, {"X-Author": "latet"}, {"X-Authority": "latet"}, {"X-Passthrough": "1"},
     {"X-Compass": "north"}, {"X-Bypass": "1"}],  # a part of the name, never a word inside one
)  # fmt: skip
def test_a_header_whose_name_says_key_or_auth_takes_the_secret_and_other_names_do_not_need_it(headers):
    secret = "siem-out" if "{secret}" in "".join(headers.values()) else None
    output = {"name": "out", "kind": "webhook", "url": "https://siem.acme.com/in", "headers": headers}
    assert SiemOutput.model_validate({**output, "secret": secret}).headers == headers


@pytest.mark.parametrize(
    "url",
    [
        "https://127.1/in",  # numbers the system's resolver reads as 127.0.0.1, as E3's issuer_problem counts them
        "https://2130706433/in",
        "https://0x7f000001/in",
        "https://10.1/in",  # numbers alone are written as an address is: 10.0.0.1
        "tls://latet.localhost:6514",  # this computer under a name of its own
        "https://[::ffff:127.0.0.1]/in",  # an IPv6 form of 127.0.0.1
        "https://[0:0:0:0:0:0:0:1]/in",  # [::1] written otherwise
    ],
)
def test_this_computer_under_another_name_is_refused_by_the_settings_themselves(url):
    kind = "syslog" if url.startswith("tls") else "webhook"
    with pytest.raises(ValidationError, match="never reaches"):
        SiemOutput.model_validate({"name": "out", "kind": kind, "url": url})
    assert SiemOutput.model_validate({"name": "out", "kind": "webhook", "url": "https://10.0.0.1/in"}).url


def test_a_secret_goes_into_the_address_encoded_and_never_into_a_header_it_would_break(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    settings = SiemOutput(name="sumo", kind="webhook", url=receiver.url + "/{secret}?k=1", secret="siem-sumo")
    make_output(settings, "a/b?c#d e", context).send(_events(1))
    assert receiver.paths == ["/collect/a%2Fb%3Fc%23d%20e?k=1"]  # the secret's characters never reshape the address
    headed = SiemOutput(name="hook", kind="webhook", url=receiver.url, headers={"Authorization": "Bearer {secret}"},
                        secret="siem-hook")  # fmt: skip
    splunk = SiemOutput(name="splunk", kind="splunk", url=receiver.url, secret="siem-splunk")
    for settings in (headed, splunk):
        for secret in ("k3y" + chr(13) + chr(10) + "x-injected: 1", "k3y" + chr(0xFC)):
            with pytest.raises(SendError, match="visible ASCII") as caught:
                make_output(settings, secret, context).send(_events(1))
            assert caught.value.retry and "k3y" not in str(caught.value)  # held, never dropped, never quoted
    assert len(receiver.received) == 1
    receiver.stop()


def test_an_output_that_names_a_secret_sends_nothing_while_it_is_not_set(tls):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    settings = SiemOutput(name="hook", kind="webhook", url=receiver.url, secret="siem-hook")
    with pytest.raises(SendError, match="secret is not set") as caught:
        make_output(settings, None, context).send(_events(1))
    assert caught.value.retry and receiver.paths == []  # never sent unsigned to a receiver that expects a signature
    unnamed = SiemOutput(name="hook", kind="webhook", url=receiver.url)  # names none: a secret given is not its own
    make_output(unnamed, "s3cr3t", context).send(_events(1))
    assert "x-latet-signature" not in receiver.received[0][0]
    receiver.stop()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_never_followed_and_holds_the_batch(tls, status):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    receiver.status = status
    output = make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context)
    with pytest.raises(SendError) as caught:
        output.send(_events(1))
    assert caught.value.retry and str(caught.value) == f"it answered {status}" and receiver.paths == ["/collect"]
    receiver.stop()


def test_an_answer_is_read_64_kb_at_most(tls, monkeypatch):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    receiver.answer = bytes(20_000_000)  # 20 MB the receiver would send, uncompressed (C1)
    read: list[int] = []
    raw = httpx.Response.aiter_raw

    async def counted(self, chunk_size=None):
        async for chunk in raw(self, chunk_size):
            read.append(len(chunk))
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_raw", counted)
    make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context).send(_events(1))
    assert siem.ANSWER_BYTES < sum(read) <= siem.ANSWER_BYTES + 256 * 1024  # what it says past them is never read
    receiver.stop()


def test_the_client_reads_nothing_of_the_environment(tls, monkeypatch):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    made: list[object] = []

    class Seen(httpx.AsyncClient):
        def __init__(self, **kwargs):
            made.append(kwargs.get("trust_env"))
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Seen)
    make_output(SiemOutput(name="hook", kind="webhook", url=receiver.url), None, context).send(_events(1))
    assert made == [False]  # M14: no proxy and no .netrc, whatever transport a later httpx would mount for them
    receiver.stop()


def test_a_syslog_frames_header_holds_a_time_and_a_type_as_the_schema_writes_them_or_nothing():
    output = SyslogOutput(SiemOutput(name="syslog", kind="syslog", url="tls://siem.acme.com:6514"),
                          siem.default_context(), host="latet.acme.lan")  # fmt: skip
    forged = {"ts": "2026-09-21T14:13:20Z host latet - admin -", "type": "exchange - [x]", "project": 'a"] [b="c|'}
    size, _, message = output.frame(forged).decode("ascii").partition(" ")
    assert int(size) == len(message) and message.startswith("<110>1 - latet.acme.lan latet - - - {")
    assert json.loads(message.split(" - - - ", 1)[1]) == forged  # a project's " ] = | stay inside the JSON


@pytest.mark.parametrize("stall", ["handshake", "reading"])
def test_a_syslog_receiver_that_stalls_is_cut_at_the_sends_deadline(tls, monkeypatch, stall):
    cert, key, context = tls
    monkeypatch.setattr(siem, "SEND_SECONDS", 1.5)
    listener = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []

    def serve():  # takes the connection, then answers no handshake, or reads nothing once it is made
        conn, _ = listener.accept()
        held.append(conn)
        if stall == "reading":
            serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            serving.load_cert_chain(cert, key)
            held.append(serving.wrap_socket(conn, server_side=True))

    threading.Thread(target=serve, daemon=True).start()
    url = f"tls://127.0.0.1:{listener.getsockname()[1]}"
    output = SyslogOutput(SiemOutput(name="syslog", kind="syslog", url=url), context)
    monkeypatch.setattr(output, "frame", lambda event: bytes(50_000_000))  # more than any buffer on the way holds
    started = time.monotonic()
    with pytest.raises(SendError, match="did not answer within") as caught:
        output.send([{}])
    assert caught.value.retry and time.monotonic() - started < 3
    for sock in (*held, listener):
        sock.close()


def test_a_secret_opens_only_for_where_its_output_sends_now():
    secrets = MemorySecrets()  # as the master key: a secret opens only for what it was sealed for
    url = "https://splunk.acme.com:8088/services/collector/event"
    splunk = SiemOutput(name="splunk", kind="splunk", url=url, secret="siem-splunk")
    assert siem.secret_of(secrets, splunk) is None  # none kept yet
    secrets.save("siem-splunk", TOKEN, bound=siem_bound(splunk))
    assert siem.secret_of(secrets, splunk) == TOKEN
    moved = splunk.model_copy(update={"url": "https://splunk.acme.com:8088/services/collector/raw"})
    with pytest.raises(siem.SecretElsewhere):  # I1, R2-3: set again before it goes anywhere else
        siem.secret_of(secrets, moved)
    assert siem.secret_of(secrets, SiemOutput(name="hook", kind="webhook", url="https://siem.acme.com/in")) is None


def test_whatever_an_output_raises_is_said_by_its_type_never_by_its_words(tls, monkeypatch):
    def raising(self, events):
        raise RuntimeError("words that quote " + TOKEN)

    monkeypatch.setattr(siem.HttpOutput, "send", raising)
    settings = SiemOutput(name="hook", kind="webhook", url="https://siem.acme.com/in")
    assert send_test(settings, None, tls[2]) == (False, "it could not be sent (RuntimeError)")  # I8


def test_a_certificate_for_another_name_is_said_without_the_name():
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_code, error.verify_message = 62, "Hostname mismatch, certificate is not valid for 'siem.acme.com'."
    wrapped = httpx.ConnectError("failed")
    wrapped.__cause__ = error
    said = siem._why(wrapped)
    assert "certificate is not trusted" in said and "siem.acme.com" not in said


# --- fix round 1 -------------------------------------------------------------------------------------------------------


def test_egress_judges_an_output_when_the_section_is_saved_never_when_the_settings_are_read(monkeypatch):
    meta = {"name": "meta", "kind": "webhook", "url": "https://169.254.169.254/latest/meta-data"}
    inside = {"name": "inside", "kind": "syslog", "url": "tls://10.0.0.5:6514"}
    stored = {"outputs": [inside, meta]}
    loaded = settings_from({"siem": stored}, skip_unknown=True)  # read as a server reads its rows: never refused
    assert [output.name for output in loaded.siem.outputs] == ["inside", "meta"]
    assert siem_problem(loaded.siem) == f"siem.outputs.1: {_UNREACHED}"
    with pytest.raises(EnterpriseSettingsError, match=r"siem\.outputs\.1: the server never reaches"):
        with_section(loaded, "siem", stored)  # saved again: refused
    link = {"outputs": [{"name": "link", "kind": "syslog", "url": "tls://[fe80::1]:6514"}]}
    with pytest.raises(EnterpriseSettingsError, match="never reaches"):
        with_section(EnterpriseSettings(), "siem", link)
    assert with_section(loaded, "reports", {"people": "named"}).reports.people == "named"  # another section saves
    assert with_section(loaded, "siem", {"outputs": [inside]}).siem.outputs[0].name == "inside"  # meta taken out
    # Egress's verdict changes under a saved output (a NAT64 prefix learned, a newer Python): every setting still
    # reads, and the outputs are still changed; a save that keeps the output says why it is refused.
    monkeypatch.setattr(egress, "forbidden", lambda address, *, inside=False: True)
    assert settings_from({"siem": {"outputs": [inside]}}).siem.outputs[0].name == "inside"
    with pytest.raises(EnterpriseSettingsError, match="never reaches"):
        with_section(EnterpriseSettings(), "siem", {"outputs": [inside]})
    assert with_section(loaded, "siem", None).siem.outputs == []


def test_a_secret_is_sealed_for_the_headers_it_goes_in_too():
    hook = SiemOutput(name="hook", kind="webhook", url="https://siem.acme.com/in", secret="siem-hook",
                      headers={"Authorization": "Bearer {secret}", "X-Source": "latet"})  # fmt: skip
    secrets = MemorySecrets()
    secrets.save("siem-hook", TOKEN, bound=siem_bound(hook))
    copied = {"Authorization": "Bearer {secret}", "X-Copy": "{secret}", "X-Source": "latet"}  # the receiver logs X-Copy
    moved = {"X-Copy": "{secret}", "X-Source": "latet"}
    reworded = {"Authorization": "Token {secret}", "X-Source": "latet"}
    for headers in (copied, moved, reworded):
        with pytest.raises(siem.SecretElsewhere):  # set again before it goes in any other header
            siem.secret_of(secrets, hook.model_copy(update={"headers": headers}))
    same = {"x-source": "other", "authorization": "Bearer {secret}"}  # a header without it, a name's case: no change
    assert siem.secret_of(secrets, hook.model_copy(update={"headers": same})) == TOKEN


def test_outputs_whose_names_never_resolve_never_starve_another_output(tls, monkeypatch):
    cert, key, context = tls
    receiver = HttpsReceiver(cert, key).start()
    release, asked, failed = threading.Event(), [], []

    def resolving(host, port):
        if host == "localhost":
            return [ipaddress.ip_address("127.0.0.1")]
        asked.append(host)
        release.wait(30)  # a name whose server never answers
        return []

    monkeypatch.setattr(egress, "resolve", resolving)

    def stuck(i):
        output = SiemOutput(name=f"slow{i}", kind="webhook", url=f"https://slow{i}.acme.com/in")
        try:
            make_output(output, None, context).send(_events(1))
        except SendError as exc:
            failed.append(str(exc))

    threads = [threading.Thread(target=stuck, args=(i,), daemon=True) for i in range(egress.LOOKUPS_PER_HOLDER)]
    try:
        for thread in threads:
            thread.start()
        assert until(lambda: len(asked) == egress.LOOKUPS_PER_HOLDER)
        healthy = SiemOutput(name="healthy", kind="webhook", url=f"https://localhost:{receiver.port}/collect")
        make_output(healthy, None, context).send(_events(1))  # its lookups are its own: never the stuck ones' share
        assert len(receiver.received) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        receiver.stop()
    assert failed == [NOT_REACHED] * egress.LOOKUPS_PER_HOLDER


@pytest.mark.parametrize(
    ("code", "said"),
    [(10, "its certificate is not trusted (it has expired)"), (18, "its certificate is not trusted (it is self-signed)"),
     (0, "its certificate is not trusted"), (None, "its certificate is not trusted")],
)  # fmt: skip
def test_a_certificate_refused_is_said_in_fixed_words_never_in_the_verifiers(code, said):
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_code = code  # the system's verifier (truststore) writes words of its own, a name among them
    error.verify_message = "The chain built for 'siem.acme.com' ends in CN=Acme Root, which is not trusted."
    wrapped = httpx.ConnectError("failed")
    wrapped.__cause__ = error
    assert siem._why(wrapped) == said


# --- A receiver at a name with several addresses: the next one checked, when the connection to the one before failed --


@pytest.fixture
def at_localhost(tmp_path):
    """A receiver's certificate for ``localhost`` alone, and a context that trusts it."""
    cert, key = addresses.certificate(tmp_path / "localhost")
    return cert, key, addresses.trusting(cert)


def _output(kind: str, port: int, context: ssl.SSLContext) -> siem.Output:
    url = f"https://localhost:{port}/collect" if kind == "webhook" else f"tls://localhost:{port}"
    return make_output(SiemOutput(name="local", kind=kind, url=url), None, context, host="latet.acme.lan")


def _connects(monkeypatch, kind: str, refused=(addresses.PAST,)) -> list[str]:
    if kind == "webhook":
        return addresses.async_connects(monkeypatch, refused)
    return addresses.socket_connects(monkeypatch, refused)


@pytest.mark.parametrize("kind", ["webhook", "syslog"])
def test_a_receiver_on_this_computer_is_reached_at_the_next_address_its_lookup_checked(at_localhost, monkeypatch, kind):
    """Windows resolves ``localhost`` to ``::1`` first, and a receiver in Docker listens on 127.0.0.1 alone: the batch
    goes to 127.0.0.1 once ``::1`` refused the connection, TLS checked against ``localhost`` (the certificate names
    nothing else), and it arrives once; no address is tried after it."""
    cert, key, context = at_localhost
    receiver = (HttpsReceiver(cert, key) if kind == "webhook" else SyslogReceiver(cert, key)).start()
    asked = addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    connects = _connects(monkeypatch, kind)
    events = _events(2)
    try:
        with addresses.refused_on_v6(receiver.port):
            _output(kind, receiver.port, context).send(events)
        if kind == "webhook":
            assert receiver.events() == events and len(receiver.received) == 1
        else:
            assert until(lambda: len(receiver.messages) == 2)
            assert [json.loads(m.split(" - ", 2)[2]) for m in receiver.messages] == events
    finally:
        receiver.stop()
    assert connects == ["::1", "127.0.0.1"] and asked == ["localhost"]


@pytest.mark.parametrize("kind", ["webhook", "syslog"])
def test_a_receiver_is_connected_at_no_address_its_lookup_did_not_check(at_localhost, monkeypatch, kind):
    """The addresses tried are those egress's lookup checked, in its order, and nothing else; a name with an address
    the server never reaches among its answers gets nothing, nothing connected."""
    cert, key, context = at_localhost
    receiver = (HttpsReceiver(cert, key) if kind == "webhook" else SyslogReceiver(cert, key)).start()
    connects = _connects(monkeypatch, kind, refused=("::1", addresses.PAST))
    try:
        addresses.resolving(monkeypatch, *addresses.LOCALHOST)
        _output(kind, receiver.port, context).send(_events(1))
        assert connects == ["::1", "127.0.0.1"]
        connects.clear()
        addresses.resolving(monkeypatch, "::1", "127.0.0.1", addresses.METADATA)
        with pytest.raises(SendError) as caught:
            _output(kind, receiver.port, context).send(_events(1))
    finally:
        receiver.stop()
    assert str(caught.value) == NOT_REACHED and connects == []


@pytest.mark.parametrize("kind", ["webhook", "syslog"])
def test_a_batch_goes_to_no_other_address_once_its_connection_was_made(tmp_path, monkeypatch, kind):
    """Reached at 127.0.0.1, a webhook reads the batch and closes the connection without an answer: the batch is not
    sent again, at the next address or anywhere (the delivery holds it, to try it later). Nor is another address tried
    when TLS fails on the connection made (a receiver's certificate for another name), for syslog as for a webhook."""
    cert, key = addresses.certificate(tmp_path / "localhost")
    other, other_key = addresses.certificate(tmp_path / "elsewhere", "elsewhere.acme.lan")
    connects = _connects(monkeypatch, kind, refused=("::1", addresses.PAST))
    addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    if kind == "webhook":
        silent = addresses.Server(None, tls=addresses.serving(cert, key))
        try:
            with pytest.raises(SendError) as caught:
                _output(kind, silent.port, addresses.trusting(cert)).send(_events(1))
        finally:
            silent.close()
        assert caught.value.retry and connects == ["::1", "127.0.0.1"]
        assert silent.connections == 1 and len(silent.requests) == 1
        connects.clear()
    receiver = (HttpsReceiver(other, other_key) if kind == "webhook" else SyslogReceiver(other, other_key)).start()
    try:
        with pytest.raises(SendError, match="certificate is not trusted") as caught:
            _output(kind, receiver.port, addresses.trusting(cert)).send(_events(1))
    finally:
        receiver.stop()
    assert caught.value.retry and connects == ["::1", "127.0.0.1"]
    assert (receiver.received if kind == "webhook" else receiver.messages) == []
