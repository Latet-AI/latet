"""A lower bound on storage retained by the server launch binding itself."""

import gc
import json
import sys

from latet.gateway.launch_keys import BINDING_BYTES
from latet.server.launch_binding import Binding


def test_server_launch_binding_single_retained_component_fits_binding_budget():
    for segment in ("a" * 4095, "\U00010000" + "a" * 4091):
        folder = "/" + segment
        binding = Binding(folder, "/", "0" * 12, "client", "posix")
        assert len(binding.fields()[0]) == 4096
        wire = json.dumps({"folder": folder, "root": "/", "flavor": "posix"}, ensure_ascii=False, separators=(",", ":"))
        assert len(wire.encode("utf-8")) < 8192
        del folder, wire

        sizes = [sys.getsizeof(value) for value in gc.get_referents(binding) if type(value) in (str, bytes)]
        assert sizes
        component_bytes = max(sizes)
        assert component_bytes <= BINDING_BYTES
