"""`latet server groups|roles|controllers|sign-in`, and `latet server people link|unlink`: E3's commands, run from any
computer with the server's administration token, through its administration API (/_latet/server/v1/admin/ee/…).

A path names a person, a group or an API key by its id alone (the request log keeps every path): a command given a name
or an e-mail finds the id first, in the lists an administrator reads, as the server finds one a body names (H1: an
e-mail only when one enabled person has it and it was given by hand or by SCIM, never one an ID token brought; a user
name at the provider; a group's name; a key's name), and says whom the server took it for ("Emily Carter
(u_…)"). What the server answered is printed with what a terminal would act on escaped (E1's ``_said``); every refusal
of the server, and a server that cannot be reached or named, exits 2. A change is refused to an agent (``admin_call``:
you run it yourself), the setting of whether the dashboard's changes that apply to everyone take the company sign-in
among them (the user's decision (c))."""

from __future__ import annotations

import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn
from urllib.parse import quote

import typer
import yaml

from latet.cli.server import _confirm, _not_secret, admin_call, people_cli, server_app
from latet.fileio import FileReadError, read_regular_bytes
from latet.server.client import ServerError, printable
from latet.server.connection import USER_ID, NotConnected

groups_cli = typer.Typer(
    help="Groups of people: list, add, rename and remove them, and who is in them (Latet Enterprise).",
    no_args_is_help=True,
)
roles_cli = typer.Typer(help="Roles, given to people or to groups (Latet Enterprise).", no_args_is_help=True)
controllers_cli = typer.Typer(
    help="Who controls each API key: the people and groups who work in its space (Latet Enterprise).",
    no_args_is_help=True,
)
sign_in_cli = typer.Typer(
    help="Single sign-on with OpenID Connect, and provisioning with SCIM (Latet Enterprise).", no_args_is_help=True
)
server_app.add_typer(groups_cli, name="groups")
server_app.add_typer(roles_cli, name="roles")
server_app.add_typer(controllers_cli, name="controllers")
server_app.add_typer(sign_in_cli, name="sign-in")

GroupArg = Annotated[str, typer.Argument(help="The group: its id (g_…) or its name.")]
PersonArg = Annotated[str, typer.Argument(help="The person: their id (u_…), e-mail or user name at the provider.")]
SubjectArg = Annotated[str, typer.Argument(help="A person (id, e-mail or user name) or a group (id or name).")]
RoleArg = Annotated[str, typer.Argument(help="group-lead, security, auditor or admin.")]
KeyArg = Annotated[
    str, typer.Argument(help="The API key: its id (u_…) or its name, as `latet server keys list` shows.")
]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Confirm in a script; your own terminal still asks.")]
SOURCES = {"local": "by hand", "scim": "SCIM", "oidc": "sign-in"}
GROUP_ID = re.compile(r"g_[a-z2-7]{10}")  # E3's group ids (latet.ee.people.GROUP_ID, which needs the server extra)
SCIM_TOKEN_ID = re.compile(r"s_[a-z2-7]{10}")
MAX_FILE = 64 * 1024  # bytes of a sign-in settings file: a few hundred, as the server keeps a section
ACCOUNTS = ("scim", "group", "open")
COMPANY_CHANGES = ("sso", "any-session")
_ENTERED = ("local", "scim")  # where an e-mail that names a person comes from: by hand, or SCIM (H1)


class _Unnamed(Exception):
    """What a name given to a command does not name, in words to say: never sent anywhere."""


def _said(text: str) -> str:
    """What the server said, as a terminal may show it: what a terminal would act on escaped, as E1's ``_said`` in
    latet.ee.cli does (not imported from there: latet.ee.cli imports this module to register its commands)."""
    return printable(text, max(1, len(text) * 10 + 1))


def _out(text: str) -> None:
    typer.echo(_said(text))


def _stop(words: str) -> NoReturn:
    typer.secho(f"latet: {_said(words)}", fg="red", err=True)
    raise typer.Exit(2)


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """The server's answer to ``path`` under its administration API's ee/; its refusal printed (escaped, with what is
    wrong where it says), and exit 2, as is a server that cannot be named or reached (E1's ``_admin``)."""
    try:
        return admin_call(method, f"ee/{path}", body)
    except ServerError as exc:
        typer.secho(f"latet: {_said(str(exc.message))}", fg="red", err=True)
        for detail in exc.details:
            typer.secho(f"  {_said(str(detail))}", fg="red", err=True)
        raise typer.Exit(2) from None
    except NotConnected as exc:
        typer.secho(f"latet: {_said(str(exc.message))}", fg="red", err=True)
        raise typer.Exit(2) from None


def _q(value: str) -> str:
    return quote(value, safe="")


def _rows(answer: Any, key: str) -> list[dict[str, Any]]:
    rows = answer.get(key) if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        _stop(f"the server's answer holds no {key}")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "?"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _day(moment: Any) -> str:
    if moment is None:
        return "never ends"
    if isinstance(moment, bool) or not isinstance(moment, int | float):
        return "ends ?"
    try:
        return f"ends on {datetime.fromtimestamp(moment, UTC).date().isoformat()}"
    except (ValueError, OverflowError, OSError):
        return "ends ?"


def _whom(subject: Any) -> str:
    """Whom the server took a name for: "Emily Carter (person, u_…)"."""
    if not isinstance(subject, dict):
        return "?"
    return f"{_text(subject.get('name'))} ({_text(subject.get('kind'))}, {_text(subject.get('id'))})"


def _person_of(answer: Any) -> str:
    """The person an answer names: "Emily Carter (u_…)"."""
    person = answer.get("person") if isinstance(answer, dict) else None
    if not isinstance(person, dict):
        return "?"
    return f"{_text(person.get('name'))} ({_text(person.get('id'))})"


def _orphaned(answer: Any) -> None:
    """O3: the SCIM tokens a change's answer says still serve, though whoever made them no longer administers."""
    tokens = answer.get("scim_tokens") if isinstance(answer, dict) else None
    if isinstance(tokens, list) and tokens:
        ids = ", ".join(_text(token) for token in tokens)
        _out(f"SCIM tokens made by someone who no longer administers still serve: {ids} (`latet server sign-in show`)")


# --- naming: a person, a group, a subject, an API key, as the server would take them ----------------------------------


_ASCII_LOWER = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}


def _user_name_key(user_name: str) -> str:
    """A user name, or an e-mail naming a person, as the server compares it: A to Z folded, every other character as it
    is (its twin is ``latet.ee.people.user_name_key``, which needs the server extra): a look-alike never names another
    person."""
    return user_name.translate(_ASCII_LOWER)


def _match_person(key: str, people: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The person ``key`` names among ``people`` (the People and groups page's list), as the server finds one a body
    names (``person_of``, H1): by their id; by their user name at the provider; by an e-mail only when one enabled
    person has it and it was given by hand or by SCIM; both with A to Z's case aside and nothing else folded
    (``_user_name_key``). None for no one; ``_Unnamed`` when it names two people, reads as one's user name and
    another's e-mail, or names a person SCIM manages, who is named by their id alone (``_not_scims``, R2)."""
    if USER_ID.fullmatch(key):
        return next((person for person in people if person["id"] == key), None)
    folded = _user_name_key(key)
    named = [p for p in people if isinstance(p.get("user_name"), str) and _user_name_key(p["user_name"]) == folded]
    having = [p for p in people if isinstance(p.get("email"), str) and _user_name_key(p["email"]) == folded]
    by_id = f"{key} names more than one person, or a person by an e-mail that may not name them: name them by their id"
    if len(named) > 1 or (named and any(person["id"] != named[0]["id"] for person in having)):
        raise _Unnamed(by_id)
    if named:
        return _not_scims(key, named[0])
    enabled = [person for person in having if person.get("disabled") is not True]
    if len(having) == 1 and len(enabled) == 1 and enabled[0].get("source") in _ENTERED:
        return _not_scims(key, enabled[0])
    if having:
        raise _Unnamed(by_id)
    return None


def _not_scims(key: str, person: dict[str, Any]) -> dict[str, Any]:
    """``person``, found by ``key`` (a user name or an e-mail), unless SCIM manages them (R2, as the server's
    ``people_api._not_scims``): ``_Unnamed``, naming no id, since the account found could be an impostor's."""
    if person.get("scim_managed") is True:
        raise _Unnamed(
            f"{key} names a person SCIM manages, whose user name and e-mail a SCIM token can change: name them by "
            "their id"
        )
    return person


def _match_group(key: str, groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The group ``key`` names: by its id, or by its name with case aside. ``_Unnamed`` when two groups' names fold
    alike: the server keeps names apart by PostgreSQL's ``lower``, which folds less (Großkunden and Grosskunden are two
    groups there, one name here), and a SCIM token could make the second to take the first one's roles."""
    if GROUP_ID.fullmatch(key):
        return next((group for group in groups if group["id"] == key), None)
    folded = key.casefold()
    found = [g for g in groups if isinstance(g.get("name"), str) and g["name"].casefold() == folded]
    if len(found) > 1:
        raise _Unnamed(f"{key} names more than one group: name it by its id")
    return found[0] if found else None


def _people() -> list[dict[str, Any]]:
    return _rows(_call("GET", "people"), "people")


def _groups() -> list[dict[str, Any]]:
    return _rows(_call("GET", "groups"), "groups")


def _person(key: str) -> dict[str, Any]:
    """The person ``key`` names: an id as it is (the server says whom it names, and refuses one it has not), a user
    name or an e-mail found in the list first."""
    _not_secret(key)
    if USER_ID.fullmatch(key):
        return {"id": key}
    try:
        found = _match_person(key, _people())
    except _Unnamed as exc:
        _stop(str(exc))
    if found is None:
        _stop(f"no person on this server is {key} (`latet server people list`)")
    return found


def _group(key: str) -> dict[str, Any]:
    try:
        found = _match_group(key, _groups())
    except _Unnamed as exc:
        _stop(str(exc))
    if found is None:
        _stop(f"no group on this server is {key} (`latet server groups list`)")
    return found


def _subject(key: str) -> str:
    """The id of the person or the group ``key`` names (an id as it is: the server says whom it names); a name that is
    both is given by its id."""
    _not_secret(key)
    if USER_ID.fullmatch(key) or GROUP_ID.fullmatch(key):
        return key
    try:
        group = _match_group(key, _groups())
        person = _match_person(key, _people())
    except _Unnamed as exc:
        _stop(str(exc))
    if group is not None and person is not None:
        _stop(f"{key} names a person and a group: name it by its id")
    found = group or person
    if found is None:
        _stop(f"no person or group on this server is {key}")
    return found["id"]


def _key(key: str) -> dict[str, Any]:
    _not_secret(key)
    keys = _rows(_call("GET", "keys"), "keys")
    if USER_ID.fullmatch(key):
        found = [k for k in keys if k["id"] == key]
    else:
        found = [k for k in keys if isinstance(k.get("name"), str) and k["name"].casefold() == key.casefold()]
    if not found:
        _stop(f"no API key on this server is {key} (`latet server keys list`)")
    if len(found) > 1:
        _stop(f"{key} names {len(found)} API keys: use the id ({', '.join(k['id'] for k in found)})")
    return found[0]


# --- groups ---------------------------------------------------------------------------------------------------------


@groups_cli.command("list")
def groups_list() -> None:
    """Show the groups, where each comes from, the roles given to it and who is in it."""
    groups = _groups()
    for group in groups:
        members = _list(group.get("members"))
        named = ", ".join(_text(m.get("name")) for m in members if isinstance(m, dict)) or "nobody"
        roles = ", ".join(_text(role) for role in _list(group.get("roles"))) or "no role"
        source = SOURCES.get(_text(group.get("source")), "?")
        _out(f"{group['id']}  {_text(group.get('name'))} ({source}): {roles}; {named}")
    if not groups:
        typer.echo('No group yet: latet server groups add "Payments leads"')


@groups_cli.command("add")
def groups_add(name: Annotated[str, typer.Argument(help="The group's name.")]) -> None:
    """Add a group, made by hand."""
    group = _call("POST", "groups", {"name": name}).get("group", {})
    _out(f"Group {_text(group.get('name'))} added ({_text(group.get('id'))}).")


@groups_cli.command("rename")
def groups_rename(group: GroupArg, name: Annotated[str, typer.Argument(help="Its new name.")]) -> None:
    """Rename a group made by hand (one from the identity provider is renamed there)."""
    found = _group(group)
    renamed = _call("PUT", f"groups/{_q(found['id'])}", {"name": name}).get("group", {})
    _out(f"Group {_text(found.get('name'))} ({found['id']}) is now {_text(renamed.get('name'))}.")


@groups_cli.command("remove")
def groups_remove(group: GroupArg) -> None:
    """Remove a group: its members keep their accounts; the roles given to it and its control of API keys go."""
    found = _group(group)
    answer = _call("DELETE", f"groups/{_q(found['id'])}")
    _out(f"Group {_text(found.get('name'))} ({found['id']}) removed.")
    _orphaned(answer)


@groups_cli.command("join")
def groups_join(group: GroupArg, person: PersonArg) -> None:
    """Put a person in a group, by hand."""
    found, someone = _group(group), _person(person)
    answer = _call("POST", f"groups/{_q(found['id'])}/members", {"person": someone["id"]})
    _out(f"{_person_of(answer)} is in {_text(found.get('name'))}.")


@groups_cli.command("leave")
def groups_leave(group: GroupArg, person: PersonArg) -> None:
    """Take a person out of a group they were put in by hand."""
    found, someone = _group(group), _person(person)
    answer = _call("DELETE", f"groups/{_q(found['id'])}/members/{_q(someone['id'])}")
    _out(f"{_person_of(answer)} is no longer in {_text(found.get('name'))}.")
    _orphaned(answer)


# --- roles ----------------------------------------------------------------------------------------------------------


@roles_cli.command("list")
def roles_list() -> None:
    """Show who has each role: the people and the groups it is given to."""
    answer = _call("GET", "roles")
    grants = answer.get("grants") if isinstance(answer, dict) else None
    grants = [grant for grant in grants if isinstance(grant, dict)] if isinstance(grants, list) else []
    for grant in grants:
        _out(f"{_text(grant.get('role'))}: {_whom(grant.get('subject'))}")
    if not grants:
        typer.echo("No role is given: everyone is a user.")


@roles_cli.command("grant")
def roles_grant(subject: SubjectArg, role: RoleArg) -> None:
    """Give a role to a person or a group. The first administrator is made this way."""
    answer = _call("POST", "roles", {"subject": _subject(subject), "role": role})
    roles = ", ".join(_text(given) for given in answer.get("roles", []))
    _out(f"{_whom(answer.get('subject'))} has: {roles}.")


@roles_cli.command("revoke")
def roles_revoke(subject: SubjectArg, role: RoleArg) -> None:
    """Take a role back from a person or a group."""
    answer = _call("DELETE", f"roles/{_q(_subject(subject))}/{_q(role)}")
    roles = ", ".join(_text(given) for given in answer.get("roles", [])) or "no role given"
    _out(f"{_whom(answer.get('subject'))} has: {roles}.")
    _orphaned(answer)


# --- the control of API keys ----------------------------------------------------------------------------------------


@controllers_cli.command("list")
def controllers_list() -> None:
    """Show each API key and the people and groups who control it."""
    keys = _rows(_call("GET", "keys"), "keys")
    for key in keys:
        controllers = _list(key.get("controllers"))
        named = ", ".join(
            f"{_text(c.get('name'))} ({_text(c.get('kind'))})" for c in controllers if isinstance(c, dict)
        )
        _out(f"{key['id']}  {_text(key.get('name'))}: {named or 'administrators only'}")
    if not keys:
        typer.echo('No API key yet: latet server keys add "Billing CI"')


@controllers_cli.command("add")
def controllers_add(key: KeyArg, subject: SubjectArg) -> None:
    """Give the control of an API key to a person or a group: they work in its space from their dashboard."""
    found = _key(key)
    answer = _call("POST", f"keys/{_q(found['id'])}/controllers", {"subject": _subject(subject)})
    _out(f"{_whom(answer.get('subject'))} controls {_text(found.get('name'))} ({found['id']}).")


@controllers_cli.command("remove")
def controllers_remove(key: KeyArg, subject: SubjectArg) -> None:
    """Take the control of an API key back."""
    found = _key(key)
    answer = _call("DELETE", f"keys/{_q(found['id'])}/controllers/{_q(_subject(subject))}")
    _out(f"{_whom(answer.get('subject'))} no longer controls {_text(found.get('name'))} ({found['id']}).")


# --- people linked to the identity provider (H1) --------------------------------------------------------------------


@people_cli.command("link")
def people_link(
    person: Annotated[
        str | None, typer.Argument(help="The person: their id (u_…), e-mail or user name at the provider.")
    ] = None,
    user_name: Annotated[
        str | None, typer.Option("--user-name", help="Their user name at the identity provider.")
    ] = None,
    scim: Annotated[bool, typer.Option("--scim", help="Hand them to SCIM too: it manages their account.")] = False,
    every: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Everyone whose account has a user name and names no sign-in, linked again by it, but those whose "
            "user name SCIM changed since a sign-in or an administrator confirmed it; with --scim, SCIM is handed "
            "those it did not manage but those who hold a role.",
        ),
    ] = False,
    yes: Yes = False,
) -> None:
    """Link a person to the identity provider by their user name there: their first sign-in finds their account.

    A person added by hand is linked this way (or links their own account from their dashboard). --scim hands them to
    SCIM too. With --all, everyone whose account has a user name and names no sign-in (after `unlink --all`, once the
    provider's issuer changed) is linked again by that user name, SCIM managing whom it managed before; with --all
    --scim, SCIM is also handed those it did not manage, but not someone who holds a role, given to them or to a group
    made by hand: they are handed over one by one, `latet server people link <id> --user-name <name> --scim`. Someone
    whose user name SCIM changed since a sign-in or an administrator confirmed it is never linked by --all: they are
    listed, to link one by one once your provider confirms the user name. Whom a run before linked already, waiting
    for their first sign-in, is left out and counted.
    """
    if every:
        if person is not None or user_name is not None:
            _stop("--all or a person, not both: each account is linked again by its own user name")
        _link_every(scim, yes)
        return
    if person is None or user_name is None:
        _stop("name the person and their user name at the provider: link PERSON --user-name NAME")
    _not_secret(user_name)
    found = _person(person)
    answer = _call("POST", f"people/{_q(found['id'])}/link", {"user_name": user_name, "scim": scim})
    handed = ", and handed to SCIM" if scim else ""
    _out(f"{_person_of(answer)} is linked to the provider's user {user_name}{handed}: their first sign-in finds them.")


def _holds_a_role(person: dict[str, Any], local: set[str]) -> bool:
    """Whether the person holds a role given to them or to a group made by hand (``local``: those groups' ids): SCIM
    is never handed them in bulk, since whoever holds a SCIM token could then change who they are (M3)."""
    through = (_dict(_dict(given).get("group")).get("id") for given in _list(person.get("through")))
    return bool(_list(person.get("roles"))) or any(group in local for group in through)


def _link_every(scim: bool, yes: bool) -> None:
    """Everyone enabled whose account has a user name and names no sign-in, linked again by that user name: SCIM
    keeps whom it managed; with ``scim`` it is handed the others too, but those who hold a role, listed apart. Whom a
    run before linked already (their first sign-in finds them, and this run would hand SCIM nothing more) is left out,
    and counted, rather than listed and asked about again. An account whose user name SCIM changed since a sign-in or
    an administrator confirmed it (``renamed``: a leaked SCIM token can make such a rename) is never linked here: by
    that user name, a first sign-in of whoever holds it at the provider would take the account and all it holds. It is
    listed apart, to link one by one once the provider confirms the user name."""
    waiting = [
        p
        for p in _people()
        if isinstance(p.get("user_name"), str) and p.get("linked") is False and p.get("disabled") is not True
    ]
    if not waiting:
        typer.echo("Nobody to link: every enabled account with a user name names a sign-in already.")
        return
    local = {group["id"] for group in _groups() if group.get("source") == "local"} if scim else set()
    plan: list[tuple[dict[str, Any], bool, str]] = []  # the person, handed to SCIM, what the list says of them
    apart: list[tuple[dict[str, Any], str]] = []  # linked already, holding a role: handed to SCIM by hand alone
    renamed: list[tuple[dict[str, Any], str]] = []  # their user name changed, unconfirmed since: by hand alone
    done = 0  # linked already, and SCIM's as this run would leave them: nothing to change
    for p in waiting:
        if p.get("renamed") is True:
            handing = " --scim" if p.get("scim_managed") is True else ""
            one_by_one = f"`latet server people link {p['id']} --user-name <name>{handing}`"
            said = "user name changed by SCIM since a sign-in or a link confirmed it: not linked by it; check it with"
            renamed.append((p, f"{said} your provider, then link them one by one with {one_by_one}"))
            continue
        if p.get("scim_managed") is True:
            handed, said = True, "SCIM manages them"
        elif not scim:
            handed, said = False, "SCIM does not manage them"
        elif _holds_a_role(p, local):
            one_by_one = f"`latet server people link {p['id']} --user-name <name> --scim`"
            handed, said = False, f"holds a role: not handed to SCIM; hand them over one by one with {one_by_one}"
        else:
            handed, said = True, "newly handed to SCIM"
        if p.get("claimable") is not True or handed != (p.get("scim_managed") is True):
            plan.append((p, handed, said))
        elif scim and not handed:
            apart.append((p, said))
        else:
            done += 1
    if done:
        people = "person is" if done == 1 else "people are"
        typer.echo(f"Left out: {done} {people} linked already and waiting for their first sign-in.")
    for p, said in apart:  # nothing to send for them: said, never asked about
        _out(f"  {_text(p.get('name'))} ({p['id']}): {_text(p.get('user_name'))} (linked already; {said})")
    for p, said in renamed:  # nor for them
        _out(f"  {_text(p.get('name'))} ({p['id']}): {_text(p.get('user_name'))} ({said})")
    if not plan:
        typer.echo("Nobody else to link.")
        return
    for p, _handed, said in plan:
        _out(f"  {_text(p.get('name'))} ({p['id']}): {_text(p.get('user_name'))} ({said})")
    newly = sum(1 for _p, _handed, said in plan if said == "newly handed to SCIM")
    handing = f", and hand SCIM the {newly} marked newly handed" if newly else ""
    _confirm(
        f"Link these {len(plan)} people again by their user names{handing}? Each one's next sign-in with that user "
        "name takes their account.",
        yes,
    )
    for p, handed, said in plan:
        answer = _call("POST", f"people/{_q(p['id'])}/link", {"user_name": p["user_name"], "scim": handed})
        newly_said = ", and newly handed to SCIM" if said == "newly handed to SCIM" else ""
        _out(f"{_person_of(answer)} is linked again{newly_said}.")


@people_cli.command("unlink")
def people_unlink(
    person: Annotated[str | None, typer.Argument(help="The person: their id (u_…), e-mail or user name.")] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Everyone whose account names a sign-in: the provider's issuer changed.")
    ] = False,
    yes: Yes = False,
) -> None:
    """Take the sign-in a person's account names off it, once the provider's issuer changed.

    Their next sign-in brings another one. Until they are linked again (`latet server people link`, or their own Link
    my company account, unless SCIM changed their user name since their last sign-in with it) no sign-in takes their
    account.
    """
    if every:
        if person is not None:
            _stop("--all or a person, not both")
        linked = [p for p in _people() if p.get("linked") is True]
        if not linked:
            typer.echo("Nobody's account names a sign-in.")
            return
        for p in linked:
            _out(f"  {_text(p.get('name'))} ({p['id']})")
        _confirm(
            f"Unlink these {len(linked)} people? No sign-in takes their accounts until they are linked again.", yes
        )
        for p in linked:
            path = f"people/{_q(p['id'])}/link"
            _out(f"{_person_of(_call('DELETE', path))} is unlinked.")
        return
    if person is None:
        _stop("name the person, or --all")
    answer = _call("DELETE", f"people/{_q(_person(person)['id'])}/link")
    _out(f"{_person_of(answer)} is unlinked: link them again for their next sign-in to find them.")


# --- sign-in --------------------------------------------------------------------------------------------------------


def _maker(token: dict[str, Any]) -> str:
    maker = _dict(token.get("maker"))
    if maker.get("kind") == "administration":
        return "made with `latet server`"
    made = f"made by {_text(maker.get('name') or 'someone removed since')} ({_text(maker.get('id'))})"
    if maker.get("disabled") is True:
        return f"{made}, who is disabled"
    return made if maker.get("admin") is True else f"{made}, who no longer administers"


@sign_in_cli.command("show")
def sign_in_show() -> None:
    """Show single sign-on's settings, whether the client secret is saved, and SCIM's address and tokens."""
    view = _call("GET", "sign-in")
    sso = view.get("sso")
    if not isinstance(sso, dict):
        typer.echo("Single sign-on: off (`latet server sign-in set` turns it on)")
        typer.echo(
            "Changes that apply to everyone, from the dashboard: any administrator's session (no single sign-on)"
        )
    else:
        _out(f"Single sign-on: {_text(sso.get('issuer'))}, client {_text(sso.get('client_id'))}")
        if view.get("secret") is True:
            typer.echo("Client secret: saved")
        elif view.get("secret_issuer_changed") is True:
            typer.echo("Client secret: saved for another issuer: save it again (`latet server sign-in secret`)")
        elif sso.get("client_secret") is None:
            typer.echo("Client secret: none (a public client)")
        else:
            typer.echo("Client secret: not saved: `latet server sign-in secret`")
        accounts = _text(sso.get("accounts"))
        group = sso.get("access_group")
        _out(f"Who gets an account: {accounts}" + (f" (the members of {_text(group)})" if group else ""))
        domains = _list(sso.get("allowed_domains"))
        if domains:
            _out(f"Allowed domains: {', '.join(_text(domain) for domain in domains)}")
        mapping = _dict(sso.get("groups"))
        for claimed, mapped in mapping.items():
            _out(f"Group {_text(claimed)} of the ID token: Latet's group {_text(mapped)}")
        _out(f"Redirect URI to register at the provider: {_text(view.get('redirect_uri'))}")
        if view.get("company_changes") == "any-session":
            typer.echo("Changes that apply to everyone, from the dashboard: any administrator's session")
        else:
            typer.echo("Changes that apply to everyone, from the dashboard: a session opened with the company sign-in")
    scim = _dict(view.get("scim"))
    tokens = [token for token in _list(scim.get("tokens")) if isinstance(token, dict)]
    count = f"{len(tokens)} token{'' if len(tokens) == 1 else 's'}"
    state = f"on, {count} ({', '.join(_text(token.get('id')) for token in tokens)})" if tokens else "off (no token)"
    _out(f"SCIM: {_text(scim.get('url'))}, {state}")
    for token in tokens:
        _out(f"SCIM token {_text(token.get('id'))}: {_day(token.get('expires'))}, {_maker(token)}")


def _json_only(value: Any) -> bool:
    """Whether ``value`` is what JSON holds: text, a finite number, true, false, null, a list, a mapping whose keys are
    text. A date, a time or binary data, which YAML reads, is not (JSON's encoder would raise at it)."""
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_only(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_only(item) for item in value)
    if type(value) is float:
        return math.isfinite(value)
    return value is None or type(value) in (str, int, bool)


def _sso_file(file: Path) -> dict[str, Any]:
    """The sso section a YAML file holds: its sso key, or the section's keys at the top. Read as the SIEM's and term
    sync's files are (the final security review's L3): a regular file, never through a symbolic link; in the encoding
    its byte order mark names, else UTF-8; with no alias (``load_yaml``: a few hundred bytes of nested ones would ask
    gigabytes of this computer once written as JSON); and only values JSON holds. Each refusal said in words, exit 2,
    never what the file holds."""
    from latet.config import NO_ALIASES, OUT_OF_RANGE, load_yaml, yaml_error_at
    from latet.ee.license import file_text

    try:
        text = file_text(read_regular_bytes(file, limit=MAX_FILE, follow=False))
    except FileReadError as failure:
        _stop(f"{file}: {failure}")
    except ValueError:
        _stop(f"{file} is not a YAML file: it is not text")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:  # where, and Latet's own reason, never what the file holds
        problem = getattr(exc, "problem", None)
        why = f": {problem}" if problem in (NO_ALIASES, OUT_OF_RANGE) else ""
        _stop(f"{file} is not a YAML file Latet reads{yaml_error_at(exc, text)}{why}")
    except RecursionError:
        _stop(f"{file} is not a YAML file Latet reads: it nests too deeply")
    sso = data.get("sso", data) if isinstance(data, dict) else None
    if not isinstance(sso, dict):
        _stop("the file holds the sso section's keys: issuer, client_id, accounts, …")
    if not _json_only(sso):
        _stop(
            f"{file} holds a value the settings cannot keep: a date, a time, binary data, a key that is not text, or a "
            "number JSON does not hold; write it in quotes"
        )
    return sso


@sign_in_cli.command("set")
def sign_in_set(
    file: Annotated[
        Path | None,
        typer.Argument(help="A YAML file with the sso section (none: the settings in use).", dir_okay=False),
    ] = None,
    accounts: Annotated[
        str | None, typer.Option("--accounts", help="Who gets an account at a first sign-in: scim, group or open.")
    ] = None,
    allowed_domain: Annotated[
        list[str] | None,
        typer.Option("--allowed-domain", help="A Google Workspace domain whose people get in (repeat it)."),
    ] = None,
    company_changes: Annotated[
        str | None,
        typer.Option(
            "--company-changes",
            help="Whether the dashboard's changes that apply to everyone take a session opened with the company "
            "sign-in (sso) or any administrator's session (any-session).",
        ),
    ] = None,
) -> None:
    """Set up single sign-on from a YAML file (its sso section, or the section's keys at the top), or change the
    settings in use with the options alone (refused, nothing changed, when someone changed them meanwhile: run it
    again).

    While the dashboard's changes that apply to everyone take the company sign-in (--company-changes sso, the default),
    a session `latet dashboard`'s one-time code opened reads and changes nothing that applies to everyone. A new issuer
    needs the client secret saved again; people who signed in are then unlinked and linked again (`latet server people
    unlink --all`, `link --all`).
    """
    if accounts is not None and accounts not in ACCOUNTS:
        _stop("--accounts is scim, group or open")
    if company_changes is not None and company_changes not in COMPANY_CHANGES:
        _stop("--company-changes is sso or any-session")
    if file is None and accounts is None and not allowed_domain and company_changes is None:
        _stop("give the settings' file, or what to change (--accounts, --allowed-domain, --company-changes)")
    shown = _dict(_call("GET", "sign-in"))
    current = shown.get("sso")
    body: dict[str, Any] = {}
    if file is not None:
        sso = _sso_file(file)
    elif not isinstance(current, dict):
        _stop("single sign-on is not set up: give its settings' file (`latet server sign-in set <file>`)")
    else:  # the settings in use, changed by the options alone: the server refuses them if they changed meanwhile
        sso = dict(current)
        if isinstance(shown.get("etag"), str):
            body["etag"] = shown["etag"]
    if accounts is not None:
        sso["accounts"] = accounts
    if allowed_domain:
        sso["allowed_domains"] = list(allowed_domain)
    if company_changes is not None:
        sso["company_changes"] = company_changes
    view = _call("PUT", "sign-in", {"sso": sso, **body})
    written = _dict(view.get("sso"))
    _out(f"Single sign-on: {_text(written.get('issuer'))}.")
    _out(f"Redirect URI to register at the provider: {_text(view.get('redirect_uri'))}")
    if view.get("company_changes") == "any-session":
        typer.echo("Changes that apply to everyone, from the dashboard: any administrator's session.")
    else:
        typer.echo("Changes that apply to everyone, from the dashboard: a session opened with the company sign-in.")
    if view.get("secret_issuer_changed") is True:
        typer.echo("The client secret was saved for another issuer: save it again with `latet server sign-in secret`.")
    elif view.get("secret") is not True and written.get("client_secret") is not None:
        typer.echo("Save the client secret the provider gave: `latet server sign-in secret`.")
    if isinstance(current, dict) and _issuer(current.get("issuer")) != _issuer(written.get("issuer")):
        typer.echo(
            "The issuer changed: the people who signed in are linked to the one before. Unlink them and link them "
            "again (`latet server people unlink --all`, then `latet server people link --all`, or each person's Link "
            "my company account)."
        )


def _issuer(value: Any) -> str:
    return value.strip().rstrip("/") if isinstance(value, str) else ""


@sign_in_cli.command("off")
def sign_in_off() -> None:
    """Turn single sign-on off: people open the dashboard with `latet dashboard` only."""
    _call("PUT", "sign-in", {"sso": None})
    typer.echo("Single sign-on is off.")


@sign_in_cli.command("test")
def sign_in_test() -> None:
    """Check that the provider answers: its discovery document and its keys. A real sign-in checks the secret."""
    result = _call("POST", "sign-in/test", {})
    if result.get("ok") is not True:
        typer.secho(
            f"The provider does not answer as single sign-on needs: {_said(_text(result.get('error')))}",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)
    _out(f"The provider answers: issuer {_text(result.get('issuer'))}, signing keys published: {result.get('keys')}.")


@sign_in_cli.command("secret")
def sign_in_secret(remove: Annotated[bool, typer.Option("--remove", help="Forget the saved secret.")] = False) -> None:
    """Save the client secret the provider gave Latet, or forget it (--remove).

    The secret is asked without showing it, or read from standard input; the server keeps it sealed with its master
    key for the issuer it is saved for, never in the settings.
    """
    if remove:
        _call("DELETE", "sign-in/secret")
        typer.echo("The client secret is forgotten.")
        return
    secret = sys.stdin.readline() if not sys.stdin.isatty() else typer.prompt("Client secret", hide_input=True)
    if not secret.strip():
        _stop("no secret given")
    _call("PUT", "sign-in/secret", {"secret": secret.strip()})
    typer.echo("The client secret is saved, sealed with the server's master key for the provider's issuer.")


@sign_in_cli.command("scim-token")
def sign_in_scim_token(
    remove: Annotated[str | None, typer.Option("--remove", help="A token's id: SCIM stops taking it.")] = None,
    days: Annotated[int | None, typer.Option("--days", min=1, max=3650, help="Days until it ends.")] = None,
    never: Annotated[bool, typer.Option("--never", help="A token that never ends.")] = False,
) -> None:
    """Make a token for your identity provider's SCIM provisioning, or remove one (--remove ID).

    The token is printed once; the server keeps its keyed hash. It ends as an administration token does (90 days unless
    --days or --never says otherwise): make the next one before it ends. A token keeps serving when whoever made it no
    longer administers: remove it here.
    """
    if remove is not None:
        if remove.startswith("latet_scim_"):
            _stop(
                "that is a SCIM token, not its id: it is in your shell's history now. Remove it by its id "
                "(`latet server sign-in show`), and give the provider a new one"
            )
        if not SCIM_TOKEN_ID.fullmatch(remove):
            _stop("not a SCIM token's id: `latet server sign-in show` lists them")
        _call("DELETE", f"sign-in/scim-tokens/{_q(remove)}")
        _out(f"The SCIM token {remove} is removed: SCIM stops taking it.")
        return
    if never and days is not None:
        _stop("--days or --never, not both")
    body: dict[str, Any] = {"days": None} if never else ({"days": days} if days is not None else {})
    answer = _call("POST", "sign-in/scim-tokens", body)
    _out(f"SCIM address: {_text(answer.get('url'))}")
    _out(f"Token {_text(answer.get('id'))} (shown once, {_day(answer.get('expires'))}): {_text(answer.get('token'))}")
    typer.echo("Give it to the identity provider's SCIM provisioning now: the server keeps no copy it can show.")
