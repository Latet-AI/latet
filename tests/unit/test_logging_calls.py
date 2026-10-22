"""No log line of Latet's holds an error's words or a traceback's text, which can quote what was being read (a text, a
value, a folder, a driver's DETAIL): every logging call of src/latet is read, and one that is given an exception, its
words, its repr, an f-string of it or anything its function made of them first (a variable, an attribute), or a
traceback's text, fails, but for a few of Latet's own errors whose words are fixed. A call may ask for the exception's
traceback (``log.exception``, ``exc_info``): a server's log and a computer's daemon.log show it by its types and frames
alone (``latet.server.logs``, ``frames_only``). Nothing outside the command line's own modules writes past the log
(``print``, the standard streams written to, a traceback printed). What a server's log shows of a library's records and
of an exception that nobody caught is ``latet.server.logs``' own (tests/unit/server/test_logs.py), and its formatter
writes an error that a line of Latet's is given by its type, as a net for what this reading cannot follow.

Nor does a line hold a person or a value by its name (the fix wave's S8, area 3 M8): an argument that is an attribute,
or a name, that holds one (a value's original or its fake, a person's email, user name, full name or host name, an
identity's name, a folder) fails; and the fields a call hands the formatter beside its text (``extra``, which a server's
JSON lines write) are read too: only those of a request's line (``_EXTRA_KEPT``), each read as an argument is."""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "latet"
_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})
_OPTIONS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})  # what a logging call takes beside its text
# The fields a call may hand the formatter (``extra``): a request's line's (``latet.server.logs.RequestLog``), which a
# server's JSON lines write, and the mark that says the line was made so.
_EXTRA_KEPT = frozenset({"method", "path", "status", "ms", "latet_request"})
# What holds a person or a value by its name, as an attribute or a name given to a line: a value's original or its
# fake, a person's email, user names, full name, host names or external id, a folder.
_PERSONAL = frozenset(
    {
        "original", "surrogate", "email", "emails", "full_name", "username", "usernames", "hostname", "hostnames",
        "external_id", "folder", "cwd",
    }
)  # fmt: skip
# The name a logger goes by wherever it comes from (a parameter, an attribute, another module): log, _log, logger,
# access_log, uvicorn_logger.
_LOGGER = re.compile(r"(?:^_*|_)(?:log|logger)$")
# A parameter that holds an error by its name, when no annotation says what it holds.
_ERROR_NAMES = frozenset({"e", "ex", "exc", "err", "error", "exception"})
_ERROR_TYPE = re.compile(r"(?:Error|Exception|ExceptionGroup)$")
# What gives the error being handled, its words or its traceback, wherever it is called.
_TRACEBACK = re.compile(
    r"traceback\.\w+|format_exc|format_exception(?:_only)?|format_tb|print_exc|print_exception|sys\.(?:exc_info|exception)"
)
# What writes a traceback, words included, to the standard error, which is a server's log.
_PRINTED = re.compile(r"(?:traceback\.)?(?:print_exc|print_exception|print_last)")
# What writes past the log, outside the command line's own modules: a print, the standard streams written to.
_WRITTEN = re.compile(r"print|sys\.(?:__)?(?:stdout|stderr)(?:__)?\.write(?:lines)?")
# Latet's own errors whose words are fixed and name no value (None: they may be logged; a name: that attribute alone).
KEPT: dict[str, str | None] = {
    "MaskingError": None,  # E0a: its words name no value
    "DetectorSkipped": None,  # how long a text was, or that the model is loading or could not be loaded (by its type)
    "ReviewError": None,  # an HTTP status, an error's type, a certificate refused
    "AwsError": "code",  # its code; its words can name a profile and quote the AWS CLI
}
# The functions that hand an exception to a log on purpose, by their module: the hooks of a server's log, whose
# formatter shows it by its types and frames alone. Every other line of their module is read.
EXEMPT: dict[Path, frozenset[str]] = {
    Path("server", "logs.py"): frozenset({"_uncaught", "_thread_ended", "_unraisable"})
}
_Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _dotted(node: ast.AST) -> str | None:
    """``log``, ``self.error``, ``traceback.format_exc``: the name an expression goes by, when it is one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def _gets_a_logger(node: ast.AST) -> bool:
    """Whether ``node`` is a call of ``getLogger``, however it was imported."""
    return isinstance(node, ast.Call) and (_dotted(node.func) or "").rpartition(".")[2] == "getLogger"


def _pairs(node: ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr) -> list[tuple[ast.AST, ast.AST]]:
    """What ``node`` sets, each with the value it sets it to (a tuple set from a tuple of the same length, element by
    element)."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    pairs: list[tuple[ast.AST, ast.AST]] = []
    for target in targets:
        value = node.value
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
            pairs += zip(target.elts, value.elts, strict=True)
        elif isinstance(target, ast.Tuple):
            pairs += [(element, value) for element in target.elts]
        elif value is not None:
            pairs.append((target, value))
    return pairs


def _annotated_as_a_logger(argument: ast.arg) -> bool:
    return argument.annotation is not None and (_dotted(argument.annotation) or "").endswith("Logger")


def _loggers(tree: ast.Module) -> set[str]:
    """The names and attributes a module's loggers go by beside the usual ones (``_LOGGER``): ``logging`` itself,
    whatever it sets to ``getLogger(...)`` (a name, an annotated name, an attribute of ``self``, a tuple's element),
    and its parameters annotated as a ``Logger``."""
    names = {"logging"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            names |= {_dotted(target) or "" for target, value in _pairs(node) if _gets_a_logger(value)}
        if isinstance(node, ast.arg) and _annotated_as_a_logger(node):
            names.add(node.arg)
    return names - {""}


def _is_logger(receiver: ast.AST, loggers: set[str]) -> bool:
    """Whether a call on ``receiver`` is a call on a logger."""
    if _gets_a_logger(receiver):
        return True
    name = _dotted(receiver)
    return name is not None and (name in loggers or bool(_LOGGER.search(name.rpartition(".")[2])))


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The function ``node`` is in, or None for a module's own lines."""
    while node in parents:
        node = parents[node]
        if isinstance(node, _Scope):
            return node
    return None


def _caught(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> dict[str, str]:
    """The names the ``except`` clauses around ``node`` bind an exception to, each with what of it may be logged
    (``KEPT``; "*" all of it, "" nothing)."""
    names: dict[str, str] = {}
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.ExceptHandler) and node.name:
            kind = node.type.id if isinstance(node.type, ast.Name) else None
            kept = KEPT.get(kind, "") if kind is not None else ""
            names.setdefault(node.name, "*" if kept is None else kept)
    return names


def _parameter(argument: ast.arg) -> str | None:
    """What of a parameter may be logged when it holds an error (``KEPT``'s way), by its annotation or, with none, by
    its name; None when it holds none."""
    if argument.annotation is None:
        return "" if argument.arg in _ERROR_NAMES else None
    named = [_dotted(node) or "" for node in ast.walk(argument.annotation)]
    named += [node.value for node in ast.walk(argument.annotation) if isinstance(node, ast.Constant)]
    kinds = {name.rpartition(".")[2] for name in named if isinstance(name, str) and _ERROR_TYPE.search(name)}
    if not kinds:
        return None
    kept = {KEPT.get(kind, "") for kind in kinds}
    if len(kept) > 1:  # one of several types: what all of them allow, which is nothing
        return ""
    (allowed,) = kept
    return "*" if allowed is None else allowed


def _set(target: ast.AST) -> list[str]:
    """The names and attributes an assignment to ``target`` sets: a tuple's each, a container's own name."""
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _set(element)]
    if isinstance(target, ast.Starred | ast.Subscript):
        return _set(target.value)
    name = _dotted(target)
    return [] if name is None else [name]


def _sources(source: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """Each place a value is set from another in ``source``: an assignment, a walrus, a ``for`` over it."""
    pairs: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(source):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            pairs += _pairs(node)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            pairs.append((node.target, node.iter))
    return pairs


# Where a name or an attribute holds an error: the function that set it (None: a module's own lines), from which line,
# and what of the error may be logged.
_Taint = dict[str, set[tuple[ast.AST | None, int, str]]]


def _at(taint: _Taint, scope: ast.AST | None, line: int) -> dict[str, str]:
    """The names and attributes that hold an error at ``line`` of ``scope``: those its function set on that line or
    before, and an attribute of ``self`` that any function of the module set."""
    errors: dict[str, str] = {}
    for name, places in taint.items():
        for where, since, allowed in places:
            if (where is scope and since <= line) or (where is not scope and name.partition(".")[0] in ("self", "cls")):
                errors.setdefault(name, allowed)
    return errors


def _tainted(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> _Taint:
    """Where each name and attribute of a module holds an error: a function's parameters that are errors, what it sets
    from a task's ``exception()``, and, to a fixed point, whatever it sets from an error, from the error of an
    ``except`` around, or from anything made of them, from the line it does so on."""
    taint: _Taint = {}
    for node in ast.walk(tree):
        if isinstance(node, _Scope):
            arguments = node.args
            every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
            for argument in every:
                if argument is not None and (allowed := _parameter(argument)) is not None:
                    taint.setdefault(argument.arg, set()).add((node, node.lineno, allowed))
    pairs = [(target, value, _scope(target, parents)) for target, value in _sources(tree)]
    changed = True
    while changed:
        changed = False
        for target, value, scope in pairs:
            errors = {**_at(taint, scope, target.lineno), **_caught(target, parents)}
            from_a_task = isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            if not (_holds(value, errors) or (from_a_task and value.func.attr == "exception")):
                continue
            allowed = errors.get(_dotted(value) or "", "")  # an error copied as it is keeps what of it may be logged
            for name in _set(target):
                place = (scope, target.lineno, allowed)
                if place not in taint.setdefault(name, set()):
                    taint[name].add(place)
                    changed = True
    return taint


def _holds(node: ast.AST, errors: dict[str, str]) -> bool:
    """Whether ``node`` holds one of ``errors`` itself, or anything made from it, or a traceback's text, but its type
    (``type(exc)``), its description (``described(exc)``) and what ``KEPT`` allows."""
    if isinstance(node, ast.Call):
        called = _dotted(node.func) or ""
        if called.rpartition(".")[2] in ("type", "described"):
            return False
        if _TRACEBACK.fullmatch(called):
            return True
    name = _dotted(node)
    if name is not None and name in errors:
        return errors[name] != "*"
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value)
        allowed = errors.get(owner) if owner is not None else None
        if allowed and node.attr == allowed:
            return False
    return any(_holds(child, errors) for child in ast.iter_child_nodes(node))


def _personal(node: ast.AST) -> bool:
    """Whether ``node`` holds a person or a value by its name, anywhere in it: an attribute or a name of ``_PERSONAL``,
    or an identity's name (``identity.name``, ``space.identity.name``)."""
    for part in ast.walk(node):
        if isinstance(part, ast.Attribute):
            owner = (_dotted(part.value) or "").rpartition(".")[2]
            if part.attr in _PERSONAL or (part.attr == "name" and owner.endswith("identity")):
                return True
        elif isinstance(part, ast.Name) and part.id in _PERSONAL:
            return True
    return False


def _extra(given: ast.AST, scope: ast.AST | None, tree: ast.Module, parents: dict[ast.AST, ast.AST]):
    """What a call hands the formatter as ``extra``: the keys outside ``_EXTRA_KEPT`` (or why they cannot be read),
    and the values, read as arguments are. A name is read as what its function set it to, a dictionary written out."""
    found = [given]
    if isinstance(given, ast.Name):
        found = [
            value
            for target, value in _sources(tree)
            if isinstance(target, ast.Name) and target.id == given.id and _scope(target, parents) is scope
        ]
    if not found or not all(isinstance(each, ast.Dict) for each in found):
        return ["extra that cannot be read"], []
    outside, values = [], []
    for each in found:
        for key, value in zip(each.keys, each.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value in _EXTRA_KEPT):
                outside.append(ast.unparse(key) if key is not None else "**")
            values.append(value)
    return ([f"extra fields beyond a request's line ({', '.join(outside)})"] if outside else []), values


def _in(node: ast.AST, parents: dict[ast.AST, ast.AST], names: frozenset[str]) -> bool:
    """Whether ``node`` is in a function named one of ``names``."""
    scope = _scope(node, parents)
    return isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef) and scope.name in names


def findings(source: str, *, written: bool = True, exempt: frozenset[str] = frozenset()) -> list[str]:
    """Each logging call of ``source`` that writes an error's words or a traceback's text, each call that prints a
    traceback, and, with ``written``, each that writes past the log: ``<line>: <why>``. Nothing in the functions named
    in ``exempt``."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    loggers = _loggers(tree)
    taint = _tainted(tree, parents)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _in(node, parents, exempt):
            continue
        called = _dotted(node.func) or ""
        if _PRINTED.fullmatch(called):
            found.append(f"{node.lineno}: a traceback ({called})")
            continue
        if written and _WRITTEN.fullmatch(called):
            found.append(f"{node.lineno}: written past the log ({called})")
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr in _LEVELS):
            continue
        if not _is_logger(node.func.value, loggers):
            continue
        why = []
        scope = _scope(node, parents)
        errors = {**_at(taint, scope, node.lineno), **_caught(node, parents)}
        given = [*node.args, *(keyword.value for keyword in node.keywords if keyword.arg not in _OPTIONS)]
        extra: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "extra":
                extra, values = _extra(keyword.value, scope, tree, parents)
                given += values
        if any(_holds(argument, errors) for argument in given):
            why.append("an error's words")
        if any(_personal(argument) for argument in given):
            why.append("a person or a value")
        why += extra
        if why:
            found.append(f"{node.lineno}: {', '.join(why)}")
    return found


def test_no_log_line_of_latets_holds_an_errors_words_or_a_tracebacks_text():
    found = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in findings(
            path.read_text("utf-8"),
            written=path.relative_to(SRC).parts[0] != "cli",  # the command line writes to its terminal
            exempt=EXEMPT.get(path.relative_to(SRC), frozenset()),
        )
    ]
    assert found == []


@pytest.mark.parametrize(
    ("caught", "logged", "found"),
    [
        ("Exception as exc", "log.error('failed: %s', exc)", ["an error's words"]),
        ("Exception as exc", "log.error(f'failed: {exc!r}')", ["an error's words"]),
        ("OSError as exc", "log.warning('failed: %s', str(exc))", ["an error's words"]),
        ("ValueError as exc", "log.info('failed: %s', exc.args[0])", ["an error's words"]),
        ("psycopg.Error as exc", "logger.warning('%r', exc)", ["an error's words"]),
        ("Exception", "log.exception('failed')", []),  # by its types and frames alone
        ("Exception", "log.debug('failed', exc_info=True)", []),
        ("Exception as exc", "log.exception('failed: %s', exc)", ["an error's words"]),
        ("AwsError as error", "log.warning('not signed: %s', error)", ["an error's words"]),
        ("Exception as exc", "log.error('failed: %s', type(exc).__name__)", []),
        ("OSError as exc", "log.error('failed: %s', described(exc))", []),
        ("MaskingError as exc", "log.error('masking failed: %s', exc)", []),
        ("AwsError as error", "log.warning('not signed: %s', error.code)", []),
        ("Exception", "log.debug('failed', exc_info=False)", []),
    ],
)
def test_the_reading_finds_what_it_is_for(caught, logged, found):
    loggers = "log = logging.getLogger(__name__)\nlogger = logging.getLogger('psycopg')\n"
    source = f"{loggers}try:\n    f()\nexcept {caught}:\n    {logged}\n"
    assert findings(source) == ([f"6: {', '.join(found)}"] if found else [])


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ("if (error := task.exception()) is not None:\n        log.error('%s', error)", "4: an error's words"),
        ("error = task.exception()\n    log.error('failed', exc_info=error)", None),  # its types and frames alone
        ("if (error := task.exception()) is not None:\n        log.error('%s', type(error).__name__)", None),
    ],
)
def test_what_a_task_raised_is_read_as_an_error_too(body, found):
    source = f"log = logging.getLogger(__name__)\ndef done(task):\n    {body}\n"
    assert findings(source) == ([found] if found else [])


_TRIED = "import logging, sys, traceback\nlog = logging.getLogger(__name__)\ntry:\n    f()\nexcept {}:\n    {}\n"


@pytest.mark.parametrize(
    ("source", "found"),
    [
        # A logger bound otherwise than by name = logging.getLogger(...).
        (
            "import logging\nlog: logging.Logger = logging.getLogger(__name__)\ntry:\n    f()\n"
            "except Exception as exc:\n    log.error('%s', exc)\n",
            ["6: an error's words"],
        ),
        (
            "from logging import getLogger\nlog = getLogger(__name__)\ntry:\n    f()\n"
            "except Exception as exc:\n    log.error('%s', exc)\n",
            ["6: an error's words"],
        ),
        (
            "import logging\nclass A:\n    def __init__(self):\n        self.log = logging.getLogger('x')\n"
            "    def f(self):\n        try:\n            g()\n        except Exception as exc:\n"
            "            self.log.error('%s', exc)\n",
            ["9: an error's words"],
        ),
        (
            "import logging\ntry:\n    f()\nexcept Exception as exc:\n    logging.getLogger(__name__).error('%s', exc)\n",
            ["5: an error's words"],
        ),
        ("def report(logger, exc):\n    logger.error('%s', exc)\n", ["2: an error's words"]),
        # The methods the reading did not know.
        (_TRIED.format("Exception as exc", "log.warn('%s', exc)"), ["6: an error's words"]),
        (_TRIED.format("Exception as exc", "log.fatal('%s', exc)"), ["6: an error's words"]),
        # An error given as a parameter, kept on self, or made a variable first.
        (
            "import logging\nlog = logging.getLogger(__name__)\ndef report(error: Exception):\n"
            "    log.error('failed: %s', error)\n",
            ["4: an error's words"],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\ntry:\n    f()\nexcept Exception as exc:\n"
            "    why = str(exc)\n    log.error('failed: %s', why)\n",
            ["7: an error's words"],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\ntry:\n    f()\nexcept Exception as exc:\n"
            "    message = f'failed: {exc}'\n    log.error(message)\n",
            ["7: an error's words"],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\nclass A:\n    def f(self):\n        try:\n"
            "            g()\n        except Exception as exc:\n            self.error = exc\n"
            "        log.error('%s', self.error)\n",
            ["9: an error's words"],
        ),
        # A traceback's text, or the error being handled, fetched.
        (_TRIED.format("Exception", "log.error('failed: %s', traceback.format_exc())"), ["6: an error's words"]),
        (_TRIED.format("Exception", "log.error('failed: %s', sys.exc_info()[1])"), ["6: an error's words"]),
        (_TRIED.format("Exception", "log.error('failed', exc_info=sys.exc_info())"), []),
        # What stays allowed: an error whose words are fixed.
        (_TRIED.format("MaskingError as exc", "log.error('%s', exc)"), []),
        # A field handed beside the text is read (a server's JSON lines write each): one a request's line has not, and
        # an error given in one, are found (S8, area 3 M8).
        (
            _TRIED.format("Exception as exc", "log.error('failed', extra={'why': exc})"),
            ["6: an error's words, extra fields beyond a request's line ('why')"],
        ),
    ],
)
def test_the_reading_finds_the_forms_it_missed(source, found):
    assert findings(source) == found


_LOGGED = "import logging\nlog = logging.getLogger(__name__)\ndef f(identity, entity, path, project, email):\n    {}\n"


@pytest.mark.parametrize(
    ("logged", "found"),
    [
        # A person or a value, by its name: an identity's name, a value's original or its fake, a person's email.
        ("log.info('masking for %s', identity.name)", ["4: a person or a value"]),
        ("log.info('%s', entity.original)", ["4: a person or a value"]),
        ("log.info('fake %s', entity.surrogate)", ["4: a person or a value"]),
        ("log.warning('%s', email)", ["4: a person or a value"]),
        ("log.info('%s', project.cwd)", ["4: a person or a value"]),
        # The fields beside the text: a request's alone, read as arguments are, and only what can be read.
        ("log.info('x', extra={'method': 'GET', 'path': path, 'status': 200, 'ms': 3})", []),
        ("log.info('x', extra={'value': entity.original})", [
            "4: a person or a value, extra fields beyond a request's line ('value')",
        ]),
        ("fields = {'status': 200, 'note': path}\n    log.info('x', extra=fields)", [
            "5: extra fields beyond a request's line ('note')",
        ]),
        ("log.info('x', extra=dict(status=200))", ["4: extra that cannot be read"]),
        # What holds no person by its name stays allowed: an identity's id, a file's name, a count.
        ("log.info('%s %s %d', identity.id, path.name, len(entity.id))", []),
    ],
)  # fmt: skip
def test_the_reading_finds_a_person_or_a_value_and_the_fields_beside_the_text(logged, found):
    """S8 (area 3, M8): a plain value given to a line of Latet's (an email, a name, a vault value, a folder) was passed
    by the reading, and so was what a call hands beside its text (``extra``), which a server's JSON lines write."""
    assert findings(_LOGGED.format(logged)) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        # Bound by the module whatever their name: a logger named like nothing a logger is named.
        (
            "from logging import getLogger\nout = getLogger(__name__)\ntry:\n    f()\n"
            "except Exception as exc:\n    out.error('%s', exc)\n",
            ["6: an error's words"],
        ),
        (
            "import logging\nout: logging.Logger = logging.getLogger(__name__)\ntry:\n    f()\n"
            "except Exception as exc:\n    out.error('%s', exc)\n",
            ["6: an error's words"],
        ),
        (
            "import logging\nclass A:\n    def __init__(self):\n        self.out = logging.getLogger('x')\n"
            "    def f(self):\n        try:\n            g()\n        except Exception as exc:\n"
            "            self.out.error('%s', exc)\n",
            ["9: an error's words"],
        ),
        (
            "import logging\ndef report(out: logging.Logger, problem: OSError | None):\n    out.error('%s', problem)\n",
            ["3: an error's words"],
        ),
        # A parameter whose annotation says it holds a text, not an error.
        (
            "import logging\nlog = logging.getLogger(__name__)\ndef report(error: str):\n    log.error('%s', error)\n",
            [],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\ndef report(error: MaskingError):\n    log.error('%s', error)\n",
            [],
        ),
        # Followed to a fixed point, and an attribute across the module's functions.
        (
            "import logging\nlog = logging.getLogger(__name__)\ntry:\n    f()\nexcept Exception as exc:\n"
            "    why = str(exc)\n    words = why.upper()\n    said = (words, 3)\n    log.error('%s %d', *said)\n",
            ["9: an error's words"],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\nclass A:\n    def f(self):\n        try:\n"
            "            g()\n        except Exception as exc:\n            self.failure = exc\n"
            "    def g(self):\n        log.error('%s', self.failure)\n",
            ["10: an error's words"],
        ),
        (
            "import logging\nlog = logging.getLogger(__name__)\ndef f(group: ExceptionGroup):\n"
            "    for inner in group.exceptions:\n        log.error('%s', inner)\n",
            ["5: an error's words"],
        ),
        # What a function sets after its line is not what that line wrote (a variable's, not self's).
        (
            "import logging\nlog = logging.getLogger(__name__)\ndef f(prepared):\n    log.warning('%s', prepared.reason)\n"
            "    try:\n        g()\n    except Exception as exc:\n        prepared.reason = str(exc)\n",
            [],
        ),
        # A traceback printed to the standard error, the server's log, with no logger at all.
        (
            "import traceback\ntry:\n    f()\nexcept Exception:\n    traceback.print_exc()\n",
            ["5: a traceback (traceback.print_exc)"],
        ),
        # What an error's type and its number's fixed text are made of may be said.
        (_TRIED.format("OSError as exc", "kind = type(exc).__name__\n    log.error('failed: %s', kind)"), []),
        (_TRIED.format("OSError as exc", "why = described(exc)\n    log.error('failed: %s', why)"), []),
    ],
)
def test_the_reading_binds_loggers_and_follows_errors_whatever_their_names(source, found):
    assert findings(source) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("print('the model is ready')\n", ["1: written past the log (print)"]),
        ("import sys\nsys.stderr.write('x')\n", ["2: written past the log (sys.stderr.write)"]),
        ("import sys\nsys.stdout.write('x')\n", ["2: written past the log (sys.stdout.write)"]),
        ("import sys\nsys.__stderr__.writelines(['x'])\n", ["2: written past the log (sys.__stderr__.writelines)"]),
        ("def f(out):\n    out.write('x')\n", []),  # a file of its own: no standard stream
    ],
)
def test_the_reading_finds_what_writes_past_the_log(source, found):
    """On a server the standard output is the log, which a print or a write of its own would reach past every rule of
    ``latet.server.logs``; the command line's own modules write to their terminal, and are not read for it."""
    assert findings(source) == found
    assert findings(source, written=False) == []


def test_the_reading_skips_the_functions_exempt_and_no_other():
    source = (
        "import logging\nlog = logging.getLogger(__name__)\n"
        "def hook(kind: type[BaseException]):\n    log.error('%s', kind)\n"
        "def other(kind: type[BaseException]):\n    log.error('%s', kind)\n"
    )
    assert findings(source, exempt=frozenset({"hook"})) == ["6: an error's words"]
