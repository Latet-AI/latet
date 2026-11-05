"""Every code path of the server that deletes vault entries moves the identity's count of forgets (``index.gone_owner``,
``vault._GONE``) in the same function (Task 8's fix round 2). A catch-up of an index of prints learns of a change from
the rows its count stamped, and a deleted entry's rows are gone with it: only that count tells the catch-up to empty
its cache (``PrintIndex.catch_up``). A path that deleted entries without it could serve a forgotten value from the
cache until the next build. The guard reads the server's sources: each statement that deletes from ``vault_entities``
(or from ``identities``, which cascades to it), written in a function or in a module constant a function names, must
sit in a function that executes ``_GONE`` with ``gone_owner``, or be one of the deletions listed with the reason no
cache outlives it."""

import ast
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "src" / "latet" / "server"
DELETES = re.compile(r"\b(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?(?:vault_entities|identities)\b", re.I)
# Deletions no cache outlives, with why.
WITHOUT_COUNT = {
    # The identity and its whole space go (its vault, its tokens: no request reaches its space again), and every
    # instance closes the space when told "removed"; the index and its cache go with the space.
    ("identities.py", "remove"),
}


def _strings(node: ast.AST) -> list[str]:
    """The string constants under ``node``, each alone and all of them joined in order (a statement built with ``+``)."""
    found = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return [*found, "".join(found)]


def _deleting(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions of a module that hold a deleting statement, or name a module constant that holds one."""
    constants: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(DELETES.search(text) for text in _strings(value)):
            constants |= {target.id for target in targets if isinstance(target, ast.Name)}
    return [
        function
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and (
            any(DELETES.search(text) for text in _strings(function))
            or any(isinstance(n, ast.Name) and n.id in constants for n in ast.walk(function))
        )
    ]


def _counts_its_forgets(function: ast.AST) -> bool:
    names = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    return {"_GONE", "gone_owner"} <= names


def _unaccounted(source: str, name: str) -> list[str]:
    return [
        function.name
        for function in _deleting(ast.parse(source))
        if (name, function.name) not in WITHOUT_COUNT and not _counts_its_forgets(function)
    ]


def test_every_deletion_of_vault_entries_moves_the_count_of_forgets():
    found: dict[str, list[str]] = {}
    unaccounted: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        source = path.read_text("utf-8")
        found[path.name] = [function.name for function in _deleting(ast.parse(source))]
        unaccounted += [f"{path.name}:{name}" for name in _unaccounted(source, path.name)]
    assert not unaccounted, f"deletes vault entries without moving the count of forgets: {unaccounted}"
    # Not vacuous: the deletions known today are found.
    assert {"forget_entity", "forget_values"} <= set(found["vault.py"])
    assert "remove" in found["identities.py"]


def test_the_guard_finds_a_deletion_without_the_count_in_a_constant_or_in_place():
    unguarded = """
_ALL_GONE = "DELETE FROM vault_entities WHERE identity_id = %s"

class Store:
    def wipe(self, conn):
        conn.execute(_ALL_GONE, (self.identity,))

    def one(self, conn, entity_id):
        conn.execute("DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (self.identity, entity_id))

    def guarded(self, conn, entity_id):
        conn.execute("DELETE " "FROM vault_entities WHERE id = %s", (entity_id,))
        conn.execute(_GONE, (gone_owner(self.identity),))

    def cascades(self, conn):
        conn.execute("TRUNCATE " + "identities")
"""
    assert sorted(_unaccounted(unguarded, "store.py")) == ["cascades", "one", "wipe"]
