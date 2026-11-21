"""Request exceptions (Latet 0.11) under a company's policy (Latet Enterprise): one knob of the policy decides who
decides them. With `exceptions: personal` (the default: a policy that sets only a profile too) a person's are hers,
approved by her as in the Community Edition, and an API key's are decided as its other changes are. With
`exceptions: company` they are the company's: whoever asks proposes, the security team decides through central
approvals, and no other approval grants one. Under any policy one not yet approved grants nothing and is never weighed
as a loosening below the floor: what the floor judges is the approval."""

import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from latet.config import Config, IdentityConfig, RequestException
from latet.ee.approvals import CentralApprovals, MemoryRecords
from latet.ee.policy import parse_policy
from latet.ee.roles import Role
from latet.ee.settings import ApprovalsSettings
from latet.floor import BelowFloor, fresh_reasons
from latet.gateway.request_profiles import REVISION
from latet.protection import exception_loosened, exception_view
from latet.runtime import make_runtime
from latet.server.spaces import _exception_logged

from ...conftest import ROOT_KEY
from ...floors import FloorEdition
from .stand_ins import FakeDirectory, Served, Spaces

run = asyncio.to_thread
ROW = {
    "destination": "anthropic",
    "profile": "anthropic.messages.1",
    "components": [{"kind": "header", "name": "x-team"}],
    "reason": "custom-header",
}
POLICIES = {
    "personal": "exceptions: personal\n",
    "a-profile-alone": "profile: balanced\n",
    "company": "exceptions: company\n",
}
PERSONAL = ("personal", "a-profile-alone")


def _floor(policy: str):
    return parse_policy(f"latet_rules: 1\n{POLICIES[policy]}")


def _with_row(config: Config) -> Config:
    return config.model_copy(update={"request_exceptions": [RequestException.model_validate(ROW)]})


class Floored(Served):
    """The edition of a server's spaces with central approvals behind its hooks and the company's policy."""

    def __init__(self, queue, floor) -> None:
        super().__init__(queue)
        self.locked = floor

    def floor(self, identity):
        return self.locked


class Queues:
    def batches(self, items):
        return {i for _, i in items}

    def changes(self, items):
        return {}


@pytest.fixture
def people():
    """Emily asks; Mark decides for the security team; Daniel controls the Billing CI key with her; Paul is an admin."""
    directory = FakeDirectory()
    emily = directory.add("Emily Carter")
    mark = directory.add("Mark Ellis", roles=(Role.SECURITY,))
    daniel = directory.add("Daniel Hayes")
    directory.add("Paul Ferris", roles=(Role.ADMIN,))
    key = directory.add("Billing CI", kind="key")
    directory.controlling[key.id] = [emily.id, daniel.id]
    return directory, emily, mark, daniel, key


@pytest.fixture
def central(people):
    """Each decides on theirs (the default): a person's changes are hers, an API key's go to its controllers."""
    return CentralApprovals(
        MemoryRecords(), people[0], settings=lambda: ApprovalsSettings(), licensed=lambda: True, space=Spaces(),
        waiting_batches=Queues().batches, ended_changes=Queues().changes,
    )  # fmt: skip


@pytest.fixture
def space(tmp_path, central, monkeypatch):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    made = []

    def make(identity, policy, edition=None, config=None):
        config = config if config is not None else Config(identity=IdentityConfig(enabled=False), ner="off")
        folder = tmp_path / identity.id
        edition = edition if edition is not None else Floored(central, _floor(policy))
        rt = make_runtime(config, data=folder, config_file=folder / "config.yaml", identity=identity, edition=edition)
        central.space.runtimes[identity.id] = rt
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close()


async def _decide(central, who, item_id, approve=True):
    """As central approvals' route does: admitted, then carried out in the asking identity's space."""
    row, capacity = central.admitted(None, who.id, item_id, "change", approve)
    if row is None:
        return capacity
    return await central.carry_change(row, approve, None if capacity == "own" else who, run=run)


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_an_exception_not_yet_approved_is_never_below_the_floor(policy):
    floor, before = _floor(policy), Config(ner="off")
    after = _with_row(before)
    assert floor.below(after) == floor.below(before)
    assert fresh_reasons(floor, before, after) == []


@pytest.mark.parametrize("policy", PERSONAL)
def test_with_personal_exceptions_a_person_approves_her_own(space, people, policy):
    _, emily, *_ = people
    rt = space(emily, policy)
    rt.check_floor(_with_row(rt.config))  # proposed: nothing below the floor
    change = rt.propose(_with_row(rt.config), "command")
    assert change is not None and change.decides == "self"
    assert rt.decide_change(change.id, True) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved and row.issuer == "person"


async def test_with_company_exceptions_a_person_proposes_and_the_security_team_decides(space, people, central):
    _, emily, mark, *_ = people
    rt = space(emily, "company")
    rt.check_floor(_with_row(rt.config))
    change = rt.propose(_with_row(rt.config), "command")
    assert change is not None and change.decides == "security"  # hers by the settings: the company's by the policy
    assert change.deciders == ["Mark Ellis"]
    assert rt.decide_change(change.id, True) == "not-yours"
    assert await _decide(central, emily, change.id) == "yours"
    assert rt.config.request_exceptions == []
    assert await _decide(central, mark, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved


def test_with_company_exceptions_a_persons_own_approval_grants_none(space, people):
    """Where nobody else decides (no central approvals), her approval is refused below the floor: rejected, nothing
    stamped."""
    _, emily, *_ = people
    rt = space(emily, "company", edition=FloorEdition(_floor("company")))
    change = rt.propose(_with_row(rt.config), "command")
    assert change is not None and change.decides == "self"
    with pytest.raises(BelowFloor) as refused:
        rt.decide_change(change.id, True)
    assert refused.value.reasons == ["a request exception for anthropic: your company's security team decides on it"]
    assert rt.config.request_exceptions == [] and rt.mediator.approvals.changes.get(change.id) is None


def test_the_floor_judges_the_approval_when_it_comes(space, people):
    """Proposed under personal exceptions, approved after the policy says company: refused then."""
    _, emily, *_ = people
    edition = FloorEdition(_floor("personal"))
    rt = space(emily, "personal", edition=edition)
    change = rt.propose(_with_row(rt.config), "command")
    assert change is not None
    edition.locked = _floor("company")
    with pytest.raises(BelowFloor):
        rt.decide_change(change.id, True)
    assert rt.config.request_exceptions == []


@pytest.mark.parametrize("policy", PERSONAL)
async def test_with_personal_exceptions_a_keys_controllers_decide_on_its_exception(space, people, central, policy):
    _, emily, mark, daniel, key = people
    rt = space(key, policy)
    rt.check_floor(_with_row(rt.config))
    change = rt.propose(_with_row(rt.config), "command", asked_by=emily.id)
    assert change is not None and change.decides == "controllers" and change.deciders == ["Daniel Hayes"]
    assert await _decide(central, mark, change.id) == "not-yours"
    assert await _decide(central, daniel, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved


async def test_with_company_exceptions_a_keys_exception_goes_to_the_security_team(space, people, central):
    _, emily, mark, daniel, key = people
    rt = space(key, "company")
    change = rt.propose(_with_row(rt.config), "command", asked_by=emily.id)
    assert change is not None and change.decides == "security" and change.deciders == ["Mark Ellis"]
    assert await _decide(central, daniel, change.id) == "not-yours"
    assert rt.config.request_exceptions == []
    assert await _decide(central, mark, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved


def test_with_company_exceptions_the_administration_still_approves_a_keys_settings(space, people):
    """An API key's settings are the administration's (0.11's D3): its save of them, the company's own, approves the
    exception it adds whatever the policy says."""
    *_, key = people
    rt = space(key, "company")
    rt.update_config(_with_row, approving=True)
    [row] = rt.config.request_exceptions
    assert row.approved and row.issuer == "administration"
    assert row.approver is None and row.approver_id is None


# --- what grants once the company decides (the quality re-review's N1) ------------------------------------------------


async def _sent(rt) -> int:
    """A Claude message through the space's gateway with the header ``ROW`` names (``x-team``): what answers it."""
    import json

    import httpx

    from latet.gateway.app import create_app

    def provider(request: httpx.Request) -> httpx.Response:
        body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}  # fmt: skip
        answer = httpx.ByteStream(json.dumps(body).encode())
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
    app = create_app(rt.gateway)
    call = {"model": "claude-sonnet-4-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]}
    headers = {"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01", "x-team": "data-science"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
    ):
        return (await client.post("/anthropic/v1/messages", json=call, headers=headers)).status_code


def _permanent(config: Config) -> Config:
    row = RequestException.model_validate({**ROW, "permanent": True})
    return config.model_copy(update={"request_exceptions": [row]})


@pytest.mark.parametrize("who", ["person", "key"])
async def test_an_exception_approved_under_personal_grants_nothing_once_the_company_decides(
    space, people, central, who
):
    """N1: an exception approved while each person decided (by the person, or by another of an API key's controllers),
    permanent, keeps no authority once the policy says `exceptions: company`: from the moment the space applies it,
    the request it let through is refused again, and the space's list says it is not in force. Back to `personal`, it
    counts again."""
    from latet.gateway import request_policy

    _, emily, _, daniel, key = people
    identity = emily if who == "person" else key
    edition = Floored(central, _floor("personal"))
    rt = space(identity, "personal", edition=edition)
    change = rt.propose(_permanent(rt.config), "command", asked_by=emily.id if who == "key" else None)
    assert change is not None
    if who == "person":
        assert rt.decide_change(change.id, True) == "approved"
    else:
        assert await _decide(central, daniel, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved and row.permanent and row.issuer == ("person" if who == "person" else "controllers")
    assert rt.gateway.request_exceptions == (row,) and await _sent(rt) == 200
    edition.locked = _floor("company")
    rt.floor_changed()  # as the server's spaces apply a new policy
    assert rt.gateway.request_exceptions == () and await _sent(rt) == 400
    [state] = request_policy.listed(rt.config, rt.exception_clock(), company=True)
    assert state["state"] == "not approved by the company"
    edition.locked = _floor("personal")
    rt.floor_changed()
    assert rt.gateway.request_exceptions == (row,) and await _sent(rt) == 200


async def test_an_api_keys_exception_approved_in_its_space_without_central_approvals_grants_nothing_under_company(
    space, people
):
    """Fix round 1 of the release minors: with no central approvals in the license (no record: ``central()`` None), a
    change of an API key's waits for "self", and a person working in the key's space approves it there (the dashboard
    passes no decider). That approval is a person's, as a controller's, never the administration's: stamped
    ``controllers``, so that once the policy says `exceptions: company` it grants nothing, as N1 has every person's and
    controller's approval do. The administration's save of the key's settings (``approving``) still stamps
    ``administration``, and still grants under company."""
    *_, key = people
    edition = FloorEdition(_floor("personal"))
    rt = space(key, "personal", edition=edition)
    change = rt.propose(_permanent(rt.config), "command")
    assert change is not None and change.decides == "self"
    assert rt.decide_change(change.id, True) == "approved"  # as the key's space's dashboard decides it
    [row] = rt.config.request_exceptions
    assert (row.issuer, row.approver, row.approver_id) == ("controllers", None, None)
    assert rt.gateway.request_exceptions == (row,) and await _sent(rt) == 200
    edition.locked = _floor("company")
    rt.floor_changed()
    assert rt.gateway.request_exceptions == () and await _sent(rt) == 400
    saved = FloorEdition(_floor("personal"))
    keys = space(SimpleNamespace(id="u_billingci3", name="Nightly CI", kind="key"), "personal", edition=saved)
    keys.update_config(_permanent, approving=True)  # the administration token's save: no decider
    [row] = keys.config.request_exceptions
    assert row.issuer == "administration"
    saved.locked = _floor("company")
    keys.floor_changed()
    assert keys.gateway.request_exceptions == (row,) and await _sent(keys) == 200


async def test_once_the_company_decides_the_exceptions_it_approved_still_grant(space, people, central):
    """N1's other half: under `exceptions: company`, an exception the security team approved through central
    approvals, and one the administration's save of an API key's settings approved, keep granting."""
    _, emily, mark, _, key = people
    rt = space(emily, "company")
    change = rt.propose(_permanent(rt.config), "command")
    assert change is not None and await _decide(central, mark, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.issuer == "security" and rt.gateway.request_exceptions == (row,) and await _sent(rt) == 200
    keys = space(key, "company")
    keys.update_config(_permanent, approving=True)
    [row] = keys.config.request_exceptions
    assert row.issuer == "administration" and keys.gateway.request_exceptions == (row,) and await _sent(keys) == 200


# --- who approved it, said by the exception (the merge review's I2) -------------------------------------------------


@pytest.mark.parametrize("policy", PERSONAL)
async def test_an_exception_a_keys_controller_approves_names_them_and_is_logged_as_theirs(
    space, people, central, policy
):
    """The security review's path: an API key's settings that carry Latet's stamps with no approval behind them (a
    first start) wait as new exceptions; another of the key's controllers approves them. The exception says who, and
    the administration log's entry for a permanent one has them as its actor."""
    _, emily, _, daniel, key = people
    stamped = RequestException.model_validate(
        {**ROW, "id": "x0123456789", "granted": datetime(2026, 11, 1, tzinfo=UTC), "revision": REVISION,
         "issuer": "administration", "permanent": True}
    )  # fmt: skip
    config = Config(identity=IdentityConfig(enabled=False), ner="off", request_exceptions=[stamped])
    rt = space(key, policy, config=config)
    logged = []
    rt.exception_log = logged.append
    [change] = rt.mediator.approvals.waiting_changes()
    assert change.decides == "controllers" and change.exceptions
    assert await _decide(central, daniel, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert row.approved and row.id != "x0123456789"
    assert (row.issuer, row.approver, row.approver_id) == ("controllers", "Daniel Hayes", daniel.id)
    assert logged == [{"exception": row.id, "scope": "identity", "by": daniel.id}]
    assert emily.id not in str(logged)


async def test_an_exception_the_security_team_approves_names_who_approved_it(space, people, central):
    _, emily, mark, *_ = people
    rt = space(emily, "company")
    logged = []
    rt.exception_log = logged.append
    permanent = _with_row(rt.config)
    permanent = permanent.model_copy(
        update={"request_exceptions": [permanent.request_exceptions[0].model_copy(update={"permanent": True})]}
    )
    change = rt.propose(permanent, "command")
    assert change is not None
    assert await _decide(central, mark, change.id) == "approved"
    [row] = rt.config.request_exceptions
    assert (row.issuer, row.approver, row.approver_id) == ("security", "Mark Ellis", mark.id)
    assert logged == [{"exception": row.id, "scope": "identity", "by": mark.id}]
    assert exception_view(row)["approver"] == "Mark Ellis"


async def test_a_keys_controller_who_approves_check_for_new_request_members_is_logged_as_its_actor(
    space, people, central
):
    """Design §3 on a server: an approval that moves `unknown_members` from `refuse` to `check` is written to the
    administration log, with the decider of the company's (here another of the key's controllers) as its actor."""
    _, emily, _, daniel, key = people
    strict = Config(identity=IdentityConfig(enabled=False), ner="off", unknown_members="refuse")
    rt = space(key, "personal", config=strict)
    logged = []
    rt.setting_log = logged.append
    change = rt.propose(rt.config.model_copy(update={"unknown_members": "check"}), "command")
    assert change is not None and change.decides == "controllers"
    assert await _decide(central, daniel, change.id) == "approved"
    assert rt.config.unknown_members == "check"
    assert logged == [{"setting": "unknown_members", "value": "check", "scope": "identity", "by": daniel.id}]
    assert emily.id not in str(logged)


def test_her_own_approval_names_nobody_else(space, people):
    _, emily, *_ = people
    rt = space(emily, "personal")
    logged = []
    rt.exception_log = logged.append
    change = rt.propose(_with_row(rt.config), "command")
    assert change is not None and rt.decide_change(change.id, True) == "approved"
    [row] = rt.config.request_exceptions
    assert (row.issuer, row.approver, row.approver_id) == ("person", None, None)


def test_an_approver_written_by_hand_is_no_approval():
    """Who approved it is Latet's stamp: an approved exception whose approver changed is new, and waits again."""
    row = RequestException.model_validate(
        {**ROW, "id": "x0123456789", "granted": datetime(2026, 11, 1, tzinfo=UTC), "revision": REVISION,
         "issuer": "security", "approver": "Mark Ellis", "approver_id": "u_abcdefghij"}
    )  # fmt: skip
    forged = row.model_copy(update={"approver": "Somebody Else"})
    assert exception_loosened(row, forged) == "added"
    sooner = forged.model_copy(update={"granted": datetime(2026, 10, 30, tzinfo=UTC)})  # an earlier end is no cover
    assert exception_loosened(row, sooner) == "added"
    assert exception_loosened(row, row.model_copy(update={"granted": datetime(2026, 10, 30, tzinfo=UTC)})) is None
    with pytest.raises(ValueError, match="who approved an exception is Latet's"):
        RequestException.model_validate({**ROW, "approver": "Mark Ellis"})  # never on an exception not approved


def test_the_administration_log_has_the_decider_as_the_actor():
    recorded = []
    server = SimpleNamespace(audit=SimpleNamespace(record=lambda *entry: recorded.append(entry)))
    _exception_logged(server, "u_emilycarte", {"exception": "x0123456789", "scope": "identity", "by": "u_markelliss"})
    _exception_logged(server, "u_emilycarte", {"exception": "x0123456788", "scope": "project"})
    assert recorded == [
        ("u_markelliss", "exception.permanent", "u_emilycarte", {"exception": "x0123456789", "scope": "identity"}),
        ("u_emilycarte", "exception.permanent", "u_emilycarte", {"exception": "x0123456788", "scope": "project"}),
    ]
