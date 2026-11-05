"""The owner-required AST guard against request-content machine branches."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "latet"

# Exact syntax of approved construction/auth/observation nodes. A changed body no
# longer matches; no whole file, function, or machine predicate is allowlisted.
_EXCEPTIONS = {
    "gateway/app.py": [
        "on_server.url if on_server is not None else None",
        "config.bridge_token if on_server is None else None",
        """if on_server is not None:
    await asyncio.to_thread(config.recorder.drain, 5)
else:
    config.recorder.drain(timeout=5)""",
        """if config.mediator is not None and on_server is not None and on_server.kind == 'key':
    from latet.bridge.apps import Apps
    apps_routes = Apps(config.mediator, on_server).routes()""",
        "[Route('/_latet/admin/shutdown', shutdown, methods=['POST'])] if on_server is None else []",
        "url_hosts(on_server.url) if on_server is not None else None",
        # A computer's local launch, its carrier taken and its refusals answered (P2-6a), as the app is built.
        "[Middleware(LocalLaunchIngress, owner=config.launch_keys)] if on_server is None else []",
        "[Middleware(LocalLaunchDispatch)] if on_server is None else []",
    ],
    "bridge/api.py": [
        "partial(mediator.verifier.wrote, reported=True) if on_server is None else _no_model_report",
        # As the bridge is built: a computer's launch keys (a server never borrows them), and how a plugin's session
        # is registered (a server's plugin bindings, a computer's local project).
        "launch_keys if on_server is None else None",
        "self._computer_session if on_server is None else self._server_session",
        """if self.on_server is not None:
    return request.scope.get('latet.identity') == self.on_server.identity""",
        "check_claude_route(data) if self.on_server is None else check_claude_route(data, request.scope.get('latet.space'), server_origin=self.on_server.url)",
        """if self.on_server is not None:
    return []""",
    ],
}


# What one machine alone builds for a request's launch (a computer's carrier check and keys, a server's ingress, its
# requests and controls): a request-path check of a value for one of these is a branch on the machine (Revision 9;
# root's ruling I-1 on P2-6a's final review). The request path asks one launch authority, injected at construction.
_MACHINE_TYPES = frozenset(
    {
        "LocalRequestLaunch",
        "ServerIngress",
        "ServerLaunchRequests",
        "ServerLaunchInput",
        "LocalLaunchKeys",
        "ProtectedLaunchControls",
        "LocalLaunchControls",
        "LocalLaunchAuthority",
    }
)


def machine_branches(source: str, path: str = "") -> list[int]:
    tree = ast.parse(source)
    allowed = set()
    for snippet in _EXCEPTIONS.get(path, []):
        node = ast.parse(snippet).body[0]
        if isinstance(node, ast.Expr):
            node = node.value
        allowed.add(ast.dump(node))
    # The machine itself, and a component one machine alone was built with: the server's launch controls (a computer's
    # bridge had none, so asking whether it has them asks which machine it runs on).
    aliases = {"on_server", "is_server", "server_mode", "_launch_controls"}

    def machine_type(node):
        """A type one machine alone hands the request path: checking a value for it chooses code by the machine."""
        return (isinstance(node, ast.Name) and node.id in _MACHINE_TYPES) or (
            isinstance(node, ast.Attribute) and node.attr in _MACHINE_TYPES
        )

    def type_check(part):
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id == "isinstance":
            return any(machine_type(item) for kind in part.args[1:2] for item in ast.walk(kind))
        if isinstance(part, ast.Compare):
            sides = [part.left, *part.comparators]
            typed = any(
                isinstance(side, ast.Call) and isinstance(side.func, ast.Name) and side.func.id == "type"
                for side in sides
            )
            return typed and any(machine_type(item) for side in sides for item in ast.walk(side))
        return False

    def presented(node):
        return any(isinstance(item, ast.Attribute) and item.attr == "presented" for item in ast.walk(node))

    def probe(part):
        """Duck typing of what one machine's ingress derived (P2-6a's re-review, R-3): ``hasattr`` or ``getattr`` on a
        launch's ``presented``, or reading an attribute of it. Only the one interface reads it (``RequestLaunch``);
        asking whether anything was presented (``launch.presented is not None``) reads nothing of it."""
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id in ("hasattr", "getattr"):
            return bool(part.args) and presented(part.args[0])
        return (
            isinstance(part, ast.Attribute) and isinstance(part.value, ast.Attribute) and part.value.attr == "presented"
        )

    def machine(node):
        return any(
            (isinstance(part, ast.Attribute) and part.attr in aliases)
            or (isinstance(part, ast.Name) and part.id in aliases)
            or type_check(part)
            or probe(part)
            or (
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and (
                    (
                        part.func.id == "getattr"
                        and len(part.args) > 1
                        and isinstance(part.args[1], ast.Constant)
                        and part.args[1].value in aliases
                    )
                    or (
                        part.func.id == "isinstance"
                        and any(isinstance(arg, ast.Name) and arg.id == "OnServer" for arg in part.args)
                    )
                )
            )
            for part in ast.walk(node)
        )

    # Propagate local/equivalent flags, including instance attributes. This is a
    # conservative guard: a newly introduced alias requires explicit inspection.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
                and ast.dump(node.value) not in allowed
                and machine(node.value)
            ):
                if isinstance(node.value, ast.Call) and not (
                    isinstance(node.value.func, ast.Name) and node.value.func.id in {"bool", "getattr", "isinstance"}
                ):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    parts = [target] if isinstance(target, (ast.Name, ast.Attribute)) else ast.walk(target)
                    for part in parts:
                        name = (
                            part.attr
                            if isinstance(part, ast.Attribute)
                            else part.id
                            if isinstance(part, ast.Name)
                            else None
                        )
                        if name and name not in aliases:
                            aliases.add(name)
                            changed = True
    found = set()

    def walk(node, exempt=False):
        exempt = exempt or ast.dump(node) in allowed
        if not exempt:
            condition = (
                node.test
                if isinstance(node, (ast.If, ast.IfExp, ast.While))
                else node
                if isinstance(node, ast.BoolOp)
                else None
            )
            if condition is not None and machine(condition):
                found.add(node.lineno)
            if isinstance(node, ast.Match):  # a class pattern naming a machine's launch type (R-3)
                for case in node.cases:
                    if any(
                        isinstance(item, ast.MatchClass) and any(machine_type(n) for n in ast.walk(item.cls))
                        for item in ast.walk(case.pattern)
                    ):
                        found.add(case.pattern.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, exempt)

    walk(tree)
    return sorted(found)


def test_no_machine_check_in_the_request_path():
    failures = {}
    paths = [*(ROOT / "gateway").glob("*.py"), *(ROOT / "protocols").glob("*.py"), ROOT / "bridge/api.py"]
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if found := machine_branches(path.read_text("utf-8"), relative):
            failures[relative] = found
    assert not failures, failures


@pytest.mark.parametrize(
    "gate",
    [
        "if self.on_server: return None",
        "flag = self.on_server\n    if flag: return None",
        "self.remote = getattr(self, 'on_server')\n    if self.remote: return None",
        "flag = isinstance(self.context, OnServer)\n    if flag: return None",
        "return self.on_server and body",
    ],
)
def test_guard_catches_scratch_reintroduced_machine_branch(gate):
    assert machine_branches("def prepare(self, body):\n    return body\n") == []
    assert machine_branches("def prepare(self, body):\n    " + gate + "\n    return body\n")


@pytest.mark.parametrize(
    "gate",
    [
        "if isinstance(launch, ServerIngress): return None",
        "if isinstance(launch, (LocalRequestLaunch, type(None))): return None",
        "if type(launch) is not LocalRequestLaunch: return None",
        "if type(launch) is ServerIngress and body: return None",
        "current = launch.current if isinstance(launch, LocalRequestLaunch) else None",
        "if type(launch) is not LocalRequestLaunch and type(launch) is not ServerIngress: return None",
        "if isinstance(launch, protected_launch.ServerIngress): return None",
        "if self._launch_controls is not None: return None",
        "controls = self._launch_controls\n    if controls is None: return None",
        # P2-6a's re-review, R-3: a class pattern and a probe of what a machine's ingress derived.
        "match launch.authority:\n        case ServerLaunchRequests():\n            return None",
        "match launch.presented:\n        case LocalRequestLaunch(status='verified'):\n            return None",
        "match launch.authority:\n        case latet.server.launch_requests.ServerLaunchRequests():\n            return None",
        "match launch:\n        case RequestLaunch(presented=LocalRequestLaunch()):\n            return None",
        "if hasattr(launch.presented, 'locator'): return None",
        "if getattr(launch.presented, 'owner', None) is not None: return None",
        "probe = getattr(launch.presented, 'verifier', None)\n    if probe: return None",
        "if launch.presented.status == 'verified': return None",
    ],
)
def test_guard_catches_a_check_of_a_machines_launch_type(gate):
    """Root's ruling I-1: an ``isinstance`` or ``type`` check of a launch value one machine alone builds, or asking
    whether the bridge has the server's launch controls, chooses code by the machine."""
    assert machine_branches("def prepare(self, launch, body):\n    return body\n") == []
    assert machine_branches("def prepare(self, launch, body):\n    " + gate + "\n    return body\n")


def test_one_launch_authority_type_is_not_a_machine_check():
    """The one launch value both machines hand the request path, and a check of it, is not a machine branch."""
    assert machine_branches("def prepare(self, launch):\n    if type(launch) is not RequestLaunch: return None\n") == []
    assert machine_branches("def prepare(self, launch):\n    if isinstance(launch, RequestLaunch): return None\n") == []
    # Asking whether a carrier was presented at all is the one interface's own question, not a probe of a machine's.
    assert machine_branches("def prepare(self, launch):\n    if launch.presented is not None: return None\n") == []
    assert machine_branches(
        "def prepare(self, launch):\n    match launch:\n        case RequestLaunch():\n            return None\n"
    ) == []  # fmt: skip


def test_construction_allowance_does_not_cover_changed_content_branch():
    clean = "on_server.url if on_server is not None else None"
    assert machine_branches(clean, "gateway/app.py") == []
    assert machine_branches("body if on_server is not None else None", "gateway/app.py")
    assert machine_branches("if on_server is not None:\n    mask(body)\n", "gateway/app.py")


def test_the_launch_authoritys_construction_choices_are_allowed_and_a_per_request_one_is_not():
    """What the launch-authority work chooses when the app and the bridge are built (R-114): the local launch's two
    middlewares, the computer's launch keys, and how a plugin session is registered. The same choice made in a request's
    handler is still a machine branch."""
    for path, built in [
        ("gateway/app.py", "[Middleware(LocalLaunchIngress, owner=config.launch_keys)] if on_server is None else []"),
        ("gateway/app.py", "[Middleware(LocalLaunchDispatch)] if on_server is None else []"),
        ("bridge/api.py", "self.launch_keys = launch_keys if on_server is None else None"),
        ("bridge/api.py", "self._session_of = self._computer_session if on_server is None else self._server_session"),
    ]:
        assert machine_branches(built, path) == [], built
    launch_keys_checked = (
        "def __init__(self, launch_keys, on_server):\n    self.launch_keys = launch_keys if on_server is None else None\n"
        "def owner(self):\n    if self.launch_keys is None:\n        raise LaunchRefused('launch-unavailable')\n"
    )
    assert machine_branches(launch_keys_checked, "bridge/api.py") == []  # an injected value's absence, as R-114's
    for per_request in [
        "def _session(self, data):\n    if self.on_server is None:\n        self.engine.attach(data)\n",
        "def owner(self, keys):\n    return keys if self.on_server is None else None\n",
        "def _session(self, data):\n    pick = self._computer_session if self.on_server is None else self._server_session\n",
    ]:
        assert machine_branches(per_request, "bridge/api.py"), per_request
    assert machine_branches("[Middleware(LocalLaunchIngress)] if on_server is None else []", "gateway/app.py")


def test_bridge_rehydration_has_one_content_path():
    tree = ast.parse((ROOT / "bridge/api.py").read_text("utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_rehydrate")
    assert machine_branches(ast.unparse(method)) == []
    mutant = "def _rehydrate(self, items):\n    if self.on_server is None:\n        self.report(items)\n"
    assert machine_branches(mutant) == [2]
