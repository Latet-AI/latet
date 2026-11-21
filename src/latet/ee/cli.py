"""`latet server license`: a Latet server's Enterprise license, through its administration API from any computer with
an administration token (the Enterprise Edition is a server's alone); and `latet policy`, which checks a policy file
before it is set on a server. Registered on the `latet` command by latet.cli.admin when the Enterprise code is there;
each Enterprise part adds its commands here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from latet.cli.main import app
from latet.cli.server import server_app
from latet.ee import approvals_cli as _e5_commands  # noqa: F401 - registers E5's latet server approvals
from latet.ee import server_cli as _e3_commands  # noqa: F401 - registers E3's commands under latet server
from latet.ee.events import cli as _e4_commands  # noqa: F401 - registers E4's reports and SIEM commands
from latet.ee.license import (
    MAX_BYTES,
    LicenseError,
    Standing,
    State,
    describe,
    feature_order,
    file_text,
    standing_of,
)
from latet.ee.policy_cli import policy_app, server_policy_app
from latet.ee.sync import cli as _e6_commands  # noqa: F401 - registers E6's latet server terms
from latet.fileio import FileReadError, read_regular_bytes

app.add_typer(policy_app, name="policy")


def _days(days: int) -> str:
    if days > 0:
        return f"{days} day{'' if days == 1 else 's'} left"
    return "its last day" if days == 0 else f"ended {-days} day{'' if days == -1 else 's'} ago"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def lines(standing: Standing, kept_in: str | None) -> list[str]:
    """What `latet server license show` prints of a standing."""
    if standing.state is State.NONE:
        return ["No license is installed: Latet runs as the Community Edition."]
    where = [] if kept_in is None else [f"kept in: {kept_in}"]
    lic = standing.license
    if lic is None:  # the words `latet server status` uses: what the server keeps, and how the file is replaced
        return [f"{_sentence(describe(standing)[1])}.", *where]
    if standing.users_in_use is None:
        users = f"{lic.users} bought, not counted"
    else:
        users = f"{lic.users} bought, {standing.users_in_use} in use (people and API keys)"
    out = [
        f"License {lic.id}{' (trial)' if lic.trial else ''}: {lic.licensee}",
        f"state: {standing.state.value}",
        f"users: {users}",
        f"features: {', '.join(sorted(lic.features, key=feature_order))}",
        f"issued: {lic.issued.isoformat()}",
        f"expires: {lic.expires.isoformat()} ({_days(standing.days_left or 0)})",
    ]
    status, detail = describe(standing)
    if status != "ok":
        out.append(f"notice: {detail}")
    return [*out, *where]


def _file(file: Path) -> bytes:
    """The bytes of the license file the person names: a regular file, at most one byte over what a license may be
    (so that a longer one is said too large, not read whole)."""
    try:
        return read_regular_bytes(file, limit=MAX_BYTES + 1, follow=True)
    except FileReadError as failure:
        if failure.reason == "too_large":
            words = f"{file} is larger than {MAX_BYTES // 1024} KB: it is not a license file"
        else:
            words = f"{file} {failure}"
        typer.secho(f"Not installed: {words}.", fg="red", err=True)
        raise typer.Exit(2) from None


# --- a server's: `latet server license` ---------------------------------------------------------------------------


server_license_app = typer.Typer(
    help="The server's Latet Enterprise license: show, install or remove it.", no_args_is_help=True
)
# E0c's `latet server` group, imported by its name: whatever was imported first, `latet server license` joins it (a
# lookup in `app` would find nothing before latet.cli.server is imported, and the command would quietly be missing).
server_app.add_typer(server_license_app, name="license")
server_app.add_typer(server_policy_app, name="policy")


def _said(text: str) -> str:
    """What the server said, as a terminal may show it: what a terminal would act on escaped."""
    from latet.server.client import printable

    return printable(text, max(1, len(text) * 10 + 1))


def _admin(method: str, path: str, body: Any = None, *, refused: str) -> Any:
    """E0c's call of the administration API (``path`` below /_latet/server/v1/admin/): a refusal, or a server that
    cannot be reached or named, said in its words, escaped, and exit 2."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    try:
        return server_cli.admin_call(method, path, body)
    except (ServerError, NotConnected) as exc:
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None


def _standing(answer: Any, refused: str) -> Standing:
    """The standing the server answered; an answer that is none is said, exit 1."""
    try:
        return standing_of(answer)
    except (KeyError, TypeError, ValueError, AttributeError):
        typer.secho(f"{refused}: the server's answer is not a license's standing.", fg="red", err=True)
        raise typer.Exit(1) from None


@server_license_app.command("show")
def server_show() -> None:
    """Show the server's license: who it is for, the users bought and in use (people and API keys), the dates."""
    standing = _standing(_admin("GET", "ee/license", refused="Not shown"), "Not shown")
    for line in lines(standing, None if standing.state is State.NONE else "the server's database"):
        typer.echo(_said(line))
    if standing.state is State.NONE:
        typer.echo("`latet server license install <file>` installs one.")
    if standing.state is State.INVALID:
        raise typer.Exit(1)


POLICY_STOPS = "the server's policy stops being in force"
TERMS_STOP = "synced terms stop being masked"  # R29: a license change that switches term sync off
# The final security review's L1: a license change that switches central approvals off while the security team decides
# on changes, on the Live Reviewer's texts or on both (``_routed`` says which): whoever asks decides again.
CHANGES_STOP = "changes that lower protection no longer go to the security team"
TEXTS_STOP = "the Live Reviewer's texts no longer go to the security team"
BOTH_GO_BACK = "changes that lower protection and the Live Reviewer's texts no longer go to the security team"
# L1' (the final security review's re-review): with `changes: self`, a license that keeps `roles` and drops approvals.
KEYS_STOP = "an API key's changes that lower protection no longer go to its other controllers"
# latet.ee.approvals_api.FEATURE, named here: that module loads the server's own (Valkey's client among them), which a
# computer that only runs `latet server` may not have.
APPROVALS = "approvals"


def _policy_stored(refused: str) -> bool:
    """Whether the server keeps a policy (Latet Enterprise's locked rules), stored whether or not it can be read there:
    a change of the license that leaves none listing locked rules ends it. A server of a release before them has none
    (404)."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    try:
        answer = server_cli.admin_call("GET", "ee/policy")
    except ServerError as exc:
        if exc.status == 404:
            return False
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    except NotConnected as exc:
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    return isinstance(answer, dict) and (answer.get("policy") is not None or bool(answer.get("problems")))


def _terms_kept(refused: str) -> bool:
    """Whether the server keeps synced terms while its license lists term sync (Latet Enterprise's E6): a change of the
    license that leaves none listing it stops masking every one. A 403 there says the license in place lists no term
    sync (nothing of it stops), a 404 a release before it; any other refusal stops the command (fix round 1's M7)."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    try:
        answer = server_cli.admin_call("GET", "ee/term-sync")
    except ServerError as exc:
        if exc.status in (403, 404):
            return False
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    except NotConnected as exc:
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    kept = answer.get("kept") if isinstance(answer, dict) else None
    return type(kept) is int and kept > 0


def _routed(refused: str, *, keys_stay: bool = False) -> list[str]:
    """What the server sends to someone other than whoever asks while its license lists approvals (Latet Enterprise's
    E5), in the words said when it stops: a change of the license that leaves none listing them sends it back to
    whoever asks. The security team's (the final security review's L1), and, with ``keys_stay`` (the license left
    keeps `roles`: an API key's space stays open) while the server has an API key and changes are whoever's settings
    they are, a key's other controllers' (L1', the server's ``api_keys``). None of it while nothing goes there; a 403
    there says the license in place lists no approvals (nothing of it stops), a 404 a release before them; any other
    refusal stops the command."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    try:
        answer = server_cli.admin_call("GET", "ee/approvals/settings")
    except ServerError as exc:
        if exc.status in (403, 404):
            return []
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    except NotConnected as exc:
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    approvals = answer.get("approvals") if isinstance(answer, dict) else None
    if not isinstance(approvals, dict):
        return []
    changes, texts = approvals.get("changes") == "security", approvals.get("live_review") == "security"
    said = [BOTH_GO_BACK] if changes and texts else [CHANGES_STOP] if changes else [TEXTS_STOP] if texts else []
    if keys_stay and not changes and isinstance(answer, dict) and answer.get("api_keys") is True:
        said.append(KEYS_STOP)
    return said


def _locks_rules(standing: Standing) -> bool:
    """Whether the license the server holds may keep the policy in force: it lists locked rules, or its file cannot be
    used, when every instance protects with the features its installer verified, which the server's answer does not
    name (the final security review's L2). Only then does a change that leaves none end the policy (one not in force
    does not stop)."""
    from latet.ee.policy import FEATURE

    if standing.state is State.INVALID:
        return True
    return standing.license is not None and FEATURE in standing.license.features


def _may_end(data: bytes, feature: str) -> bool:
    """Whether installing ``data`` may switch ``feature`` off: the file does not list it, or this Latet cannot check
    which it lists (signed with a key or in a version it does not know: the server may). A file that is no license is
    the server's to refuse, saying why: nothing is asked about it."""
    from latet.ee.license import parse_license

    try:
        return feature not in parse_license(data).features
    except LicenseError as exc:
        return exc.code in ("key", "no-keys", "version")


def _ends_the_policy(data: bytes) -> bool:
    """Whether installing ``data`` ends the server's policy: the file may switch locked rules off (``_may_end``), the
    license there lists them, and a policy is stored."""
    from latet.ee.policy import FEATURE

    if not _may_end(data, FEATURE):
        return False
    if not _locks_rules(_standing(_admin("GET", "ee/license", refused="Not installed"), "Not installed")):
        return False
    return _policy_stored("Not installed")


def _ends_the_terms(data: bytes) -> bool:
    """Whether installing ``data`` stops masking the synced terms (R29): the file may switch term sync off
    (``_may_end``) while the server keeps synced terms under a license that lists it (``_terms_kept``)."""
    from latet.ee.sync.service import FEATURE

    return _may_end(data, FEATURE) and _terms_kept("Not installed")


def _ends_the_approvals(data: bytes) -> list[str]:
    """What installing ``data`` sends back to whoever asks, in words (the final security review's L1, L1'): the file
    may switch approvals off (``_may_end``) while the server sends something to someone else under a license that
    lists them (``_routed``; an API key's controllers' only while the file may keep `roles`). Empty when nothing
    stops."""
    if not _may_end(data, APPROVALS):
        return []
    return _routed("Not installed", keys_stay=_may_keep(data, "roles"))


def _may_keep(data: bytes, feature: str) -> bool:
    """Whether installing ``data`` may leave ``feature`` on: the file lists it, or this Latet cannot check which it
    lists (as ``_may_end``)."""
    from latet.ee.license import parse_license

    try:
        return feature in parse_license(data).features
    except LicenseError as exc:
        return exc.code in ("key", "no-keys", "version")


def _joined(words: list[str], last: str) -> str:
    """``words`` in a sentence: "a", "a and b", "a, b and c" (``last``: "and", "or")."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {last} {words[-1]}"


def _lacks(stops: list[tuple[str, str]]) -> str:
    """What a license that stops ``stops`` (each its words and the feature it needs) does not include: locked rules,
    term sync, approvals, or several."""
    return _joined(list(dict.fromkeys(feature for _, feature in stops)), "or")


def _stops(stops: list[tuple[str, str]]) -> None:
    """Said once a change of the license ended the server's policy, the synced terms' masking or central approvals,
    whether it was asked about or not (--yes)."""
    for words, _ in stops:
        typer.secho(f"{words[:1].upper()}{words[1:]}.", fg="yellow")


@server_license_app.command("install")
def server_install(
    file: Annotated[Path, typer.Argument(help="The license file the licensor sent (.json).")],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Install it without asking when the server's policy, its synced terms or its central approvals stop "
            "with it.",
        ),
    ] = False,
) -> None:
    """Check a license file on the server and keep it in its database: every instance takes it at once."""
    data = _file(file)
    try:
        text = file_text(data)  # in the encoding its byte order mark names, as the server reads it
    except ValueError:
        typer.secho("Not installed: it is not a license file: it is not text.", fg="red", err=True)
        raise typer.Exit(2) from None
    # A license without locked rules, term sync or approvals: the policy stops, or the synced terms' masking, or
    # central approvals, yours to decide (L4, R29, the final security review's L1). Each is read before anything is
    # sent.
    ends = [(POLICY_STOPS, "locked rules")] if _ends_the_policy(data) else []
    if _ends_the_terms(data):
        ends.append((TERMS_STOP, "term sync"))
    ends.extend((words, "approvals") for words in _ends_the_approvals(data))
    if ends:
        from latet.cli.admin import _yourself
        from latet.cli.agent import at_your_terminal

        lacks, said = _lacks(ends), _joined([words for words, _ in ends], "and")
        _yourself("latet server license install", f"installs a license without {lacks}: {said}")
        if not yes:
            if not at_your_terminal():
                typer.secho(
                    f"Not installed: this license does not include {lacks}, so {said}: run it at your own terminal, "
                    "or add --yes.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(2)
            question = f"This license does not include {lacks}: {said}. Install it?"
            if not typer.confirm(question, default=False):
                raise typer.Exit(1)
    answer = _admin("PUT", "ee/license", {"license": text}, refused="Not installed")
    standing = _standing(answer, "Not installed")
    lic = standing.license
    if lic is None:  # the server answers the license it installed: never here
        typer.secho("Not installed: the server's answer names no license.", fg="red", err=True)
        raise typer.Exit(2)
    trial = " (trial)" if lic.trial else ""
    typer.echo(
        _said(
            f"Installed on the server: {lic.id}{trial}, {lic.licensee}, {lic.users} users, "
            f"until {lic.expires.isoformat()}."
        )
    )
    status, detail = describe(standing)
    if status != "ok":
        typer.secho(_said(detail), fg="yellow")
    _stops(ends)


@server_license_app.command("remove")
def server_remove(yes: Annotated[bool, typer.Option("--yes", "-y", help="Remove it without asking.")] = False) -> None:
    """Remove the server's license: the Enterprise features stop for everyone on it."""
    from latet.cli.admin import _yourself
    from latet.cli.agent import at_your_terminal

    _yourself("latet server license remove")
    # Looked at first: nothing to ask about when there is none.
    held = _standing(_admin("GET", "ee/license", refused="Not removed"), "Not removed")
    if held.state is State.NONE:
        typer.echo("No license was installed on the server.")
        return
    policy = _locks_rules(held) and _policy_stored("Not removed")  # L4: a policy in force stops
    terms = _terms_kept("Not removed")  # R29: the synced terms' masking stops
    routed = _routed("Not removed")  # L1: what goes to the security team goes back (a removal closes keys' spaces)
    ends = [(POLICY_STOPS, "locked rules")] if policy else []
    if terms:
        ends.append((TERMS_STOP, "term sync"))
    ends.extend((words, "approvals") for words in routed)
    if not yes:
        stops = f", and {_joined([words for words, _ in ends], 'and')}" if ends else ""
        if not at_your_terminal():
            typer.secho("Not removed: run it at your own terminal, or add --yes.", fg="red", err=True)
            raise typer.Exit(2)
        question = f"Remove the server's license? The Enterprise features stop for everyone on it{stops}."
        if not typer.confirm(question, default=False):
            raise typer.Exit(1)
    answer = _admin("DELETE", "ee/license", refused="Not removed")
    if isinstance(answer, dict) and answer.get("removed") is True:
        typer.echo("License removed: the server runs as the Community Edition.")
        _stops(ends)
    else:
        typer.echo("No license was installed on the server.")
