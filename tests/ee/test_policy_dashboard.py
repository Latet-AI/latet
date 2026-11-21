"""Every person's dashboard is told the floor in force, where it comes from and what it sets, and which of their own
settings are below it, raised while in use. What it sets is named, never quoted: no term, value or pattern of the
company's reaches a person (M6)."""

from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.ee.policy import parse_policy
from latet.ee.policy_routes import policy_routes
from latet.ee.policy_server import PolicyUnavailable
from latet.ee.policy_sources import source_view
from latet.gateway.app import create_app
from latet.runtime import make_runtime

from ..floors import FloorEdition

ORIGIN = "https://latet.acme.lan"
SECRET_POLICY = """latet_rules: 1
exceptions: company
profile: max
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Northwind Traders
    type: CLIENT
allow_values:
  - acme-staging.example
allow_patterns:
  - 'ACME-[0-9]{6}'
recognizers:
  - name: contract-number
    type: CONTRACT
    pattern: 'CN-[0-9]{8}'
data_hosts:
  - falcon.acme.lan
llm_review:
  guidance: Never name the Falcon programme to the reviewer.
"""


class Source:
    """A server's policy as the route reads it (Task 5's ServerPolicy, without PostgreSQL): its floor and summary."""

    def __init__(self, text, problems=()):
        self._floor = parse_policy(text, source="server", received=1_791_191_520.0, origin=ORIGIN)
        self._problems = [{"level": "warn", "text": problem} for problem in problems]
        self._unavailable = False

    def floor(self):
        if self._unavailable:
            raise PolicyUnavailable("not read")
        return self._floor

    def summary(self):
        return {"sources": [source_view(self._floor)], "problems": self._problems}


class Edition(FloorEdition):
    """The Community Edition with a server's policy and its route: what the Enterprise Edition gives a space."""

    def __init__(self, policy):
        super().__init__(None)
        self.policy = policy

    def floor(self, identity):
        return self.policy.floor()

    def dashboard_routes(self, dashboard):
        return policy_routes(dashboard, self.policy)


def read(tmp_path, policy, profile="balanced", unreadable=False):
    """What a signed-in person's dashboard is answered, and the answer without the sign-in."""
    config = Config(profile=profile, identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=Edition(policy))
    try:
        policy._unavailable = unreadable  # the space was built on a floor: the policy cannot be read any more
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            before = client.get("/_latet/api/v1/ee/policy").status_code
            code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
            answer = client.get("/_latet/api/v1/ee/policy")
            return before, answer
    finally:
        rt.close()


def test_the_dashboard_is_told_the_floor_its_source_and_what_it_raises(tmp_path):
    before, answer = read(tmp_path, Source("latet_rules: 1\nexceptions: company\nprofile: max\n"))
    view = answer.json()
    assert before == 401  # behind the dashboard's sign-in
    assert "profile: from max to balanced" in view["below"] and view["problems"] == []
    (floor,) = view["floors"]
    assert (floor["source"], floor["origin"], floor["exceptions"]) == ("server", ORIGIN, "company")
    assert floor["rules"] == {"latet_rules": 1, "profile": "max"}
    assert floor["brought"] == ["pii", "id_regions", "include_private_ips", "data", "images"]
    assert floor["notices"] == 0


def test_a_person_is_told_what_is_locked_and_never_a_value_of_the_companys(tmp_path):
    _, answer = read(tmp_path, Source(SECRET_POLICY, problems=["a notice"]))
    text = answer.text
    for value in (
        "Project Falcon",
        "Northwind Traders",
        "acme-staging",
        "ACME-",
        "CN-[0-9]",
        "falcon.acme.lan",
        "Falcon programme",
    ):
        assert value not in text, value
    (floor,) = answer.json()["floors"]
    rules = floor["rules"]
    assert rules["terms"] == 2 and rules["allow_values"] == 1 and rules["allow_patterns"] == 1
    assert rules["data_hosts"] == 1 and rules["recognizers"] == ["contract-number"] and rules["guidance"] is True


def test_what_is_below_the_policy_is_weighed_once_for_a_policy_and_a_persons_settings(tmp_path, monkeypatch):
    """M-B: the Rules page asks at each visit; the raise behind its answer runs once for a policy and the settings."""
    from latet.ee import policy as policy_module

    weighed = []
    real = policy_module.below_floor

    def counted(floor, config):
        weighed.append(floor.etag)
        return real(floor, config)

    monkeypatch.setattr(policy_module, "below_floor", counted)
    source = Source("latet_rules: 1\nprofile: max\n")
    config = Config(profile="balanced", identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=Edition(source))
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
            weighed.clear()
            answers = [client.get("/_latet/api/v1/ee/policy").json()["below"] for _ in range(3)]
            assert answers[0] == answers[2] and "profile: from max to balanced" in answers[0] and len(weighed) == 1
            source._floor = parse_policy("latet_rules: 1\nprofile: max\nner: auto\n", source="server", origin=ORIGIN)
            assert client.get("/_latet/api/v1/ee/policy").status_code == 200 and len(weighed) == 2  # a new policy
    finally:
        rt.close()


def test_a_floor_that_cannot_be_read_is_said_among_the_problems_and_locks_nothing(tmp_path):
    source = Source("latet_rules: 1\nprofile: max\n", problems=["the policy cannot be read now"])
    _, answer = read(tmp_path, source, unreadable=True)
    assert answer.status_code == 200
    assert answer.json() == {"floors": [], "below": [], "problems": ["the policy cannot be read now"]}
