"""A pattern from a person's settings, or one made of data (a vault's values, a recognizer's words), never reaches the
process's standard error, which is a server's log: RE2 writes a pattern it cannot compile there itself, past the log's
redaction and escaping, unless it is compiled with its quiet options (``quiet_options``). So every compile of one has
them; an allow pattern that does not compile is refused where the settings are read, in words that name the setting and
never the pattern, and left out where settings that hold one anyway are applied (the safe side: more is masked), with a
notice that names the setting and never the pattern."""

import ast
import traceback
from pathlib import Path

import pytest
import re2
import yaml
from pydantic import ValidationError

from latet.config import Config, IdentityConfig, ProjectEntry, load_config
from latet.engine.build import build
from latet.engine.detectors import terms
from latet.engine.vault import Vault
from latet.projects import Projects
from latet.runtime import make_runtime
from latet.transfer import read_rules

from ..conftest import ROOT_KEY

SRC = Path(__file__).parents[2] / "src" / "latet"
# An allow pattern missing its ")", made of what it lets through: an address and a client's name.
BROKEN = r"(emily\.carter@acme\.com|Falcon Holdings"
HELD = ("emily", "Falcon")


def _nothing_held(*texts):
    return not any(held in text for held in HELD for text in texts)


@pytest.mark.parametrize(
    ("data", "where"),
    [
        ({"allow_patterns": ["ok-[0-9]+", BROKEN]}, ("allow_patterns", 1)),
        ({"projects": [{"path": "/p", "allow_patterns": [BROKEN]}]}, ("projects", 0, "allow_patterns", 0)),
    ],
)
def test_settings_with_an_allow_pattern_that_does_not_compile_are_refused_by_its_place(data, where, capfd):
    with pytest.raises(ValidationError) as refused:
        Config.model_validate(data)
    [error] = refused.value.errors(include_url=False, include_context=False, include_input=False)
    assert (error["loc"], error["msg"]) == (where, "Value error, not a valid RE2 pattern")
    assert capfd.readouterr().err == ""


def test_a_build_leaves_out_an_allow_pattern_that_does_not_compile_and_says_which_entry(capfd, caplog):
    config = Config(identity=IdentityConfig(enabled=False))
    config.allow_patterns = ["ok-[0-9]+", BROKEN]  # settings that hold one anyway: assigned, never validated
    allow = build(config, Vault(ROOT_KEY)).masker.allow
    assert allow.allows("ok-42") and not allow.allows("Falcon Holdings")  # the rest still applies; nothing more passes
    said = [r.getMessage() for r in caplog.records if r.name == "latet.engine.masker"]
    assert said == ["allow_patterns: entry 2 is not a valid RE2 pattern and is left out"]
    assert capfd.readouterr().err == "" and _nothing_held(caplog.text)


def test_a_projects_allow_pattern_that_does_not_compile_is_left_out_with_a_notice_by_its_place(tmp_path, capfd):
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    entry = ProjectEntry(path=str(root))
    entry.allow_patterns = ["ok-[0-9]+", BROKEN]  # assigned, never validated
    project = Projects(Config(projects=[entry]), Vault(ROOT_KEY)).resolve(str(root))
    [notice] = [n for n in project.notices if n.key == "allow_patterns"]
    assert notice.message == "entry 2 is not a valid RE2 pattern: left out"
    assert project.config.allow_patterns == ["ok-[0-9]+"]
    assert capfd.readouterr().err == ""


def test_an_imported_allow_pattern_that_does_not_compile_is_left_out_with_a_notice_by_its_place(capfd):
    settings, notices = read_rules(yaml.safe_dump({"allow_patterns": ["ok-[0-9]+", BROKEN]}))
    assert settings.allow_patterns == ["ok-[0-9]+"]
    assert notices == ["allow_patterns: entry 2 is not a valid RE2 pattern, left out"]
    assert capfd.readouterr().err == ""


def _config_file(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    return path


def _a_hand_edited_file(tmp_path):
    """A config.yaml edited by hand: an allow pattern that does not compile, globally and in a project's entry."""
    return _config_file(
        tmp_path,
        {
            "terms": [{"value": "Mercury Labs", "type": "CLIENT"}],
            "allow_patterns": ["ok-[0-9]+", BROKEN],
            "projects": [{"path": str(tmp_path / "shop"), "allow_patterns": [BROKEN, "fine-[a-z]+"]}],
        },
    )


LEFT_OUT = [
    "config.yaml: allow_patterns entry 2 is not a valid RE2 pattern and is left out",
    "config.yaml: projects entry 1, allow_patterns entry 1 is not a valid RE2 pattern and is left out",
]


def test_a_config_yaml_holding_an_allow_pattern_that_does_not_compile_loads_with_it_left_out(tmp_path, caplog, capfd):
    """Left out where the file is read (more is masked), with a notice that names its place; the rest applies."""
    config = load_config(_a_hand_edited_file(tmp_path))
    assert [t.value for t in config.terms] == ["Mercury Labs"] and config.allow_patterns == ["ok-[0-9]+"]
    assert config.projects[0].allow_patterns == ["fine-[a-z]+"]
    assert [r.getMessage() for r in caplog.records if r.name == "latet.config"] == LEFT_OUT
    assert capfd.readouterr().err == "" and _nothing_held(caplog.text)


def test_the_daemon_starts_with_a_config_yaml_holding_an_allow_pattern_that_does_not_compile(tmp_path):
    rt = make_runtime(data=tmp_path / "data", config_file=_a_hand_edited_file(tmp_path), root_key=ROOT_KEY)
    try:
        assert rt.config.allow_patterns == ["ok-[0-9]+"] and rt.built.masker.allow.allows("ok-42")
    finally:
        rt.close()


# Settings refused, each holding what a person's settings hold: an address, a client's name.
REFUSED = {
    "retention_days": "emily.carter@acme.com",
    "profile": "Falcon Holdings",
    "id_regions": ["IT", "Falcon Holdings"],
    "policies": {"emily.carter@acme.com": "Falcon Holdings"},
    "llm_review": {"emily.carter@acme.com": True},
    "upstreams": [
        {"name": "falcon-holdings", "url": "https://gw.falcon-holdings.example"},
        {"name": "falcon-holdings", "url": "http://emily.example"},
    ],
}


def test_settings_that_are_not_valid_are_refused_by_place_never_quoting_what_they_hold(tmp_path):
    path = _config_file(tmp_path, REFUSED)
    with pytest.raises(ValueError, match="is not valid settings") as refused:
        load_config(path)
    said = str(refused.value)
    for place in ("retention_days", "profile", "id_regions", "policies.*", "llm_review.*", "upstreams.1.url"):
        assert place in said
    shown = "".join(traceback.format_exception(refused.value))  # as a traceback shows it: the chained errors too
    assert not any(held.lower() in shown.lower() for held in HELD)


def test_two_destinations_of_one_name_are_refused_by_their_places():
    twice = [{"name": "falcon-holdings", "url": f"https://gw{n}.acme.example"} for n in (1, 2)]
    with pytest.raises(ValidationError) as refused:
        Config.model_validate({"upstreams": twice})
    [error] = refused.value.errors(include_url=False, include_context=False, include_input=False)
    assert error["msg"] == "Value error, entries 1 and 2 have the same name: each destination needs its own"
    assert "falcon" not in str(refused.value)


def test_a_settings_models_own_error_quotes_no_value_it_was_given_either():
    """What prints a refusal of the settings anywhere (a command's error, a traceback) shows no value given: pydantic
    still names a key the settings were given where it is wrong (``policies.<key>``), which a load names ``*``."""
    values = {key: value for key, value in REFUSED.items() if key not in ("policies", "llm_review")}
    values["policies"] = {"EMAIL_ADDRESS": "Falcon Holdings"}
    for model, data in ((Config, values), (ProjectEntry, {"path": "/p", "id_regions": ["Falcon Holdings"]})):
        with pytest.raises(ValidationError) as refused:
            model.model_validate(data)
        assert not any(held.lower() in str(refused.value).lower() for held in HELD)


def test_an_alternation_of_values_too_large_for_re2_writes_none_of_them(monkeypatch, capfd):
    monkeypatch.setattr(terms, "RE2_MEMORY", 1 << 10)  # the vault's values outgrow what RE2 may use
    values = [terms.Term(f"{HELD[0]}.carter{i}@acme.com") for i in range(200)]
    with pytest.raises(re2.error):  # as before: the vault's values are never left out
        terms._Alternation(values, False, True)
    assert capfd.readouterr().err == ""


# --- every compile, read ---------------------------------------------------------------------------------------------

# RE2's calls that compile a pattern: the module's functions, and what compile() makes.
_COMPILES = frozenset(
    {"compile", "_Regexp", "search", "match", "fullmatch", "finditer", "findall", "split", "sub", "subn"}
)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def _literal(node: ast.AST | None, constants: set[str]) -> bool:
    """Whether ``node`` is a pattern written in the code: a string, strings joined with ``+``, or a module's name for
    one."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str | bytes)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal(node.left, constants) and _literal(node.right, constants)
    return isinstance(node, ast.Name) and node.id in constants


def _constants(tree: ast.Module) -> set[str]:
    """The names a module sets, at its level, to a pattern written in the code."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and _literal(node.value, names):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and _literal(node.value, names):
            names.add(node.target.id)
    return names


def _quiet(node: ast.AST, tree: ast.Module) -> bool:
    """Whether ``node`` is RE2's quiet options: a call of ``quiet_options``, or a name set to one."""
    if isinstance(node, ast.Call):
        return (_dotted(node.func) or "").rpartition(".")[2] == "quiet_options"
    if isinstance(node, ast.Name):
        return any(
            isinstance(assigned, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == node.id for target in assigned.targets)
            and _quiet(assigned.value, tree)
            for assigned in ast.walk(tree)
        )
    return False


def loud(source: str) -> list[str]:
    """Each compile of an RE2 pattern in ``source`` that is neither written in the code nor given RE2's quiet options,
    and each set of RE2's options made otherwise than by ``quiet_options``: ``<line>: <call>``."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    constants = _constants(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _dotted(node.func) or ""
        given = [*node.args[1:], *(keyword.value for keyword in node.keywords if keyword.arg == "options")]
        if called == "re2.Options":
            inside = node
            while inside in parents and not isinstance(inside, ast.FunctionDef):
                inside = parents[inside]
            if not (isinstance(inside, ast.FunctionDef) and inside.name == "quiet_options"):
                found.append(f"{node.lineno}: {called}")
        elif called.startswith("re2.Set."):  # its patterns are added later, each compiled with these options
            if not any(_quiet(options, tree) for options in [*node.args, *(k.value for k in node.keywords)]):
                found.append(f"{node.lineno}: {called}")
        elif called.removeprefix("re2.") in _COMPILES and called.startswith("re2."):
            pattern = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
            if not (_literal(pattern, constants) or any(_quiet(options, tree) for options in given)):
                found.append(f"{node.lineno}: {called}")
    return found


def test_every_pattern_from_settings_or_data_is_compiled_with_re2s_quiet_options():
    found = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in loud(path.read_text("utf-8"))
    ]
    assert found == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import re2\nre2.compile(pattern)\n", ["2: re2.compile"]),
        ("import re2\nre2.compile(pattern, re2.Options())\n", ["2: re2.compile", "2: re2.Options"]),
        ("import re2\nre2.search(pattern, text)\n", ["2: re2.search"]),
        ("import re2\nre2._Regexp(pattern.encode(), options)\n", ["2: re2._Regexp"]),
        ("import re2\nre2.Set.SearchSet(re2.Options())\n", ["2: re2.Set.SearchSet", "2: re2.Options"]),
        ("import re2\nre2.compile('a|b')\nre2.compile(r'a' + 'b')\n", []),
        ("import re2\nNAME = r'^[a-z]+$'\ndef f(v):\n    return re2.fullmatch(NAME, v)\n", []),
        ("import re2\nre2.compile(pattern, quiet_options())\n", []),
        ("import re2\ndef f(p):\n    options = quiet_options(max_mem=1)\n    return re2._Regexp(p, options)\n", []),
        ("import re2\nre2.Set.SearchSet(quiet_options())\n", []),
        ("import re2\ndef quiet_options():\n    options = re2.Options()\n    return options\n", []),
    ],
)
def test_the_reading_finds_a_loud_compile(source, found):
    assert sorted(loud(source)) == sorted(found)
