"""A person's own identity left in clear where terms are carved (Latet Enterprise, `exceptions: company`): their name,
user names, e-mail addresses and the domain of their address, and host names, of a type the company allows, are cut
around what the masking masks without them, as their terms are (``Config.carves_terms``), so that none set to an IBAN or
a token leaves it in clear. Without carving (the Community Edition, no floor), the masking is as it was."""

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from latet.config import Config, IdentityConfig
from latet.engine.build import build_masker
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY
from .test_locked_terms import TICKET, TOKENS

IBAN, COMPACT = "DE89 3704 0044 0532 0130 00", "DE89370400440532013000"
ALLOWED = {"PERSON": "allow", "USERNAME": "allow", "EMAIL": "allow", "HOSTNAME": "allow", "DOMAIN": "allow"}


def _masker(config, identity=None):
    return build_masker(config, Vault(ROOT_KEY), identity)[0]


def _carved(config):
    config = config.model_copy(deep=True)
    config.carve_terms()
    return config


def _masked_at(masker, text):
    """The characters of ``text`` masked (a replacement that is not let through), by their places."""
    return {i for r in masker.mask(text).replacements for i in range(r.start, r.end)}


def test_a_name_of_yours_set_to_an_iban_is_cut_around_it_where_terms_are_carved():
    mine, me = Config(ner="off", policies=ALLOWED), IdentityConfig(full_name=IBAN)
    text = f"Please pay {IBAN} today, signed {IBAN}"
    # No carving (Community): your name is let through, and gives way to the IBAN found where it stands (0.9.10: a value
    # let through leaves in clear itself and nothing more), the IBAN masked all the same.
    assert IBAN not in _masker(mine, me).mask(text).text
    masked = _masker(_carved(mine), me).mask(text)
    assert IBAN not in masked.text and {r.etype for r in masked.replacements} == {"IBAN"}
    assert _masked_at(_masker(_carved(mine)), text) <= _masked_at(_masker(_carved(mine), me), text)


def test_your_name_inside_a_masked_field_leaves_none_of_it_in_clear_where_terms_are_carved():
    """The identity carve's discriminating guard since 0.9.10: a value of a structure longer than your name is never
    inside the group your name names, so ``_merged`` alone leaves your name's part of the field in clear; carved, the
    field is masked whole."""
    mine = Config(ner="off", fields=[{"name": "ledger", "type": "LEDGER"}], policies={"PERSON": "allow"})
    me = IdentityConfig(full_name="Emily Carter")
    text = json.dumps({"ledger": "Emily Carter ledger 7", "note": "Emily Carter called"})
    assert '"ledger": "Emily Carter ' in _masker(mine, me).mask(text, SlotKind.TOOL_RESULT).text  # as before
    masked = _masker(_carved(mine), me).mask(text, SlotKind.TOOL_RESULT).text
    assert '"ledger": "Emily Carter' not in masked and "Emily Carter called" in masked  # field whole; outside, yours


@pytest.mark.parametrize(
    "text",
    [
        f"the host {COMPACT}.example answers",
        f"write ops@{COMPACT}.example, host {COMPACT}.example",
    ],
)
def test_the_domain_of_your_address_left_in_clear_never_names_a_masked_value_where_terms_are_carved(text):
    """The identity detector finds your address's domain wherever it stands (personal data, its rule its own), and your
    address brings it along as a repeat: neither, left in clear, names the group of a value masked without them."""
    mine, me = Config(ner="off", policies=ALLOWED), IdentityConfig(emails=[f"ops@{COMPACT}.example"])
    masker = _masker(_carved(mine), me)
    assert COMPACT not in masker.mask(text).text
    assert _masked_at(_masker(_carved(mine)), text) <= _masked_at(masker, text)


@pytest.mark.parametrize(
    ("me", "text"),
    [
        (IdentityConfig(hostnames=[COMPACT]), f"host {COMPACT} is up"),
        (IdentityConfig(usernames=[COMPACT]), f"/home/{COMPACT}/notes.txt and {COMPACT}"),
    ],
    ids=["host name", "user name"],
)
def test_your_host_and_user_names_left_in_clear_are_cut_as_your_name_is(me, text):
    mine = Config(ner="off", policies=ALLOWED)
    assert COMPACT not in _masker(mine, me).mask(text).text  # no carving: it gives way to the IBAN (0.9.10)
    assert COMPACT not in _masker(_carved(mine), me).mask(text).text


@st.composite
def texts_and_identities(draw):
    """A text of TOKENS, and an identity whose name, user name, host name and address (one holding @) are each a run of
    one to three of its words."""
    words = draw(st.lists(st.sampled_from([*TOKENS, "Emily Carter", "build-runner-01"]), min_size=1, max_size=8))
    seps = draw(st.lists(st.sampled_from([" ", "; ", " and ", "."]), min_size=len(words), max_size=len(words)))
    text = "".join(w + s for w, s in zip(words, seps, strict=True)).strip()

    def run():
        start = draw(st.integers(0, len(words) - 1))
        end = draw(st.integers(start + 1, min(len(words), start + 3)))
        return "".join(w + s for w, s in zip(words[start:end], seps[start:end], strict=True)).strip(" ;.")

    name, user, host, address = run(), run(), run(), run()
    me = IdentityConfig(
        full_name=name or None,
        usernames=[user] if user else [],
        hostnames=[host] if host else [],
        emails=[address] if "@" in address else [],
    )
    return text, me


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(texts_and_identities())
def test_where_terms_are_carved_your_identity_left_in_clear_never_unmasks_a_character(drawn):
    """Every character the masking masks without your identity, it masks with it, left in clear."""
    text, me = drawn
    mine = _carved(Config(ner="off", pii=["EMAIL", "IBAN"], recognizers=[TICKET], policies=ALLOWED))
    assert _masked_at(_masker(mine), text) <= _masked_at(_masker(mine, me), text), (text, me)
