"""The dashboard's sign-ins through a store that other dashboards may share: a code works where its subject's
dashboard is, a session is that subject's alone, and what is kept of a secret is its hash."""

import hashlib

import pytest
from starlette.testclient import TestClient

from latet.api import rest as rest_module
from latet.api.rest import Dashboard
from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.runtime import make_runtime
from latet.state import CODE_TTL, SESSION_TTL, IdentityRef, LocalSignIns

from ...conftest import ROOT_KEY
from .conftest import _live_runtime


def test_a_code_one_dashboard_gave_signs_in_at_another_over_the_same_sign_ins(tmp_path):
    shared = LocalSignIns()
    one, two = (_live_runtime(tmp_path / name) for name in ("one", "two"))
    try:
        one.gateway.dashboard = Dashboard(one, sign_ins=shared)
        two.gateway.dashboard = Dashboard(two, sign_ins=shared)
        with TestClient(create_app(two.gateway), base_url="http://127.0.0.1:5283") as client:
            assert client.get("/_latet/api/v1/me").status_code == 401
            code = one.gateway.dashboard.issue_code()
            assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
            assert client.get("/_latet/api/v1/me").status_code == 200
            again = client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            assert again.headers["location"].endswith("?login=expired")
            client.cookies.clear()
            client.cookies.set("latet_session", "not-a-session")
            assert client.get("/_latet/api/v1/me").status_code == 401
    finally:
        one.close()
        two.close()


EMILY = IdentityRef("u_emilycarte", "Emily Carter", "person")


@pytest.fixture
def two_dashboards(tmp_path):
    """Your computer's runtime and one that is Emily's space on a server, their dashboards over one store of sign-ins
    (a server's may be one for every identity): the runtimes, and the store."""
    shared, config = LocalSignIns(), Config(identity=IdentityConfig(enabled=False))
    yours = make_runtime(config, data=tmp_path / "yours", config_file=tmp_path / "yours.yaml", root_key=ROOT_KEY)
    emilys = make_runtime(
        config, data=tmp_path / "emilys", config_file=tmp_path / "emilys.yaml", root_key=ROOT_KEY, identity=EMILY
    )
    for rt in (yours, emilys):
        rt.gateway.dashboard = Dashboard(rt, sign_ins=shared)
    yield yours, emilys, shared
    yours.close()
    emilys.close()


def _dashboard_of(rt):
    return TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283")


def test_a_sign_in_opens_the_session_of_the_subject_its_code_was_made_for(two_dashboards):
    _, emilys, shared = two_dashboards
    with _dashboard_of(emilys) as client:
        asked = client.post("/_latet/admin/login-code", headers={"x-latet-token": emilys.gateway.bridge_token})
        signed = client.get(f"/_latet/ui/login?code={asked.json()['code']}", follow_redirects=False)
        assert signed.headers["location"] == "/_latet/ui/"
        # The code the dashboard gives is for the identity whose space it is, and so is the session it opens.
        assert shared.subject_of(client.cookies["latet_session"]) == "u_emilycarte"
        assert client.get("/_latet/api/v1/me").status_code == 200


def test_a_code_made_for_another_subject_signs_in_at_no_other_dashboard(two_dashboards):
    yours, emilys, shared = two_dashboards
    for here, there in ((yours, emilys), (emilys, yours)):
        code = there.gateway.dashboard.issue_code()  # for the other dashboard's subject, in the store both read
        with _dashboard_of(here) as client:
            refused = client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            assert refused.headers["location"].endswith("?login=expired") and "set-cookie" not in refused.headers
            assert client.get("/_latet/api/v1/me").status_code == 401
    assert shared._sessions == {}  # no session was opened, for either subject


def test_a_session_of_another_subject_is_no_session_at_this_dashboard(two_dashboards):
    yours, emilys, shared = two_dashboards
    mine, hers = shared.open(""), shared.open("u_emilycarte")  # as each one's own dashboard opened them
    for here, own, others in ((yours, mine, hers), (emilys, hers, mine)):
        with _dashboard_of(here) as client:
            client.cookies.set("latet_session", others)
            assert client.get("/_latet/api/v1/me").status_code == 401
            client.cookies.set("latet_session", own)
            assert client.get("/_latet/api/v1/me").status_code == 200


def test_the_session_a_sign_in_opens_is_the_cookie_it_has_always_been_and_only_its_hash_is_kept(live):
    """Signing in sets the session id in a cookie no script can read and no other site sends, for the dashboard's paths
    and twelve hours, with no other attribute; the sign-ins keep the SHA-256 of that id and of the code, never either
    one."""
    dashboard = live.gateway.dashboard
    with TestClient(create_app(live.gateway), base_url="http://127.0.0.1:5283") as client:
        code = dashboard.issue_code()
        signed = client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
        named, *attributes = signed.headers["set-cookie"].split("; ")
        name, sid = named.split("=", 1)
        assert name == "latet_session" and client.cookies["latet_session"] == sid
        assert sorted(attributes) == ["HttpOnly", "Max-Age=43200", "Path=/_latet", "SameSite=strict"]  # these, no more
        assert list(dashboard.sign_ins._sessions) == [hashlib.sha256(sid.encode()).hexdigest()]
        assert dashboard.sign_ins._codes == {}  # the code was used: gone, not kept as used
        assert code not in repr(vars(dashboard.sign_ins)) and sid not in repr(vars(dashboard.sign_ins))


def test_a_sign_ins_code_lasts_two_minutes_and_its_session_twelve_hours_and_the_dashboard_still_names_them():
    assert (CODE_TTL, SESSION_TTL) == (120.0, 12 * 3600.0)
    assert rest_module.CODE_TTL is CODE_TTL and rest_module.SESSION_TTL is SESSION_TTL  # where they were named before


def test_dashboards_given_no_sign_ins_keep_their_own_and_a_code_signs_in_only_where_it_was_made(tmp_path):
    one, two = (_live_runtime(tmp_path / name) for name in ("one", "two"))  # each runtime makes its own dashboard
    try:
        assert one.gateway.dashboard.sign_ins is not two.gateway.dashboard.sign_ins
        code = one.gateway.dashboard.issue_code()
        with TestClient(create_app(two.gateway), base_url="http://127.0.0.1:5283") as client:
            refused = client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            assert refused.headers["location"].endswith("?login=expired")
            assert client.get("/_latet/api/v1/me").status_code == 401
    finally:
        one.close()
        two.close()
