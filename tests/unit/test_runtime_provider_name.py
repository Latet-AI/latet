"""A person linked to the company's identity provider is masked under the provider's name (Latet Enterprise, decision
(b) of 2026-10-25: ``Edition.provider_name``), as the free runtime takes it: overlaid on the settings in use after the
raise where they leave names (PERSON) in clear everywhere, never on the person's own settings. Where names are masked,
the person's own name stays in use, masked; a computer's name fills an empty one for that. A change of their own name is
refused whenever the edition names them, whatever the save changes besides. The Community Edition, which names nobody,
leaves a person's own name as it was."""

import json
from dataclasses import replace

import pytest

from latet.api.rest import Dashboard, _floor_unavailable
from latet.config import Config, IdentityConfig, ProjectEntry, TermConfig
from latet.edition import Community
from latet.engine.types import Policy
from latet.floor import (
    ACCOUNT_UNAVAILABLE,
    FLOOR_UNAVAILABLE,
    PROVIDER_NAME,
    TERMS_UNAVAILABLE,
    AccountUnavailable,
    BelowFloor,
    FloorUnavailable,
    TermsUnavailable,
)
from latet.gateway.app import OnServer
from latet.runtime import make_runtime, provider_named
from latet.server.machines import merged_identity
from latet.state import local_backends

from ..conftest import ROOT_KEY
from ..floors import FloorEdition
from .server.conftest import EMILY, SHARED, URL, MemorySettings

IBAN = "DE89 3704 0044 0532 0130 00"
ALLOW = {"PERSON": Policy.ALLOW}
AUTHOR = "commit 1a2b Author: Emily Jane Carter"  # her git author, as her computers send it


class Named(FloorEdition):
    """The Enterprise Edition's answer for a person whose account is linked to the provider: the name it gives (None:
    not linked; an exception: it cannot be said), as it answers at each ask."""

    def __init__(self, name="Emily Carter"):
        super().__init__()
        self.name = name

    def provider_name(self, identity):
        if isinstance(self.name, Exception):
            raise self.name
        return self.name if identity is not None and identity.kind == "person" else None


def _space(tmp_path, edition, policies=ALLOW, full_name=IBAN, projects=()):
    """Emily's space as a server makes it, over ``edition``: her own settings (by default they let PERSON through, and
    name her as the IBAN, written before her account was linked)."""
    settings = MemorySettings(SHARED)
    identity = IdentityConfig(full_name=full_name)
    own = SHARED.model_copy(
        update={"policies": dict(policies), "identity": identity, "projects": [*SHARED.projects, *projects]}
    )
    settings.save(own)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY, edition=edition
    )
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    return rt, settings


def _identity(config, **changes):
    return config.model_copy(update={"identity": config.identity.model_copy(update=changes)})


def test_a_name_from_the_provider_is_the_one_masked_and_a_change_of_it_is_refused(tmp_path):
    rt, settings = _space(tmp_path, Named())
    try:
        assert rt.named_by_provider == "Emily Carter"
        assert rt.in_use().identity.full_name == "Emily Carter" and rt.config.identity.full_name == IBAN
        assert rt.raised(rt.config).identity.full_name == "Emily Carter"  # the playground's, and a project's
        masked = rt.built.masker.mask(f"Pay {IBAN} to the supplier")
        assert IBAN not in masked.text and {r.etype for r in masked.replacements} == {"IBAN"}
        mine = rt.config
        with pytest.raises(BelowFloor) as refused:
            rt.update_config(lambda config: _identity(config, full_name="Mallory"))
        assert refused.value.reasons == [PROVIDER_NAME] and rt.config == mine
        assert settings.load().identity.full_name == IBAN  # nothing kept
        rt.update_config(lambda config: _identity(config, usernames=["emily"]))
        assert rt.config.identity.usernames == ["emily"] and rt.in_use().identity.full_name == "Emily Carter"
        rt.update_config(lambda config: _identity(config, enabled=False))
        assert not rt.config.identity.enabled and rt.in_use().identity.full_name == "Emily Carter"
        with pytest.raises(BelowFloor) as refused:
            rt.propose(_identity(rt.config, full_name="Mallory"), "command")
        assert refused.value.reasons == [PROVIDER_NAME]
        waiting = rt.propose(_identity(rt.config, full_name="Mallory", enabled=True), "file")  # a file read again
        assert waiting is not None
        with pytest.raises(BelowFloor) as refused:
            rt.decide_change(waiting.id, True)
        assert refused.value.reasons == [PROVIDER_NAME] and rt.config.identity.full_name == IBAN
        assert rt.mediator.approvals.settled[waiting.id] == "rejected"
    finally:
        rt.close()


def test_the_community_edition_keeps_the_persons_own_name(tmp_path):
    rt, _ = _space(tmp_path, Community())
    try:
        assert rt.named_by_provider is None and rt.in_use().identity.full_name == IBAN
        rt.update_config(lambda config: _identity(config, full_name="Mallory"))
        assert rt.config.identity.full_name == "Mallory" and rt.in_use().identity.full_name == "Mallory"
        assert rt.named_by_provider is None
    finally:
        rt.close()


@pytest.mark.parametrize(
    ("policies", "projects"),
    [
        ({}, ()),  # PERSON masked: her default
        (ALLOW, (ProjectEntry(path="/home/emily/billing", policies={"PERSON": Policy.MASK}),)),  # masked in a folder
    ],
    ids=["her default", "masked in one of her folders"],
)
def test_where_names_are_masked_her_own_name_stays_in_use_and_masked(tmp_path, policies, projects):
    """A full name is an identity value of type PERSON alone: where PERSON is masked, anywhere, the provider's name in
    place of hers would leave her git author undetected, in clear (fix round 1 of step 6b). Her name is the provider's
    to give all the same: a change of it is refused (fix round 2)."""
    rt, _ = _space(tmp_path, Named(), policies=policies, full_name="Emily Jane Carter", projects=projects)
    try:
        assert rt.provided_name == "Emily Carter" and rt.named_by_provider is None
        assert rt.in_use().identity.full_name == "Emily Jane Carter"
        assert rt.raised(rt.config).identity.full_name == "Emily Jane Carter"
        if not projects:
            assert "Emily Jane Carter" not in rt.built.masker.mask(AUTHOR).text
        else:
            assert rt.projects is not None
            scope = rt.projects.resolve("/home/emily/billing").scope
            assert scope is not None and "Emily Jane Carter" not in scope.masker.mask(AUTHOR).text
        with pytest.raises(BelowFloor) as refused:
            rt.update_config(lambda config: _identity(config, full_name="E. Carter"))
        assert refused.value.reasons == [PROVIDER_NAME] and rt.config.identity.full_name == "Emily Jane Carter"
    finally:
        rt.close()


FOLDER = ProjectEntry(path="/home/emily/billing", policies={"PERSON": Policy.MASK})


@pytest.mark.parametrize(
    ("policies", "save"),
    [
        ({}, {"policies": ALLOW}),  # names let through, and a name of hers, at once
        (ALLOW, {"policies": {}}),  # names masked, and a name of hers, at once
        (ALLOW, {"projects": [*SHARED.projects, FOLDER]}),  # a folder masking names, and a name of hers, at once
    ],
    ids=["letting names through", "masking names", "a folder masking names"],
)
def test_a_change_of_her_name_is_refused_while_the_provider_names_her_whatever_else_the_save_changes(
    tmp_path, policies, save
):
    """The refusal follows the edition's answer, never the name in use: no save, nor two in a row, gives her a name of
    her own choosing where the provider names her (fix round 2 of step 6b). The rest of the save, alone, applies."""
    rt, _ = _space(tmp_path, Named(), policies=policies, full_name=IBAN)
    try:
        mine = rt.config
        with pytest.raises(BelowFloor) as refused:
            rt.update_config(lambda config: _identity(config, full_name="Mallory").model_copy(update=save))
        assert refused.value.reasons == [PROVIDER_NAME] and rt.config == mine
        rt.update_config(lambda config: config.model_copy(update=save))
        with pytest.raises(BelowFloor) as refused:
            rt.update_config(lambda config: _identity(config, full_name="Mallory"))
        assert refused.value.reasons == [PROVIDER_NAME] and rt.config.identity.full_name == IBAN
    finally:
        rt.close()


def test_the_refusal_asks_the_edition_at_once_and_refuses_while_it_cannot_say(tmp_path):
    """Linked a moment ago, before her space heard of it: her name is the provider's to give already. And while the
    edition cannot say, a change of her name is refused, never let through."""
    edition = Named(None)
    rt, _ = _space(tmp_path, edition, policies=ALLOW, full_name=IBAN)
    try:
        assert rt.provided_name is None and rt.named_by_provider is None
        edition.name = "Emily Carter"
        with pytest.raises(BelowFloor) as refused:
            rt.update_config(lambda config: _identity(config, full_name="Mallory"))
        assert refused.value.reasons == [PROVIDER_NAME]
        edition.name = None  # unlinked again: a change of her name waits for her, as any change that lowers protection
        waiting = rt.propose(_identity(rt.config, full_name="Mallory"), "file")
        assert waiting is not None
        edition.name = FloorUnavailable("a person's account could not be read")
        with pytest.raises(FloorUnavailable):
            rt.update_config(lambda config: _identity(config, full_name="Mallory"))
        with pytest.raises(FloorUnavailable):  # nothing decided: the change still waits, never approved unapplied
            rt.decide_change(waiting.id, True)
        assert waiting.id in rt.mediator.approvals.changes and rt.config.identity.full_name == IBAN
    finally:
        rt.close()


def _sent(name):
    """A computer's identity merged in, as ``machines.put_identity`` merges it: its name fills an empty one only."""

    def change(config):
        merged = merged_identity(config.identity, IdentityConfig(full_name=name, usernames=["emily.carter"]))
        return None if merged is None else config.model_copy(update={"identity": merged})

    return change


def test_a_computers_name_fills_an_empty_one_and_is_masked_once_names_are(tmp_path):
    """Her computers send her git author at `latet connect`: kept while the provider's name is in use, so that it is
    masked once names are (the company's floor, her own settings or a folder masking them), never left undetected."""
    rt, _ = _space(tmp_path, Named(), policies=ALLOW, full_name=None)
    try:
        rt.update_config(_sent("Emily Jane Carter"), fills_name=True)
        assert rt.config.identity.full_name == "Emily Jane Carter" and rt.config.identity.usernames == ["emily.carter"]
        assert rt.named_by_provider == "Emily Carter" and rt.in_use().identity.full_name == "Emily Carter"
        assert rt.update_config(_sent("Mallory"), fills_name=True) is None  # a held name is never replaced by one
        with pytest.raises(BelowFloor) as refused:  # nor by a computer's call that would replace it
            rt.update_config(lambda config: _identity(config, full_name="Mallory"), fills_name=True)
        assert refused.value.reasons == [PROVIDER_NAME]
        rt.update_config(lambda config: config.model_copy(update={"policies": {}}))  # names masked from now on
        assert rt.named_by_provider is None and rt.in_use().identity.full_name == "Emily Jane Carter"
        assert "Emily Jane Carter" not in rt.built.masker.mask(AUTHOR).text
    finally:
        rt.close()


def test_the_providers_name_keeps_the_company_terms_and_the_carving():
    """The overlay is a copy that keeps what no dump holds: the terms the company locks, and the carving of what a
    person leaves in clear (``Config.locked_terms``, ``Config.carves_terms``)."""
    config = Config(ner="off", identity=IdentityConfig(full_name=IBAN))
    config.lock_terms([TermConfig(value="Project Falcon", type="PROJECT")])
    config.carve_terms()
    named = provider_named(config, "Emily Carter")
    assert named.identity.full_name == "Emily Carter" and config.identity.full_name == IBAN
    assert named.carves_terms and named.locked_terms == config.locked_terms


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("named", "policies", "locked", "shown"),
    [
        ("Emily Carter", ALLOW, True, "provider"),  # the provider's name in use
        ("Emily Carter", {}, True, None),  # names masked: her own in use, masked, and not hers to change either
        (None, ALLOW, False, None),  # not linked
        (False, ALLOW, False, None),  # the Community Edition
    ],
    ids=["the provider's in use", "hers in use", "not linked", "community"],
)
def test_the_rules_page_is_told_whether_a_change_of_her_name_is_refused(tmp_path, named, policies, locked, shown):
    """/config's identity says ``full_name_locked`` whenever a change of her name would be refused, whichever name is in
    use (``full_name_from`` names the provider only where its name is the one in use)."""
    edition = Community() if named is False else Named(named)
    rt, _ = _space(tmp_path, edition, policies=policies, full_name="E. Carter")
    try:
        view = Dashboard(rt)._config_view(rt.config)["identity"]
        assert (view["full_name_locked"], view["full_name_from"]) == (locked, shown)
        if locked:  # as the view says: refused
            with pytest.raises(BelowFloor) as refused:
                rt.update_config(lambda config: _identity(config, full_name="Mallory"))
            assert refused.value.reasons == [PROVIDER_NAME]
        else:
            rt.update_config(lambda config: _identity(config, full_name="Mallory"))
            assert rt.config.identity.full_name == "Mallory"
    finally:
        rt.close()


def test_a_refusal_while_the_persons_account_cannot_be_read_names_the_account():
    """The edition cannot read the account for the name a person is masked under (Latet Enterprise): the 503 says so
    in fixed words of its own, never that the company's policy cannot be read."""
    assert issubclass(AccountUnavailable, FloorUnavailable)
    for failure, words in (
        (AccountUnavailable("unread"), ACCOUNT_UNAVAILABLE),
        (FloorUnavailable("x"), FLOOR_UNAVAILABLE),
        (TermsUnavailable("unread"), TERMS_UNAVAILABLE),
    ):
        answer = _floor_unavailable(failure)
        assert (answer.status_code, answer.headers["retry-after"]) == (503, "1")
        assert json.loads(bytes(answer.body)) == {"error": words} and "unread" not in words
