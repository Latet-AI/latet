"""The short launch-control parser accepts only bounded flat UTF-8 JSON."""

from starlette.requests import Request

from latet.bridge.api import Bridge
from latet.gateway.launch_ingress import valid_launch_scope
from latet.gateway.launch_keys import LaunchRefused


async def test_short_launch_control_rejects_ambiguous_or_nonflat_json():
    bridge = Bridge.__new__(Bridge)  # _launch_json only owns the short Request stream.
    nine = b"{" + b",".join(b'"a":1' for _ in range(9)) + b"}"
    cases = (
        (b"{}", set(), True),
        ('{"a":"escaped \\" and { braces }","b":"é"}'.encode(), {"a", "b"}, True),
        (b'{"a":1,"a":2}', {"a"}, False),
        (rb'{"a":1,"\u0061":2}', {"a"}, False),
        (rb'{"a":"\ud800"}', {"a"}, False),
        ('{"a":1}'.encode("utf-16"), {"a"}, False),
        ('{"a":1}'.encode("utf-32"), {"a"}, False),
        (b'{"a":[]}', {"a"}, False),
        (b'{"a":{}}', {"a"}, False),
        (b"[]", set(), False),
        (nine, {"a"}, False),
    )

    async def accepted(body, fields):
        sent = False

        async def receive():
            nonlocal sent
            assert not sent
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST"}, receive)
        try:
            await bridge._launch_json(request, fields)
        except LaunchRefused as error:
            code = error.code
        else:
            return True
        assert code == "launch-invalid"
        return False

    observed = [await accepted(body, fields) for body, fields, _ in cases]
    assert observed == [expected for _, _, expected in cases]


def test_launch_scope_bounds_short_controls_without_changing_hold_query_or_post_body():
    scope = {"raw_path": b"/" * 256, "headers": [(b"x", b"")] * 32}
    assert valid_launch_scope(scope)
    assert not valid_launch_scope({**scope, "raw_path": b"/" * 257})
    assert not valid_launch_scope({**scope, "headers": [(b"x", b"")] * 33})
    assert valid_launch_scope({**scope, "headers": [(b"x", b"x" * 4095)]})
    assert not valid_launch_scope({**scope, "headers": [(b"x", b"x" * 4096)]})
    post = {**scope, "headers": [(b"content-length", b"8")]}
    assert valid_launch_scope(post)
    held = {**scope, "query_string": b"generation=0"}
    assert not valid_launch_scope(held)
    assert valid_launch_scope(held, hold=True)
    assert not valid_launch_scope({**held, "query_string": b"x" * 65}, hold=True)
    assert not valid_launch_scope({**held, "headers": post["headers"]}, hold=True)
    assert not valid_launch_scope({**held, "headers": [(b"transfer-encoding", b"chunked")]}, hold=True)
    assert not valid_launch_scope({**held, "headers": [(b"x", b"latet_lk_" + b"x" * 43)]}, hold=True)
