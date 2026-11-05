"""Compatibility of compact server launch bindings with the existing sealed format."""

import hmac

import pytest

from latet.server.launch_binding import Binding, BindingAddress, BindingCodec
from latet.server.launch_records import LaunchUnavailable, packed
from latet.server.sealing import Sealer


def test_server_binding_keeps_canonical_ancestry_and_the_existing_sealed_fields():
    folder, root, project = "/Repo/\U00010000-child", "/Repo", "a" * 12
    binding = Binding(folder, root, project, "client", "posix")
    fields = (folder.encode("utf-8"), b"/Repo", b"a" * 12, b"client", b"posix")
    assert (binding.folder, binding.root, binding.fields()) == (folder, root, fields)
    assert binding.contains(folder + "/descendant")
    assert not binding.contains("/repo/child")
    assert not binding.contains("/Repo/../other")

    windows = Binding(r"C:\repo\child", r"C:\Repo", project, "server", "windows")
    assert windows.contains(r"C:\REPO\sibling")
    assert not windows.contains(r"C:\Repository\child")
    with pytest.raises(LaunchUnavailable):
        Binding(r"C:\Repo\..\child", r"C:\Repo", project, "server", "windows")

    root_key, verifier, auth = b"r" * 32, b"v" * 32, b"a" * 32
    epoch, locator, launch_id, publication = b"e" * 16, b"l" * 32, b"i" * 16, b"p" * 16
    address = BindingAddress(epoch, "u_aaaaaaaaaa", locator, launch_id, 0, publication)
    key = b"test:launch"
    aad = "launch-binding-v1:test:launch:u_aaaaaaaaaa"
    plain = packed((b"1", epoch, b"u_aaaaaaaaaa", locator, launch_id, b"0", publication, verifier, *fields))
    prior_sealer = Sealer(root_key, b"launch-binding-v1")
    codec = BindingCodec(root_key)
    assert codec.open(key, address, prior_sealer.seal(plain, aad)) == (verifier, binding)
    assert prior_sealer.open(codec.seal(key, address, verifier, binding), aad) == plain
    digest = hmac.digest(auth, packed((b"launch-binding-v1", b"u_aaaaaaaaaa", locator, launch_id, *fields)), "sha256")
    assert binding.digest(auth, address.issuer, locator, launch_id) == digest
