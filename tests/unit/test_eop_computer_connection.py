"""Saved-pin, fallback storage, and CLI compatibility regressions for EC1-EC3."""

import builtins
import errno
import runpy
import ssl
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from latet.config import data_dir
from latet.engine.reviewer import certificate
from latet.server import client, connection

PIN = "sha256:" + "ab" * 32
MARKER = "planted_sensitive_exception_9714"


@pytest.mark.parametrize("raw", [b"not a PEM", b"\xff", b"-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----"])
def test_malformed_saved_certificate_is_unavailable_in_both_consumers(computer, raw):
    path = data_dir() / "certificates" / ("ab" * 32 + ".pem")
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(raw)
    assert certificate.load(data_dir(), PIN) is None
    context = client.tls_context(PIN)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.get_ca_certs(binary_form=True) == []


def test_missing_requested_pin_cannot_enter_public_trust(computer, monkeypatch):
    def public_trust(*args, **kwargs):
        pytest.fail("a requested missing pin entered system trust")

    monkeypatch.setattr(client.truststore, "SSLContext", public_trust)
    assert client.tls_context(PIN).get_ca_certs(binary_form=True) == []


@pytest.mark.parametrize("error", [errno.EACCES, errno.EROFS, errno.ENOTDIR])
def test_unusable_fallback_folder_has_a_safe_problem(computer, monkeypatch, error):
    folder = data_dir()
    original = Path.mkdir

    def cannot_create(self, *args, **kwargs):
        if self == folder:
            raise OSError(error, MARKER)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", cannot_create)
    problem = connection.token_file_problem(folder)
    assert problem is not None
    assert MARKER not in problem
    assert "file" in problem.lower() or "folder" in problem.lower()


def test_usable_keyring_does_not_require_a_working_fallback_folder(computer, keyring_on):
    folder = computer / "not-a-directory"
    folder.write_bytes(b"existing operator file")
    assert connection.token_file_problem(folder) is None
    assert folder.read_bytes() == b"existing operator file"


def test_not_connected_is_rendered_by_installed_typer_without_traceback(computer):
    app = typer.Typer()

    @app.command()
    def refuse():
        raise connection.NotConnected("connection unavailable")

    result = CliRunner().invoke(app, [])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "connection unavailable" in result.output
    assert "Traceback" not in result.output


def test_missing_bundled_typer_exception_uses_public_fallback(computer, monkeypatch):
    from click.exceptions import ClickException

    original = builtins.__import__

    def without_bundled(name, *args, **kwargs):
        if name == "typer._click.exceptions":
            raise ModuleNotFoundError("simulated absent bundled path", name=name)
        return original(name, *args, **kwargs)

    # A fresh module namespace avoids deleting/replacing shared sys.modules entries.
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", without_bundled)
        namespace = runpy.run_path(connection.__file__)
    assert issubclass(namespace["NotConnected"], ClickException)


def test_bundled_typer_initialization_error_is_not_hidden_by_fallback(computer, monkeypatch):
    original = builtins.__import__
    failure = RuntimeError("synthetic framework initialization failure")

    def broken_bundled(name, *args, **kwargs):
        if name == "typer._click.exceptions":
            raise failure
        return original(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", broken_bundled)
        with pytest.raises(RuntimeError) as caught:
            runpy.run_path(connection.__file__)
    assert caught.value is failure
