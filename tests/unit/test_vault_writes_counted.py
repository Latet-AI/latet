"""Every code path that writes a vault's entries or their word rows stamps the rows of the entries it writes with the
change's count (``PgVaultStore._count``) in the same function (Task 8's fix round 5). Three mechanisms rest on it: an
index of prints catches up by the rows counted past its watermark (``PrintIndex.catch_up``), its cache drops what
those rows name, and a vault confirms its memory by those names alone (``StoredVault.resync(only=...)``). A writer
that moved no count, or stamped no row of the entry it wrote, would leave that entry stale in all three until the next
build or whole resync. The guard reads Latet's sources (``src/latet``, so that a package added later, as the Enterprise
edition's, is read too): each statement that inserts into or updates ``vault_entities`` or ``vault_words``, or deletes
from ``vault_words``, written in a function or in a module constant a function names, must sit in a function that calls
``_count``, or be one of the writes listed with the reason no reader's answer changes. Deletions of entries have a
guard of their own (``test_vault_forgets_counted``: the count of forgets)."""

import ast
import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "src" / "latet"
WRITES = re.compile(
    r"\b(?:(?:INSERT\s+INTO|UPDATE|MERGE\s+INTO|COPY)\s+(?:ONLY\s+)?vault_(?:entities|words)"
    r"|DELETE\s+FROM\s+(?:ONLY\s+)?vault_words)\b",
    re.IGNORECASE,
)
# Writes that stamp nothing, with why.
WITHOUT_COUNT = {
    # The stamp itself: the statement that moves the count writes it into the rows of the entries it counts.
    ("vault.py", "_count"),
    # A rule kept with a field's name in it, rewritten to the form every reader makes of it as it opens the entry
    # (``modules.kept``): no answer of any reader changes, so nothing is to be told.
    ("vault.py", "_cut_field_names"),
}


def _strings(node: ast.AST) -> list[str]:
    """The string constants under ``node``, each alone and all of them joined in order (a statement built with ``+``)."""
    found = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return [*found, "".join(found)]


def _writing(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions of a module that hold a writing statement, or name a module constant that holds one."""
    constants: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(WRITES.search(text) for text in _strings(value)):
            constants |= {target.id for target in targets if isinstance(target, ast.Name)}
    return [
        function
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and (
            any(WRITES.search(text) for text in _strings(function))
            or any(isinstance(n, ast.Name) and n.id in constants for n in ast.walk(function))
        )
    ]


def _stamps(function: ast.AST) -> bool:
    """Whether ``function`` calls ``_count`` (``self._count(...)``, ``store._count(...)``)."""
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            called = node.func
            if (isinstance(called, ast.Attribute) and called.attr == "_count") or (
                isinstance(called, ast.Name) and called.id == "_count"
            ):
                return True
    return False


def _unstamped(source: str, name: str) -> list[str]:
    return [
        function.name
        for function in _writing(ast.parse(source))
        if (name, function.name) not in WITHOUT_COUNT and not _stamps(function)
    ]


def test_every_write_of_a_vaults_rows_stamps_them_with_its_count():
    found: dict[str, list[str]] = {}
    unstamped: list[str] = []
    for path in sorted(SOURCES.rglob("*.py")):
        source = path.read_text("utf-8")
        writers = [function.name for function in _writing(ast.parse(source))]
        if writers:
            found[path.relative_to(SOURCES).as_posix()] = writers
        unstamped += [f"{path.relative_to(SOURCES).as_posix()}:{name}" for name in _unstamped(source, path.name)]
    assert not unstamped, f"writes a vault's rows without stamping them with the change's count: {unstamped}"
    # Not vacuous: the writers known today are found, and every one listed is still a writer.
    assert {"save_entity", "save_rule", "replace_fake", "_fill", "_fill_prints"} <= set(found["server/vault.py"])
    assert {name for module, name in WITHOUT_COUNT} <= set(found["server/vault.py"])


def test_the_guard_finds_a_write_without_the_stamp_in_a_constant_or_in_place():
    unstamped = """
_NEW: Final = (
    "INSERT INTO vault_entities (identity_id, id) VALUES (%s, %s)"
)
_RENAMED = "UPDATE " "vault_words SET word = %s WHERE identity_id = %s"

class Store:
    def keep(self, conn, e):
        conn.execute(_NEW, (self.identity, e.id))

    def rename(self, conn, word):
        conn.execute(_RENAMED, (word, self.identity))

    def edges_gone(self, conn, e):
        conn.execute("DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s", (self.identity, e.id))

    def lowercase(self, conn, e):
        conn.execute("update vault_entities set rule = %s where id = %s", (e.rule, e.id))

    def stamped(self, conn, e):
        conn.execute(_NEW, (self.identity, e.id))
        self._count(conn, [e.id])

    def read(self, conn):
        return conn.execute("SELECT id FROM vault_entities WHERE identity_id = %s", (self.identity,)).fetchall()

    def strings(self, conn, key):
        conn.execute("INSERT INTO vault_strings (identity_id, key) VALUES (%s, %s)", (self.identity, key))
"""
    assert sorted(_unstamped(unstamped, "store.py")) == ["edges_gone", "keep", "lowercase", "rename"]
