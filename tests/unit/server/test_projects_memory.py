"""What a space's projects hold on a server, where every folder a client names is a project of its own: no more maskers
or copies of the settings than the projects remembered use, the dashboard's list building none for a project no
conversation used, and a weight that follows what they hold."""

from latet import projects as projects_module
from latet.config import Config, ProjectEntry, TermConfig
from latet.engine.vault import Vault
from latet.projects import Projects, project_id


def _settings(count: int, terms: int = 0) -> Config:
    customers = [TermConfig(value=f"Customer Company {i:04d}", type="ORG") for i in range(terms)]
    entries = [
        ProjectEntry(path=f"/home/emily/client-{i:05d}", terms=[TermConfig(value=f"Client Name {i:05d}")])
        for i in range(count)
    ]
    return Config(terms=customers, projects=entries)


def test_the_projects_forgotten_take_their_rules_with_them(monkeypatch):
    monkeypatch.setattr(projects_module, "PROJECTS_KEPT", 10)
    projects = Projects(_settings(30), Vault(b"k" * 32), files=False)
    for i in range(30):
        projects.resolve(f"/home/emily/client-{i:05d}")
    assert projects.count == 10
    assert len(projects._scopes) <= 2 * 10  # a masker for each project remembered (and a safe one), no more
    assert len(projects._shared) <= 10 + 1  # a copy of the settings for each, and the global rules'


def test_the_dashboards_list_builds_no_rules_for_a_project_no_conversation_used():
    projects = Projects(_settings(150), Vault(b"k" * 32), files=False)
    listed = projects.known()
    assert len(listed) == 150 and {p.name for p in listed} >= {"client-00000", "client-00149"}
    assert projects.count == 0 and len(projects._scopes) == 0 and len(projects._shared) <= 1  # nothing kept for them
    opened = projects.get(project_id("/home/emily/client-00007"))  # opened from the list: worked out now
    assert opened is not None and opened.scope is not None and projects.count == 1


def test_a_servers_projects_match_their_terms_within_a_bounded_budget_and_are_weighed_by_it():
    from latet.engine.detectors.terms import RE2_MEMORY, TermsDetector

    def budgets(projects: Projects) -> list[int]:
        return [
            detector.memory
            for project in projects._projects.values()
            if project.scope is not None
            for detector in project.scope.masker.detectors
            if isinstance(detector, TermsDetector)
        ]

    server, computer = (Projects(_settings(5, terms=500), Vault(b"k" * 32), files=files) for files in (False, True))
    for projects in (server, computer):
        for i in range(5):
            projects.resolve(f"/home/emily/client-{i:05d}")
    held = budgets(server)
    assert len(held) == 5 and all(memory <= RE2_MEMORY // 8 for memory in held)  # sized to its 501 terms
    assert server.weight >= sum(held)  # and weighed whole: what their automata may grow to as they work
    assert budgets(computer) == [RE2_MEMORY] * 5  # a computer's few projects as before


def test_a_projects_weight_follows_what_its_rules_hold(monkeypatch):
    monkeypatch.setattr(projects_module, "PROJECTS_KEPT", 10)
    light = Projects(_settings(10), Vault(b"k" * 32), files=False)
    heavy = Projects(_settings(10, terms=500), Vault(b"k" * 32), files=False)
    for projects in (light, heavy):
        for i in range(10):
            projects.resolve(f"/home/emily/client-{i:05d}")
    per_term = projects_module._MASKER_PER_TERM + projects_module._COPY_PER_OBJECT
    assert heavy.weight - light.weight >= 10 * 500 * per_term  # the shared terms are in each project's masker and copy
    before = heavy.weight
    heavy.use(_settings(10, terms=500), None)  # new global rules: every project worked out again at its next use
    assert heavy.weight >= before * 0.9  # until then each still holds its rules, and is weighed with them
    for i in range(10):
        heavy.resolve(f"/home/emily/client-{i:05d}")
    assert heavy.weight <= before * 1.2  # the rules before the change are neither held nor counted any more
    assert len(heavy._masker_weights) <= 10
