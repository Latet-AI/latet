"""What a token of an API key may do: scan, mask, unmask and forget, mask bringing scan and nothing bringing forget; how
its row keeps them (NULL for the tokens that serve agents, which scan, mask and unmask and may not forget: a key's first,
a person's, every token made before permissions; a token added to a key keeps its own, all three too); and the grant a
lookup answers. No services are needed."""

import itertools
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.server import grants
from latet.server.grants import DEFAULT, PERMISSIONS, Grant, bridge_needs, loaded, normalized, stored
from latet.server.identities import Identity

MIGRATIONS = Path(grants.__file__).parent / "migrations"
KEY = Identity("u_billingcix", "Billing CI", None, "key", False, 0.0, None, "local", None)


def test_mask_brings_scan():
    assert normalized(["mask"]) == {"mask", "scan"}


def test_a_token_without_permissions_of_its_own_scans_masks_and_unmasks():
    assert loaded(None) == DEFAULT == {"scan", "mask", "unmask"}


def test_a_token_added_to_a_key_keeps_all_three_in_its_row():
    """Ruling T13c: a token added to an API key keeps what it may do in its row, scan, mask and unmask too, so that NULL
    stays the row of the tokens that serve agents alone."""
    assert stored(normalized(["mask", "unmask"])) == stored(DEFAULT) == "mask,scan,unmask"
    assert loaded("mask,scan,unmask") == DEFAULT


def test_forget_is_never_implied():
    """Task 11b: a token without permissions of its own (NULL: a key's first token, every token made before them) cannot
    forget, nor does any other permission bring it: it is given by name alone, and a token that holds every one of them
    keeps them in its row, never NULL."""
    assert "forget" in PERMISSIONS and "forget" not in DEFAULT
    assert normalized(["forget"]) == {"forget"}
    assert normalized(["mask", "forget"]) == {"mask", "scan", "forget"}
    assert "forget" not in normalized(["scan", "mask", "unmask"])
    assert stored(normalized(["mask", "unmask", "forget"])) == "forget,mask,scan,unmask"
    assert not Grant(KEY, "t_aaaaaaaaaa", DEFAULT).allows("forget")
    assert Grant(KEY, "t_aaaaaaaaaa", DEFAULT).lacking("forget") == "forget"


def test_stored_forms():
    assert stored(normalized(["scan"])) == "scan"
    assert stored(normalized(["unmask"])) == "unmask"
    assert stored(normalized(["scan", "unmask"])) == "scan,unmask"
    assert stored(normalized(["mask"])) == "mask,scan"
    assert stored(normalized(["forget"])) == "forget"
    assert stored(normalized(["forget", "scan"])) == "forget,scan"
    assert stored(normalized(["mask", "forget"])) == "forget,mask,scan"
    assert stored(normalized(["unmask", "forget"])) == "forget,unmask"
    assert stored(normalized(["scan", "unmask", "forget"])) == "forget,scan,unmask"


@pytest.mark.parametrize("given", [[], ["read"], ["MASK"], ["Forget"]])
def test_unknown_or_no_permission_refused(given):
    with pytest.raises(ValueError, match="scan, mask, unmask, forget"):
        normalized(given)


def test_loaded_refuses_what_stored_cannot_make():
    with pytest.raises(ValueError, match="not in a form Latet stores"):
        loaded("mask")


@pytest.mark.parametrize(
    "column",
    ["", "scan,mask,unmask", "scan,mask", "SCAN", "scan, unmask", "scan,", "forget,mask", "mask,scan,forget", "FORGET"],
)
def test_loaded_reads_no_other_form_and_quotes_none(column):
    """What a writer of the database put there is never read as permissions, and its words are not repeated."""
    with pytest.raises(ValueError, match="not in a form Latet stores") as refused:
        loaded(column)
    assert column == "" or column not in str(refused.value)


def test_what_stored_makes_loaded_reads_back():
    for size in range(1, len(PERMISSIONS) + 1):
        for chosen in [PERMISSIONS[:size], PERMISSIONS[-size:]]:
            granted = normalized(chosen)
            assert loaded(stored(granted)) == granted


@pytest.mark.parametrize("given", [frozenset({"mask"}), frozenset(), frozenset({"read"})])
def test_stored_takes_only_what_normalized_gives(given):
    with pytest.raises(ValueError, match="a token's permissions"):
        stored(given)


def _check_forms() -> tuple[str, set[str]]:
    """The latest migration that sets the column's CHECK, and the forms it allows."""
    latest = None
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        found = re.search(r"permissions IN \(([^)]*)\)", migration.read_text("utf-8"))
        if found is not None:
            latest = migration.name, set(re.findall(r"'([^']*)'", found.group(1)))
    assert latest is not None
    return latest


def test_the_databases_check_allows_the_forms_stored_makes_and_no_other():
    """The column's CHECK, as the latest migration that sets it has it, and ``loaded`` agree, form for form."""
    name, allowed = _check_forms()
    made = {
        stored(normalized(chosen))
        for size in range(1, len(PERMISSIONS) + 1)
        for chosen in itertools.combinations(PERMISSIONS, size)
    }
    assert name == "011_added_token_permissions.sql"
    assert allowed == made and len(made) == 11 and "mask,scan,unmask" in made
    assert all(loaded(form) for form in allowed)


def test_a_grant_allows_what_it_holds():
    full, logs = Grant(KEY, "t_aaaaaaaaaa", DEFAULT, agents=True), Grant(KEY, "t_bbbbbbbbbb", normalized(["mask"]))
    assert full.gateway and full.allows("scan", "mask", "unmask")
    assert not logs.gateway and logs.allows("mask") and logs.allows("scan") and not logs.allows("mask", "unmask")
    assert logs.allows()  # what needs no permission (the bridge's ping)
    assert not Grant(KEY, "t_cccccccccc", normalized(["unmask"])).gateway
    assert not Grant(KEY, "t_dddddddddd", normalized(["forget"])).gateway
    assert "Billing CI" not in repr(logs)  # printed, it names its identity by its id


def test_only_a_token_without_permissions_of_its_own_serves_agents():
    """Ruling T13b: a token added to an API key, whatever it may do, calls no model through Latet: its unmasks go
    through the applications API alone, each in the administration log. A grant serves agents only when it says so (a
    lookup of a NULL row), never by default."""
    assert not Grant(KEY, "t_aaaaaaaaaa", DEFAULT).gateway  # an added token with all three
    assert not Grant(KEY, "t_bbbbbbbbbb", normalized(["mask", "unmask", "forget"])).gateway
    assert not Grant(KEY, "t_cccccccccc", DEFAULT).agents
    assert Grant(KEY, "t_dddddddddd", DEFAULT, agents=True).gateway  # a key's first token


def test_bridge_needs():
    """What each of the plugin's bridge calls needs of a token: a call the list does not name needs scan, mask and
    unmask, so that a restricted token is refused what nobody thought of, and a token without permissions of its own is
    refused nothing it was given before."""
    engine = "/_latet/engine/v1"
    assert bridge_needs(f"{engine}/ping") == frozenset()
    assert bridge_needs(f"{engine}/route-check") == frozenset()
    assert bridge_needs(f"{engine}/mask") == {"mask"}
    assert bridge_needs(f"{engine}/verify") == {"mask"}
    assert bridge_needs(f"{engine}/outbound") == {"mask"}
    assert bridge_needs(f"{engine}/instructions") == {"mask"}
    assert bridge_needs(f"{engine}/approvals/wait") == {"mask"}
    assert bridge_needs(f"{engine}/rehydrate") == {"unmask"}
    assert bridge_needs(f"{engine}/scope") == {"unmask"}
    assert bridge_needs(f"{engine}/aws-sessions") == {"mask", "unmask"}
    assert bridge_needs(f"{engine}/aws-sessions/session-one") == {"mask", "unmask"}
    assert bridge_needs(f"{engine}/aws-sessions/session-one/hold") == {"mask", "unmask"}
    assert bridge_needs(f"{engine}/something-new") == DEFAULT
    for unnamed in (f"{engine}/approvals", f"{engine}/aws-sessionsx", f"{engine}/ping/more", "/_latet/admin/known"):
        assert bridge_needs(unnamed) == DEFAULT, unnamed
        assert Grant(KEY, "t_aaaaaaaaaa", DEFAULT).lacking(*bridge_needs(unnamed)) is None
        assert Grant(KEY, "t_bbbbbbbbbb", normalized(["forget", "scan"])).lacking(*bridge_needs(unnamed)) == "mask"


def test_every_route_of_the_bridge_is_named():
    """Each route the plugin's bridge serves has a decision of its own in ``bridge_needs``: a new one, until it is named
    there, needs scan, mask and unmask, and this test fails, so that someone decides what it needs."""
    from latet.bridge.api import Bridge

    routes = Bridge(SimpleNamespace(engine=None), "token").routes()  # type: ignore[arg-type]
    assert routes
    for route in routes:
        assert route.path.startswith("/_latet/engine/v1/"), route.path
        assert bridge_needs(route.path) != DEFAULT, route.path
        assert "forget" not in bridge_needs(route.path), route.path  # no call of the bridge forgets


def test_a_grant_names_the_first_permission_it_lacks():
    logs, reader = Grant(KEY, "t_aaaaaaaaaa", normalized(["mask"])), Grant(KEY, "t_bbbbbbbbbb", normalized(["unmask"]))
    assert logs.lacking(*bridge_needs("/_latet/engine/v1/rehydrate")) == "unmask"
    assert logs.lacking(*bridge_needs("/_latet/engine/v1/mask")) is None
    assert logs.lacking() is None  # what needs no permission (the bridge's ping)
    assert reader.lacking(*DEFAULT) == "scan"  # in the order PERMISSIONS names them
    assert reader.lacking("unmask", "mask") == "mask"
    assert reader.lacking("forget", "mask") == "mask"
    assert Grant(KEY, "t_cccccccccc", DEFAULT).lacking(*DEFAULT) is None
    assert Grant(KEY, "t_cccccccccc", DEFAULT).lacking(*PERMISSIONS) == "forget"
