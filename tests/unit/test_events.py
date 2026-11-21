"""An event of what happens in Latet: counts, categories, codes and ids, never a value or a text. make_event checks
the shape of each field, so a text cannot go where a code or an id goes."""

import pytest

from latet.events import EVENT_TYPES, make_event

FIELDS = {"v", "id", "ts", "type", "identity", "kind", "group", "agent", "project", "masked", "stopped", "reason"}
FIELDS |= {"outcome", "by", "rule", "chain"}


def test_an_event_has_the_schemas_fields_and_its_time_in_utc():
    event = make_event(
        "exchange", clock=lambda: 1793697164.0, identity="u_7f3a2b9c1d", kind="person", group="g_payments",
        agent="claude-code", project="billing-api", masked={"EMAIL": 3, "IBAN": 1},
    )  # fmt: skip
    assert set(event) == FIELDS
    assert (event["v"], event["type"], event["ts"]) == (1, "exchange", "2026-11-03T09:12:44Z")
    assert event["masked"] == {"EMAIL": 3, "IBAN": 1} and event["stopped"] == 0 and event["by"] is None
    assert len(event["id"]) == 32 and make_event("license")["id"] != event["id"]


def test_the_types_are_the_schemas():
    assert EVENT_TYPES == (
        "exchange", "stopped", "approval", "change", "policy", "sync", "sign-in", "license", "admin", "apps",
    )  # fmt: skip


@pytest.mark.parametrize("kind", ["exchanges", "", "License"])
def test_an_unknown_type_is_refused(kind):
    with pytest.raises(ValueError, match="unknown event type"):
        make_event(kind)


def test_a_field_the_schema_does_not_have_is_refused():
    with pytest.raises(ValueError, match="no field text"):
        make_event("stopped", text="Emily Carter's IBAN")


@pytest.mark.parametrize("reason", ["sent Emily Carter's IBAN", "IBAN GB82WEST12345698765432", "a" * 81, "Blocked"])
def test_a_reason_is_a_code_never_a_text(reason):
    with pytest.raises(ValueError, match="reason is a code"):
        make_event("stopped", reason=reason)


@pytest.mark.parametrize("masked", [{"emily.carter@acme.com": 1}, {"EMAIL": -1}, {"EMAIL": "3"}, {"EMAIL": True}])
def test_masked_counts_values_by_type(masked):
    with pytest.raises(ValueError, match="counts values by type"):
        make_event("exchange", masked=masked)


@pytest.mark.parametrize("masked", [5, "EMAIL", ["EMAIL"], [("EMAIL", 1)]])
def test_masked_is_a_mapping(masked):
    with pytest.raises(ValueError, match="an event's masked counts values by type"):
        make_event("exchange", masked=masked)


@pytest.mark.parametrize(
    "project",
    [
        "src/billing-api",
        "C:/Users/emily/billing-api",
        "billing\N{REVERSE SOLIDUS}api",
        "billing" + chr(10) + "api",
        "billing" + chr(0),
        chr(0x1F) + "billing",
        "billing" + chr(0x7F),
        "",
        "b" * 201,
    ],
)
def test_a_project_is_a_name_never_a_path(project):
    with pytest.raises(ValueError, match="an event's project"):
        make_event("exchange", project=project)


def test_a_projects_name_may_hold_spaces_and_letters_of_any_language():
    name = "Billing API (Zahlungsverkehr " + chr(0xFC) + "berall)"
    assert make_event("exchange", project=name)["project"] == name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identity", "Emily Carter"),
        ("by", "the security team"),
        ("group", "payments"),
        ("kind", "machine"),
        ("stopped", -1),
    ],
)
def test_ids_kinds_and_counts_have_their_shapes(field, value):
    with pytest.raises(ValueError, match=f"an event's {field}"):
        make_event("stopped", **{field: value})
