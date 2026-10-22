"""The upgrade to the Community Edition drops nothing silently (the plan's R-K): the rules for everyone an earlier
version kept among the shared settings stay in the row, and no space built from then on applies them; an API key's space
drops them at once; and for a person the upgrade acts as an administrator removing every rule for everyone at once.
What protected her keeps protecting her until she decides (approving drops them, rejecting keeps them as her own, a save
of hers meanwhile decides nothing), a value let through for everyone is masked again at once, and two instances agree.

Each test builds the state an earlier version left under an edition that shares its rules (``sharing``), its spaces
built and their settings approved, then switches every server to the Community Edition, as the upgrade does."""

import asyncio

import pytest

from latet.config import Config, LiveReview, Profile, TermConfig
from latet.edition import Community
from latet.engine.types import SlotKind
from latet.runtime import digest

from .sharing import share_rules
from .support import listening, until

pytestmark = pytest.mark.server

ADMIN = "admin:a_first00000"
FALCON = TermConfig(value="Project Falcon", type="PROJECT")
BUDGET = "the Project Falcon budget"
CANARY = "billing@acme.com"  # a value the administrator let through for everyone


def _terms(runtime) -> list[str]:
    return [t.value for t in runtime.config.terms]


def _recognizers(prefix: str, count: int) -> list[dict]:
    return [
        {"name": f"{prefix}{i}", "type": "BADGE", "pattern": f"{prefix.upper()}{i:04d}-[0-9]{{6}}"}
        for i in range(count)
    ]


def _add_term(value):
    return lambda current: current.model_copy(update={"terms": [*current.terms, TermConfig(value=value)]})


async def _leaves(runtime, text: str) -> str:
    """``text`` as the space would send it in a tool's output."""
    engine = runtime.built.engine
    session = await asyncio.to_thread(engine.session, "cc:leaves")
    return (await asyncio.to_thread(engine.mask_text, text, SlotKind.TOOL_RESULT, session)).text


def _before(*servers, **rules) -> None:
    """What an earlier version left: every one of ``servers`` sharing the rules its shared settings store, and the
    administrator's ``rules`` for everyone stored there."""
    share_rules(*servers)
    servers[0].shared.set(Config(**rules), actor=ADMIN)


async def _upgraded(*servers) -> None:
    """Every one of ``servers`` runs the Community Edition from now on, as after the upgrade: the rules for everyone the
    row stores stay there, in force for nobody. Each space open on them reads its settings again, as at the
    announcement of a change of the shared settings; a space built from now on reads them so anyway."""
    for server in servers:
        server.edition = Community()
        server.shared.rules = server.edition.shared_rules
        server.shared.forget()
    _, counter = await asyncio.to_thread(servers[0].shared.counted)
    await asyncio.to_thread(servers[0].shared.announce, counter)


def _as_the_administration(runtime, change) -> None:
    """What the administration's save of an API key's settings does: applied through its runtime and approved, under
    the runtime's lock."""
    with runtime._lock:
        runtime.update_config(change)
        runtime.settings.save_reviewed(runtime.config)


async def _waiting(runtime):
    """The change that waits for her over the settings in use: the one her approval applies."""
    await until(
        lambda: any(c.base == digest(runtime.config) for c in runtime.mediator.approvals.waiting_changes()),
        seconds=30,
    )
    [change] = [c for c in runtime.mediator.approvals.waiting_changes() if c.base == digest(runtime.config)]
    return change


async def test_a_person_whose_space_is_first_built_after_the_upgrade_masks_none_of_the_rules_kept(server):
    """Daniel had no space before the upgrade, so no settings of his were approved: his space, built afterwards, applies
    none of the rules the row keeps (the shared term is not masked, the profile is his own), and nothing waits for him.
    The row keeps them all the same."""
    _before(server, terms=[FALCON], profile=Profile.MAX)
    daniel, _ = server.identities.add_person("Daniel Okafor")
    async with listening(server):
        await _upgraded(server)
        his = await server.spaces.runtime(daniel)
        assert "Project Falcon" not in _terms(his) and his.config.profile == Profile.BALANCED
        assert "Project Falcon" in await _leaves(his, BUDGET)
        assert not list(his.mediator.approvals.changes)
        approved = await asyncio.to_thread(his.reviewed)
        assert approved is not None and approved.terms == [] and approved.profile == Profile.BALANCED
    assert set(await asyncio.to_thread(server.shared.kept)) == {"terms", "profile"}  # kept in the row, by their keys


async def test_an_api_keys_space_stops_applying_the_rules_kept_at_once(server):
    """An API key's settings are its own part laid over the shared settings of now, approved by the administration: the
    upgrade removes the rules for everyone from under it at once, nothing waits for anyone, and its own rules stay."""
    _before(server, terms=[FALCON])
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(_as_the_administration, runtime, _add_term("Mercury Labs"))
        assert {"Project Falcon", "Mercury Labs"} <= set(_terms(runtime))
        await _upgraded(server)
        await until(lambda: "Project Falcon" not in _terms(runtime))
        assert _terms(runtime) == ["Mercury Labs"] and not list(runtime.mediator.approvals.changes)
        assert "Project Falcon" in await _leaves(runtime, BUDGET)
        assert await asyncio.to_thread(runtime.reviewed) == await asyncio.to_thread(runtime.settings.load)
        await server.spaces.drop(key.id)
        again = await server.spaces.runtime(key)  # built after the upgrade, as at an instance's restart
        assert _terms(again) == ["Mercury Labs"] and not list(again.mediator.approvals.changes)


async def test_a_person_who_approved_a_shared_term_keeps_masking_it_after_the_upgrade_while_its_removal_waits(server):
    """Emily's approved settings hold the shared term: after the upgrade her space keeps masking it, the term in force
    but not in her own part (she decided nothing), and its removal waits for her, in a space built again too."""
    _before(server, terms=[FALCON])
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)  # her approved settings: the shared term with the rest
        approved = await asyncio.to_thread(runtime.reviewed)
        assert approved is not None and "Project Falcon" in [t.value for t in approved.terms]
        await _upgraded(server)
        change = await _waiting(runtime)
        assert any(reason.startswith("terms:") for reason in change.loosened)
        assert "Project Falcon" not in [t.value for t in change.config.terms]  # what her approval would apply
        assert "Project Falcon" in _terms(runtime)
        assert "Project Falcon" not in await _leaves(runtime, BUDGET)
        own = await asyncio.to_thread(runtime.settings._own)
        assert "terms" not in own
        await server.spaces.drop(emily.id)
        again = await server.spaces.runtime(emily)  # built after the upgrade, as at an instance's restart
        assert "Project Falcon" in _terms(again)
        assert [c.id for c in again.mediator.approvals.waiting_changes()] == [change.id]


async def test_her_approval_of_the_removal_drops_the_rules_kept_for_good(server):
    _before(server, terms=[FALCON])
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await _upgraded(server)
        change = await _waiting(runtime)
        assert await asyncio.to_thread(runtime.decide_change, change.id, True) == "approved"
        assert "Project Falcon" not in _terms(runtime)
        assert "Project Falcon" in await _leaves(runtime, BUDGET)
        approved = await asyncio.to_thread(runtime.reviewed)
        assert approved is not None and "Project Falcon" not in [t.value for t in approved.terms]
        await server.spaces.drop(emily.id)
        again = await server.spaces.runtime(emily)
        assert "Project Falcon" not in _terms(again) and not list(again.mediator.approvals.changes)


async def test_her_rejection_of_the_removal_keeps_the_rules_kept_as_her_own(server):
    _before(server, terms=[FALCON])
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await _upgraded(server)
        change = await _waiting(runtime)
        assert await asyncio.to_thread(runtime.decide_change, change.id, False) == "rejected"
        own = await asyncio.to_thread(runtime.settings._own)
        assert [t["value"] for t in own.get("terms", [])] == ["Project Falcon"]  # hers now
        assert "Project Falcon" in _terms(runtime) and not list(runtime.mediator.approvals.changes)
        assert "Project Falcon" not in await _leaves(runtime, BUDGET)
        await server.spaces.drop(emily.id)
        again = await server.spaces.runtime(emily)
        assert "Project Falcon" in _terms(again) and not list(again.mediator.approvals.changes)


async def test_a_value_let_through_for_everyone_before_the_upgrade_is_masked_again_at_once(server):
    """A value the administrator let through for everyone stops being let through at once: masking it again is
    stricter, so nothing waits for her decision."""
    _before(server, allow_values=[CANARY])
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        assert CANARY in runtime.config.allow_values
        assert CANARY in await _leaves(runtime, f"write to {CANARY}")  # let through, before
        await _upgraded(server)
        await until(lambda: CANARY not in runtime.config.allow_values)
        assert CANARY not in await _leaves(runtime, f"write to {CANARY}")
        assert not list(runtime.mediator.approvals.changes)
        approved = await asyncio.to_thread(runtime.reviewed)
        assert approved is not None and CANARY not in approved.allow_values
        await server.spaces.drop(emily.id)
        again = await server.spaces.runtime(emily)
        assert CANARY not in again.config.allow_values


async def test_her_save_while_the_removal_waits_leaves_the_kept_term_hers_to_decide(server):
    """A save of hers made over the composition (the formerly shared term kept while its removal waits) is laid back on
    her own settings: the term does not become her own part, it stays in force, and the removal is asked again over the
    settings in use, hers to approve."""
    _before(server, terms=[FALCON])
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await _upgraded(server)
        await _waiting(runtime)
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))  # an unrelated save of hers
        own = await asyncio.to_thread(runtime.settings._own)
        assert [t["value"] for t in own.get("terms", [])] == ["Mercury Labs"]  # not the term the composition kept
        assert {"Project Falcon", "Mercury Labs"} <= set(_terms(runtime))  # in force: never looser meanwhile
        change = await _waiting(runtime)  # asked again over what is in use now
        assert "Project Falcon" not in [t.value for t in change.config.terms]
        assert "Mercury Labs" in [t.value for t in change.config.terms]


async def test_her_unrelated_save_while_the_removal_waits_counts_only_her_own_recognizers(server):
    """Three hundred recognizers set for everyone before the upgrade stay in force for her while their removal waits;
    they are not hers. A save of hers that changes something else is counted on her own three hundred, under what a
    server keeps for one identity (500), and applies."""
    _before(server, recognizers=_recognizers("zq", 300))
    emily, _ = server.identities.add_person("Emily Carter")
    mine = _recognizers("zm", 300)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await asyncio.to_thread(
            runtime.update_config, lambda c: c.model_copy(update={"recognizers": [*c.recognizers, *mine]})
        )
        await _upgraded(server)
        await _waiting(runtime)
        await asyncio.to_thread(runtime.update_config, lambda c: c.model_copy(update={"profile": Profile.MAX}))
        assert runtime.config.profile == Profile.MAX and len(runtime.config.recognizers) == 600


async def test_a_profile_and_the_live_reviewer_kept_from_before_hold_until_she_decides(server):
    """The max profile and the Live Reviewer the administrator set for everyone are rules for everyone too: after the
    upgrade both keep applying to her (private addresses masked, every new text waiting for her) until she approves
    their removal, which then applies."""
    _before(server, profile=Profile.MAX, live_review=LiveReview(enabled=True))
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        assert runtime.config.profile == Profile.MAX and runtime.config.live_review.enabled
        await _upgraded(server)
        change = await _waiting(runtime)
        assert runtime.config.profile == Profile.MAX and runtime.config.live_review.enabled
        assert "10.1.2.3" not in await _leaves(runtime, "the database is at 10.1.2.3")
        assert await asyncio.to_thread(runtime.decide_change, change.id, True) == "approved"
        assert runtime.config.profile == Profile.BALANCED and not runtime.config.live_review.enabled
        assert "10.1.2.4" in await _leaves(runtime, "the database is at 10.1.2.4")  # a new address: none of hers


async def test_two_instances_apply_no_rule_kept_to_a_new_person_and_agree_on_what_waits_for_one_who_approved_them(
    instances,
):
    """Emily's space is open on both instances when they are upgraded: both keep the formerly shared term in force for
    her and show the one change that waits for her. Daniel's space, first built after the upgrade, applies none of the
    rules kept on either instance."""
    a, b = instances
    _before(a, b, terms=[FALCON])
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        assert "Project Falcon" in _terms(here) and "Project Falcon" in _terms(there)  # shared, before
        await _upgraded(a, b)

        def waiting(runtime) -> list[str]:
            return [c.id for c in runtime.mediator.approvals.waiting_changes()]

        await until(lambda: len(waiting(here)) == 1 and waiting(here) == waiting(there), seconds=30)
        await asyncio.sleep(0.3)  # what either instance would still open
        assert len(waiting(here)) == 1 and waiting(here) == waiting(there)
        for runtime in (here, there):
            assert "Project Falcon" in _terms(runtime)
            assert "Project Falcon" not in await _leaves(runtime, BUDGET)
        assert here.config == there.config
        for server in (a, b):
            his = await server.spaces.runtime(daniel)
            assert "Project Falcon" not in _terms(his) and not list(his.mediator.approvals.changes)
            assert "Project Falcon" in await _leaves(his, BUDGET)
