"""docs/public/apps-api.openapi.json, the applications API's description published with the documentation, says what
the server answers: the four operations by POST alone, the most texts (or values to forget) a call takes, the kinds of
text, and for each operation exactly the statuses it can answer."""

import json
from pathlib import Path
from typing import Any

from latet.bridge.apps import APPS_API, KINDS, MOST_TEXTS

OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "public" / "apps-api.openapi.json"

# The statuses of each operation (``bridge.apps`` and the server's gate, ``ServerGate._apps``): 400 a body that is not
# one, 401 no token or a wrong one, 403 a page's request, a person's token or a permission the token lacks, 413 past
# ``max_request_mb``, 422 texts naming more of the vault than one call may weigh, 429 past a limit, 500 a failure, 503 a
# store that does not answer (or, for unmask, the administration log that did not take it), 507 a full vault, which
# only mask can fill. 404 (another path under the prefix) and 405 (another method) belong to no operation. A forget
# (Task 11b) weighs no candidates and fills no vault: no 422 and no 507; its 503 is also a log that did not take it.
_COMMON = {"200", "400", "401", "403", "413", "422", "429", "500", "503"}
STATUSES = {"scan": _COMMON, "mask": _COMMON | {"507"}, "unmask": _COMMON, "forget": _COMMON - {"422"}}
TEXTS = ("scan", "mask", "unmask")  # the operations that take texts


def _spec() -> dict[str, Any]:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def _resolved(spec: dict[str, Any], node: Any) -> Any:
    """``node`` with its ``$ref`` followed, as many times as it takes (a reference into this file only)."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), ref
        node = spec
        for part in ref[2:].split("/"):
            node = node[part]
    return node


def _body(spec: dict[str, Any], operation: str) -> dict[str, Any]:
    post = spec["paths"][f"{APPS_API}/{operation}"]["post"]
    content = _resolved(spec, post["requestBody"])["content"]["application/json"]
    return _resolved(spec, content["schema"])


def test_the_file_is_an_openapi_description():
    spec = _spec()
    assert spec["openapi"].startswith("3.1.")
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_it_has_the_four_operations_by_post_alone():
    spec = _spec()
    assert set(spec["paths"]) == {f"{APPS_API}/{operation}" for operation in STATUSES}
    for path, item in spec["paths"].items():
        assert set(item) == {"post"}, path


def test_every_operation_asks_for_an_api_keys_token():
    spec = _spec()
    schemes = spec["components"]["securitySchemes"]
    for operation in STATUSES:
        post = spec["paths"][f"{APPS_API}/{operation}"]["post"]
        (requirement,) = post.get("security", spec.get("security"))
        (name,) = requirement
        assert schemes[name]["type"] == "http"
        assert schemes[name]["scheme"].lower() == "bearer"


def test_a_call_takes_as_many_texts_as_the_server_does():
    spec = _spec()
    for operation, field in [*((operation, "texts") for operation in TEXTS), ("forget", "values")]:
        body = _body(spec, operation)
        assert body["required"] == [field], operation
        listed = _resolved(spec, body["properties"][field])
        assert listed["maxItems"] == MOST_TEXTS, operation
        assert listed["minItems"] == 1, operation
        assert _resolved(spec, listed["items"])["type"] == "string"


def test_a_forget_answers_two_counts():
    spec = _spec()
    answer = spec["paths"][f"{APPS_API}/forget"]["post"]["responses"]["200"]["content"]["application/json"]
    schema = _resolved(spec, answer["schema"])
    assert set(schema["required"]) == {"forgotten", "unknown"} == set(schema["properties"])
    assert set(answer["example"]) == {"forgotten", "unknown"}


def test_scan_and_mask_take_the_kinds_the_server_knows_and_unmask_none():
    spec = _spec()
    for operation in ("scan", "mask"):
        kind = _resolved(spec, _body(spec, operation)["properties"]["kind"])
        assert set(kind["enum"]) == set(KINDS), operation
        assert kind["default"] == "prose"
    assert "kind" not in _body(spec, "unmask")["properties"]
    assert "kind" not in _body(spec, "forget")["properties"]


def test_each_operation_lists_exactly_the_statuses_it_answers():
    spec = _spec()
    for operation, statuses in STATUSES.items():
        responses = spec["paths"][f"{APPS_API}/{operation}"]["post"]["responses"]
        assert set(responses) == statuses, operation
        for status, response in responses.items():
            assert _resolved(spec, response)["description"], (operation, status)
