"""Who decides on what waits in an identity's space on a server (E5), over its settings kept as ``PgSettings`` keeps
them (in memory here): settings that arrive stay the person's only when the shared settings alone lowered them, a change
asked again keeps the route it opened with, and an API key's destinations never save what waits as its own part."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from latet.edition import Community
from latet.gateway.approvals import Waits
from latet.runtime import make_runtime
from latet.server import spaces
from latet.state import IdentityRef, local_backends

from ...central import Company, Queue
from ...conftest import ROOT_KEY
from .conftest import EMILY, SHARED, MemorySettings

ADDED = "https://llm2.acme.com/v1"


def _space(tmp_path, edition, identity=EMILY):
    """``identity``'s space over the shared settings and its own part in memory, running ``edition``."""
    settings = MemorySettings(SHARED)
    settings.kind = identity.kind
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    runtime = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=identity, edition=edition
    )
    return runtime, settings


def _with_acme(config):
    return config.model_copy(update={"allow_values": [*config.allow_values, "ACME"]})


def test_a_change_for_the_security_team_asked_again_by_a_computer_s_destinations_stays_theirs(tmp_path):
    """The review of Task 2, I1: a change of source file that waits for the security team, no composition held, is asked
    again with a computer's destinations. Those are saved as approved settings first, which leaves her own part kept
    equal to the one approved: [D1]'s exemption would make the change hers. Asked again, it keeps its route, and she
    can withdraw it, never approve it."""
    company = Company(Queue(["Daniel Hayes", "Mark Ellis"]))
    runtime, settings = _space(tmp_path, company)
    try:
        settings.own = {"allow_values": ["ACME"]}  # written outside any approval: the part approved stays {}
        change = runtime.propose(_with_acme(runtime.config), "file")
        assert change is not None and change.decides == "security" and runtime._held() is None
        assert runtime.add_destinations([(ADDED, "claude")])
        [again] = runtime.mediator.approvals.waiting_changes()
        assert again.id != change.id and (again.decides, again.deciders) == ("security", change.deciders)
        assert "ACME" in again.config.allow_values and any(d.url == ADDED for d in again.config.upstreams)
        assert company.queue.opens == [change, again]
        assert (change.id, "change", "superseded", None) in company.queue.ends
        assert runtime.decide_change(again.id, True) == "not-yours" and "ACME" not in runtime.config.allow_values
    finally:
        runtime.close()


def test_settings_that_arrive_lowered_by_the_shared_settings_alone_stay_the_person_s(tmp_path):
    """[D1] (the review of Task 2, m4): a change of source file that only the shared settings lowered (a shared term
    removed), her own part kept as she approved it, stays hers to decide: nothing reaches the company's record."""
    company = Company(Queue())
    runtime, settings = _space(tmp_path, company)
    try:
        settings.own = {"terms": [{"value": "Vega"}]}
        settings.approved_part = dict(settings.own)  # approved as it is kept
        settings._shared = lambda: SHARED.model_copy(update={"terms": []})  # Project Falcon removed for everyone
        found = settings.load()  # her settings as her space reads them again (``spaces._reapply``)
        change = runtime.propose(found, "file")
        assert change is not None and change.decides == "self" and change.deciders == []
        assert company.queue.opens == [] and runtime.decide_change(change.id, True) == "approved"
    finally:
        runtime.close()


def test_settings_that_arrive_are_weighed_as_the_change_holds_them_never_as_read_again(tmp_path):
    """The whole-branch security review's L1: [D1] weighs the own part the change was made from, laid over the shared
    settings it was made over, never the own part read again as the change opens. Here another instance wrote her own
    part back to the one she approved meanwhile: the change still lets a value through by her own part, and goes to the
    security team, which she cannot approve for."""
    company = Company(Queue(["Mark Ellis"]))
    runtime, settings = _space(tmp_path, company)
    try:
        settings.approved_part = {}  # what she approved, and what her own part kept holds again now
        change = runtime.propose(_with_acme(runtime.config), "file")  # made while her own part let ACME through
        assert change is not None and (change.decides, change.deciders) == ("security", ["Mark Ellis"])
        assert company.queue.opens == [change] and runtime.decide_change(change.id, True) == "not-yours"
    finally:
        runtime.close()


def test_her_save_that_waits_for_the_security_team_is_never_asked_again_as_a_composition_s_content(tmp_path):
    """The re-review of Task 2, n1: while a composition is in use (a term the shared settings removed waits for her),
    her save in the dashboard that lowers protection waits for the security team, which ends the change that waited
    for her; then a computer's destinations are saved over the composition. What she asked for is not in the settings
    kept, so it is never asked again as their content: nothing labelled hers holds the shared settings' removal, and
    here, in memory, where nothing applies the settings again, her request still waits as she made it, for the security
    team. On a server the re-application this save queues offers the composition's own change, which supersedes her
    request (at most one waits): she is told, and asks again."""
    company = Company(Queue(["Mark Ellis"]))
    runtime, settings = _space(tmp_path, company)
    try:
        settings.approved, settings.approved_part = runtime.config, {}  # what she approved: the settings as they are
        settings._shared = lambda: SHARED.model_copy(update={"terms": []})  # Project Falcon removed for everyone
        spaces._reapply(SimpleNamespace(runtime=runtime, identity=EMILY))
        [arrived] = runtime.mediator.approvals.waiting_changes()
        assert arrived.decides == "self" and runtime._held() is not None  # [D1]: hers, over a composition
        with pytest.raises(Waits) as waits:
            runtime.update_config(_with_acme, from_dashboard=True)
        asked = waits.value.change
        assert (asked.source, asked.decides) == ("dashboard", "security") and "ACME" in asked.config.allow_values
        assert runtime.add_destinations([(ADDED, "claude")])
        assert any(d.url == ADDED for d in runtime.config.upstreams) and "ACME" not in runtime.config.allow_values
        [still] = runtime.mediator.approvals.waiting_changes()
        assert still.id == asked.id and "ACME" in still.config.allow_values
        assert company.queue.opens == [asked] and company.queue.ends == []
        assert runtime.decide_change(still.id, True) == "not-yours"
    finally:
        runtime.close()


def test_a_key_s_destinations_while_its_own_change_waits_never_save_what_waits(tmp_path):
    """The review of Task 2, m6 (R29 in an API key's space): its destinations are saved as approved settings; the change
    of its own that waited is over (a save of a key's settings approves what it sets) and asked again with them, never
    written as the key's own part; the next re-application, which finds nothing lowered, supersedes it, as a save by
    the administration does. Protection is never lower for it: what waited never applies."""
    key = IdentityRef("u_billingcia", "Billing CI", "key")
    runtime, settings = _space(tmp_path, Community(), identity=key)
    try:
        change = runtime.propose(_with_acme(runtime.config), "file")
        assert change is not None and change.decides == "self"
        assert runtime.add_destinations([(ADDED, "claude")])
        approvals = runtime.mediator.approvals
        assert approvals.settled[change.id] == "superseded"
        [again] = approvals.waiting_changes()
        assert "ACME" in again.config.allow_values and any(d.url == ADDED for d in again.config.upstreams)
        assert "allow_values" not in settings.own and any(d["url"] == ADDED for d in settings.own["upstreams"])
        assert settings.reviewed() == runtime.config and "ACME" not in runtime.config.allow_values
        spaces._settle_obsolete(runtime, every=True)
        assert approvals.waiting_changes() == [] and approvals.settled[again.id] == "superseded"
    finally:
        runtime.close()
