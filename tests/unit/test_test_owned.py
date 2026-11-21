"""What a test makes in the shared test services is named in a journal before it is made (``tests/owned.py``): after a
run stopped by force, the journal says which schemas, prefixes, roles, databases and Valkey users were that run's own,
and nothing in it is a password or a user of an address."""

import ast
import json
from pathlib import Path

import pytest

from ..owned import OWNED, owned

TESTS = Path(__file__).resolve().parents[1]
# The address's secrets, which the journal never holds: a user and a password of their own, so that each is found if
# written, and a certificate's path that names a folder of the computer.
DATABASE = (
    "postgresql://owner-u1:pw-0d1ce5@127.0.0.1:55432/latet?sslmode=verify-full&sslrootcert=C%3A%5Ccerts%5Croot.crt"
)
VALKEY = "redis://valkey-u2:pw-5ecre7@127.0.0.1:56379/0"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_a_name_is_written_down_with_its_place_and_nothing_of_the_address_but_its_place(tmp_path, monkeypatch):
    journal = tmp_path / "worker-1.owned.jsonl"
    monkeypatch.setenv(OWNED, str(journal))
    assert owned("latet_t_0123456789", "schema", DATABASE) == "latet_t_0123456789"
    assert owned("latet_t_9876543210:", "prefix", VALKEY) == "latet_t_9876543210:"
    first, second = _records(journal)
    assert (first["name"], first["kind"], first["at"]) == ("latet_t_0123456789", "schema", "127.0.0.1:55432/latet")
    assert (second["name"], second["kind"], second["at"]) == ("latet_t_9876543210:", "prefix", "127.0.0.1:56379/0")
    assert "test_a_name_is_written_down" in first["test"]  # which test made it, as pytest says
    text = journal.read_text("utf-8")
    for secret in ("owner-u1", "pw-0d1ce5", "valkey-u2", "pw-5ecre7", "certs", "sslrootcert"):
        assert secret not in text


def test_a_host_that_cannot_be_read_is_not_written(tmp_path, monkeypatch):
    """A raw @ in a password ends the user's part at the first @: what libpq takes for the host holds the password's
    tail, and is not written."""
    journal = tmp_path / "owned.jsonl"
    monkeypatch.setenv(OWNED, str(journal))
    owned("latet_t_0123456789", "schema", "postgresql://u:pass@word-tail@127.0.0.1:55432/latet")
    [record] = _records(journal)
    assert record["at"] == "unreadable" and "word-tail" not in journal.read_text("utf-8")


def test_without_a_journal_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv(OWNED, raising=False)
    monkeypatch.chdir(tmp_path)
    assert owned("latet_t_0123456789", "schema", DATABASE) == "latet_t_0123456789"
    assert list(tmp_path.iterdir()) == []


def test_a_journal_that_cannot_be_written_stops_the_test_before_anything_is_made(tmp_path, monkeypatch):
    monkeypatch.setenv(OWNED, str(tmp_path / "missing" / "owned.jsonl"))
    with pytest.raises(FileNotFoundError):
        owned("latet_t_0123456789", "schema", DATABASE)


# Each shape the tests make today, as the places that make them make it.
SHAPED = [
    ("latet_t_0123456789", "schema"),  # the server's `schema` fixture, the TLS schema
    ("t_0123456789ab", "schema"),  # the Enterprise tests' instances
    ("latet_t_0123456789:", "prefix"),  # the server's `prefix` fixture
    ("t01234567:", "prefix"),  # the Enterprise tests' instances
    ("latet_r_01234567", "role"),  # `role_url`, test_db_start
    ("latet_t_01234567", "role"),  # test_db_start's password with every mark of an address
    ("latet_t_0123456789", "database"),  # test_db_start's encodings
    ("latet_t_0123456789abcdef0123456789abcdef", "valkey-user"),  # test_bus's documented ACL
]

# A cleanup removes what exists under a journaled name: a name that is not random, or short, would take others' too.
REFUSED = [
    ("", "schema"),
    ("latet_t_", "schema"),
    ("latet_t_keyfuzz_x", "schema"),
    ("latet_t_keyfuzz_pi0102_99700a327719_0", "schema"),  # another campaign's, on the same PostgreSQL
    ("latet_t_012345678", "schema"),  # one digit short
    ("latet_t_0123456789a", "schema"),  # one digit long
    ("latet_t_ABCDEF0123", "schema"),
    ("latet_t_0123456789\n", "schema"),
    ("latet_t_0123456789:", "schema"),
    ("t_0123456789ab_x", "schema"),
    ("latet_t_0123456789", "prefix"),  # a prefix ends at its colon
    ("latet_t_0123456789:*", "prefix"),
    ("t0123456:", "prefix"),
    ("latet_r_0123456789", "role"),
    ("latet_t_0123456789", "valkey-user"),
    ("latet_t_0123456789", "table"),  # no kind the tests make
]


@pytest.mark.parametrize(("name", "kind"), SHAPED)
def test_each_shape_the_tests_make_is_written_down(tmp_path, monkeypatch, name, kind):
    journal = tmp_path / "owned.jsonl"
    monkeypatch.setenv(OWNED, str(journal))
    assert owned(name, kind, DATABASE) == name
    assert [(r["name"], r["kind"]) for r in _records(journal)] == [(name, kind)]


@pytest.mark.parametrize(("name", "kind"), REFUSED)
def test_a_name_of_another_shape_is_refused_before_anything_is_written(tmp_path, monkeypatch, name, kind):
    journal = tmp_path / "owned.jsonl"
    monkeypatch.setenv(OWNED, str(journal))
    with pytest.raises(ValueError, match="shape"):
        owned(name, kind, DATABASE)
    assert not journal.exists()
    monkeypatch.delenv(OWNED)
    with pytest.raises(ValueError, match="shape"):  # refused without a journal too: no test makes such a name
        owned(name, kind, DATABASE)


def _constant(node: ast.AST) -> str | None:
    """The text of an expression made of string constants alone (``"latet_" + "t_"``), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant(node.left), _constant(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _head(node: ast.AST) -> str:
    """The constant text an expression that builds a string begins with: ``"latet_t_"`` for ``"latet_" + "t_" + x``,
    ``f"latet_t_{x}"``, ``"latet_t_%s" % x`` and ``"latet_t_{}".format(x)``."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant(node.left)
        return _head(node.left) if left is None else left + _head(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _constant(node.left) or ""
    if isinstance(node, ast.JoinedStr):
        head = ""
        for part in node.values:
            if not isinstance(part, ast.Constant):
                break
            head += str(part.value)
        return head
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _constant(node.func.value) or ""
    return _constant(node) or ""


def _names_made(tree: ast.AST) -> list[ast.AST]:
    """The expressions that make a name for the shared services, each whole: a string built from a constant beginning
    with ``latet_t_`` or ``latet_r_`` and something that is not constant (by ``+``, an f-string, ``%`` or
    ``.format``), and the f-strings ``f"t_{uuid...}"`` and ``f"t{uuid...}:"`` of the Enterprise tests' instances."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        built = isinstance(node, ast.BinOp | ast.JoinedStr | ast.Call) and _constant(node) is None
        prefixed = built and _head(node).startswith(("latet_t_", "latet_r_"))
        if isinstance(node, ast.JoinedStr) and not any(isinstance(part, ast.FormattedValue) for part in node.values):
            prefixed = False  # an f-string with nothing to fill is a constant
        formatted = (
            isinstance(node, ast.JoinedStr)
            and bool(node.values)
            and isinstance(node.values[0], ast.Constant)
            and node.values[0].value in ("t_", "t")
            and any(isinstance(part, ast.FormattedValue) and "uuid" in ast.unparse(part.value) for part in node.values)
        )
        if prefixed or formatted:
            found.append(node)
    inner = {id(part) for node in found for part in ast.walk(node) if part is not node}
    return sorted((node for node in found if id(node) not in inner), key=lambda node: (node.lineno, node.col_offset))


def test_every_name_a_test_makes_for_the_shared_services_is_written_down():
    """Each such name is made inside ``owned(...)``, so that a run's journal misses none of them."""
    unwritten = []
    for path in sorted(TESTS.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text("utf-8"), str(path))
        inside_owned = {
            id(inner)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "owned"
            for argument in node.args[:1]
            for inner in ast.walk(argument)
        }
        unwritten += [
            f"{path.relative_to(TESTS).as_posix()}:{node.lineno}"
            for node in _names_made(tree)
            if id(node) not in inside_owned
        ]
    assert unwritten == [], unwritten


def test_the_guard_sees_the_names_it_is_meant_to_see():
    """The guard's control: each way a name is made today is found when not written down, and found written down."""
    made = ast.parse(
        "a = 'latet_t_' + secrets.token_hex(5)\n"
        "b = 'latet_r_' + secrets.token_hex(4)\n"
        "c = f't_{uuid.uuid4().hex[:12]}'\n"
        "d = f't{uuid.uuid4().hex[:8]}:'\n"
        "e = 'latet_t_' + secrets.token_hex(5) + ':'\n"
        "f = f'latet_t_{secrets.token_hex(5)}'\n"
        "g = f'latet_r_{secrets.token_hex(4)}:'\n"
        "h = 'latet_t_%s' % secrets.token_hex(5)\n"
        "i = 'latet_t_{}'.format(secrets.token_hex(5))\n"
        "j = 'latet_' + 't_' + secrets.token_hex(5)\n"
    )
    assert [node.lineno for node in _names_made(made)] == list(range(1, 11))  # each once, the whole expression
    ignored = ast.parse(
        "a = tmp_path / 'latet_t_slow_migrations'\n"
        "b = f't_{name}'\n"
        "c = 'latet_' + x\n"
        "d = 'latet_' + 't_' + 'unit'\n"  # a fixed name: nothing random to write down
        "e = f'latet_x_{x}'\n"
        "f = 'SELECT 1 WHERE n LIKE %s' % x\n"
        "g = 'latet_t_unit'\n"
    )
    assert _names_made(ignored) == []
