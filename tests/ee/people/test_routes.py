"""What E3's routes share, without a server (ee/routes.py, ee/people_api.py): a dashboard request that names no one,
and the command that connects a computer."""

import datetime
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from latet.ee.people_api import connect_command
from latet.ee.routes import EeRoute, Who, dashboard_route
from latet.engine.reviewer.certificate import fingerprint_of

from ..servers import URL


async def _inline(work, *args, **kwargs):
    return work(*args, **kwargs)


def test_a_dashboard_request_that_names_no_one_is_refused_and_its_handler_never_called():
    """L2: no identity is never anyone, whatever the gates before it let through."""
    asked: list[Who] = []

    async def handler(request, who):
        asked.append(who)
        return JSONResponse({"ok": True})

    licensing = SimpleNamespace(licensed=lambda feature: True, manageable=lambda: True)
    edition = SimpleNamespace(server=SimpleNamespace(), people=SimpleNamespace(), licensing=licensing)
    dashboard = SimpleNamespace(guarded=lambda endpoint: endpoint, _in_pool=_inline)
    app = Starlette(routes=[dashboard_route(EeRoute("GET", "/people", handler), dashboard, edition)])  # type: ignore[arg-type]

    async def nameless(scope, receive, send):  # an empty identity, which no server hands on
        await app({**scope, "latet.identity": ""}, receive, send)

    with TestClient(app) as client:
        assert client.get("/_latet/api/v1/ee/people").status_code == 401
    with TestClient(nameless) as client:
        assert client.get("/_latet/api/v1/ee/people").status_code == 401
    assert asked == []
    named = Starlette(routes=[dashboard_route(EeRoute("GET", "/people", handler), dashboard, edition)])  # type: ignore[arg-type]

    async def emily(scope, receive, send):
        await named({**scope, "latet.identity": "u_emilycartr"}, receive, send)

    with TestClient(emily) as client:
        assert client.get("/_latet/api/v1/ee/people").json() == {"ok": True}
    [who] = asked
    assert (who.identity_id, who.actor, who.person) == ("u_emilycartr", "u_emilycartr", True)


def _certificate(folder, *, authority=False, issued_by="latet.acme.lan", file="server.crt"):
    """A certificate for the tests' server, written to ``folder``: self-signed by default, its own (CA:FALSE) or an
    authority; or one the company's authority ``issued_by`` signed."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "latet.acme.lan")])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issued_by)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=authority, path_length=None), critical=True)
        .sign(ec.generate_private_key(ec.SECP256R1()) if issued_by != "latet.acme.lan" else key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    path = folder / file
    path.write_bytes(pem.encode())
    return path, fingerprint_of(pem)


def test_the_command_that_connects_a_computer_pins_a_self_signed_certificate_of_the_server(tmp_path):
    """F14: E0c's shape, the code apart; `--fingerprint` when the instance serves a self-signed certificate of its
    own, which `latet connect` pins."""
    own, fingerprint = _certificate(tmp_path)
    assert (
        connect_command(SimpleNamespace(url=URL, certificate=own)) == f"latet connect {URL} --fingerprint {fingerprint}"
    )
    assert connect_command(SimpleNamespace(url=URL + "/", certificate=None)) == f"latet connect {URL}"
    authority, _ = _certificate(tmp_path, authority=True, file="authority.crt")  # never one to pin: refused
    assert connect_command(SimpleNamespace(url=URL, certificate=authority)) == f"latet connect {URL}"
    issued, _ = _certificate(tmp_path, issued_by="Acme Corporate CA", file="issued.crt")  # computers check it
    assert connect_command(SimpleNamespace(url=URL, certificate=issued)) == f"latet connect {URL}"
    gone = tmp_path / "gone.crt"
    assert connect_command(SimpleNamespace(url=URL, certificate=gone)) == f"latet connect {URL}"
