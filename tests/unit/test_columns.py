"""Strict data mode, guided: the columns of a tool's output, and for each a proposal (keep it in clear,
keep it masked, or your call) with its reason."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from latet.columns import columns_of, propose, strict_values, technical_kind
from latet.config import Config, FieldRule
from latet.engine.build import build
from latet.engine.reviewer.client import ReviewError
from latet.engine.reviewer.columns import opinions
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

BENCH = Path(__file__).parents[1] / "data" / "llm_review" / "cases.yaml"
CASES = {c["id"]: c for c in yaml.safe_load(BENCH.read_text("utf-8"))["cases"]}
DB_CASES = [name for name in CASES if name.startswith("db-")]
KINDS = json.loads((Path(__file__).parents[1] / "data" / "technical_kinds.json").read_text("utf-8"))["cases"]
ORDERS = """\
 id  |   stato   | creato_il  |   cliente    |      email       | importo |          nota
-----+-----------+------------+--------------+------------------+---------+------------------------
 101 | SPEDITO   | 2026-09-01 | Mario Rossi  | mario@rossi.it   | 1250.50 | richiamare dopo le 18
 102 | IN_CORSO  | 2026-09-03 | Anna Bianchi | anna.b@gmail.com |   80.00 | cliente molto esigente
 103 | SPEDITO   | 2026-09-04 | Luca Verdi   | l.verdi@acme.it  |  430.00 | da sollecitare entro venerdì
 104 | ANNULLATO | 2026-09-05 | Sara Neri    | sara@neri.it     |   12.00 | rimborso già fatto
(4 rows)
"""


def _proposals(text: str, fields=()) -> dict[str, tuple[str, str]]:
    masker = build(Config(), Vault(ROOT_KEY)).masker
    return {c.name: (c.proposal, c.reason) for c in propose(columns_of(text), masker, fields)}


def test_the_columns_of_an_orders_table():
    assert _proposals(ORDERS) == {
        "id": ("keep", "ids"),
        "stato": ("keep", "states"),
        "creato_il": ("keep", "dates"),
        "cliente": ("mask", "person"),
        "email": ("mask", "people"),
        "importo": ("ask", "amounts"),
        "nota": ("mask", "free-text"),
    }


def test_a_column_says_what_strict_data_mode_masked_in_it():
    [state] = [c for c in columns_of(ORDERS, {"SPEDITO", "IN_CORSO"}) if c.name == "stato"]
    assert state.masked == 3 and state.examples == ["SPEDITO", "IN_CORSO", "ANNULLATO"]


def test_json_columns_are_named_by_their_path():
    text = json.dumps(
        {"items": [{"sku": f"A-{i}", "qty": i, "shipped_at": f"2026-01-0{i}T10:00:00Z"} for i in range(1, 5)]}
    )
    found = _proposals(text)
    assert found["items[].qty"] == ("keep", "counts") and found["items[].shipped_at"] == ("keep", "dates")
    assert set(found) == {"items[].sku", "items[].qty", "items[].shipped_at"}


def test_key_values_are_columns_too():
    text = "user=mario.rossi status=active retries=3\nuser=anna.bianchi status=paused retries=1\n"
    found = _proposals(text)
    assert found["user"] == ("mask", "person") and found["status"] == ("keep", "states")


def test_dates_of_birth_stay_masked():
    text = "id,data_nascita\n1,1984-03-02\n2,1990-11-30\n"
    assert _proposals(text)["data_nascita"] == ("mask", "birth-dates")


def test_yes_or_no_can_stay_in_clear():
    text = "id,attivo\n1,si\n2,no\n3,si\n"
    assert _proposals(text)["attivo"] == ("keep", "flags")


def test_people_in_a_human_resources_table():
    found = _proposals(CASES["db-psql-hr"]["text"])
    assert found["nome"] == ("mask", "person") and found["note_hr"] == ("mask", "free-text")
    assert found["ral"][0] == "ask"


def test_customers_and_their_contacts():
    found = _proposals(CASES["db-psql-clienti"]["text"])
    assert found["id"] == ("keep", "ids") and found["referente"] == ("mask", "person")
    assert found["ragione_sociale"][0] != "keep"


def test_secret_columns_stay_masked():
    found = _proposals(CASES["db-csv-utenti"]["text"])
    assert found["api_key"] == ("mask", "secret-name") and found["reset_token"] == ("mask", "secret-name")
    assert found["email"] == ("mask", "people")


def test_a_column_named_for_a_status_of_a_secret_is_masked_unless_every_value_has_a_status_shape():
    """`auth_status`, `token_state`...: the name says what a check found, so a column of lowercase words, numbers, times,
    dates and booleans is none of a secret's; one value of any other shape and it is masked by its name, as it was."""
    text = (
        "id,auth_status,token_state,password_changed\n"
        "1,failed,saml,2026-10-26\n"
        "2,ok,bearer-local,2026-10-27T12:00:01Z\n"
        "3,pending,Winter2024x,2026-10-28\n"
    )
    found = _proposals(text)
    assert found["auth_status"][1] != "secret-name" and found["password_changed"][1] != "secret-name"
    assert found["token_state"] == ("mask", "secret-name")  # one value of another shape


@pytest.mark.parametrize("case", DB_CASES)
def test_nothing_to_protect_is_ever_proposed_in_clear(case):
    """Every value the bench says must be reported stays out of the columns proposed in clear."""
    masker = build(Config(), Vault(ROOT_KEY)).masker
    protect = [value for value, _ in CASES[case]["expect"]]
    for column in propose(columns_of(CASES[case]["text"]), masker):
        if column.proposal == "keep":
            assert not [p for p in protect for v in column.values if p in v], (column.name, column.reason)


def test_a_column_kept_in_clear_today_says_so():
    masker = build(Config(), Vault(ROOT_KEY)).masker
    found = {c.name: c for c in propose(columns_of(ORDERS), masker, [FieldRule(name="Stato", mask=False)])}
    assert found["stato"].kept_now


def test_a_rule_of_the_project_comes_before_one_for_everywhere():
    """Your settings for a project come before the global rules: a column one of them matches is not changed by a rule
    for everywhere."""
    nearer = [FieldRule(name="stato", mask=True)]
    found = {c.name: c for c in propose(columns_of(ORDERS), build(Config(), Vault(ROOT_KEY)).masker, [], nearer)}
    assert not found["stato"].everywhere and found["id"].everywhere


def test_a_column_the_repository_file_masks_is_held_by_the_file_whatever_you_choose():
    """Re-review minor 1 (Ruling 098-R2): a column the project's latet.yaml masks stays masked there whatever you choose,
    This project or Everywhere, the stricter rule applying. It is said held by the file, not as a column to choose This
    project for, which changes nothing for it."""
    nearer, held = [FieldRule(name="stato")], [FieldRule(name="cliente")]
    found = {c.name: c for c in propose(columns_of(ORDERS), build(Config(), Vault(ROOT_KEY)).masker, [], nearer, held)}
    assert not found["stato"].everywhere and not found["stato"].held_by_file
    assert not found["cliente"].everywhere and found["cliente"].held_by_file
    assert found["id"].everywhere and not found["id"].held_by_file


def test_the_values_strict_data_mode_masked():
    spans = [{"start": 0, "end": 7, "module": "data"}, {"start": 8, "end": 13, "module": "personal"}]
    assert strict_values("SPEDITO mario", spans) == {"SPEDITO"}


@pytest.mark.parametrize("case", KINDS, ids=[c["field"] for c in KINDS])
def test_what_a_column_s_name_says(case):
    assert technical_kind(case["field"]) == case["kind"]


class FakeClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def ask(self, system: str, text: str, schema: dict) -> str:
        self.asked.append(text)
        return self.answer


def test_the_local_reviewer_gives_an_opinion_per_column():
    columns = columns_of(ORDERS)
    answer = {
        "columns": [
            {"name": "stato", "keep": True, "why": "order states"},
            {"name": "cliente", "keep": False, "why": "names"},
        ]
    }
    client = FakeClient(json.dumps(answer))
    found = asyncio.run(opinions(client, columns))
    assert found == {"stato": {"keep": True, "why": "order states"}, "cliente": {"keep": False, "why": "names"}}
    assert "Mario Rossi" in client.asked[0] and '"column": "stato"' in client.asked[0]


def test_an_opinion_that_cannot_be_read_is_said():
    with pytest.raises(ReviewError):
        asyncio.run(opinions(FakeClient("no idea"), columns_of(ORDERS)))


USERNAMES = ["mrossi", "abianchi", "mrossi", "abianchi", "mrossi", "abianchi"]  # they repeat, as states do
BIRTHDAYS = ["1984-03-02", "1990-11-30", "1975-01-15", "2001-07-07"]


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("created_by", USERNAMES),
        ("assigned_to", USERNAMES),
        ("reporter", USERNAMES),
        ("assignee", USERNAMES),
        ("medico", ["Rossi", "Bianchi", "Rossi", "Bianchi", "Rossi", "Bianchi"]),
        ("sesso", ["M", "F", "F", "M"]),
        ("compleanno", BIRTHDAYS),
        ("fecha_nacimiento", BIRTHDAYS),
        ("data_decesso", BIRTHDAYS),
        ("hiv_status", ["positive", "negative", "negative", "negative"]),
        ("marital_status", ["married", "single", "single", "married"]),
        ("is_pregnant", ["true", "false", "false", "true"]),
        ("tag", USERNAMES),  # values that repeat, and a name that says nothing of them
        ("quando", BIRTHDAYS),  # dates, and a name that does not say of what
    ],
)
def test_a_column_about_people_is_never_proposed_in_clear(column, values):
    """Only a column's name says it holds ids, dates of events, states or counts: the shape of its
    values alone (one word that repeats, a date, yes or no) never proposes it in clear."""
    text = f"id,{column}\n" + "".join(f"{i},{value}\n" for i, value in enumerate(values, 1))
    assert _proposals(text)[column][0] != "keep"
