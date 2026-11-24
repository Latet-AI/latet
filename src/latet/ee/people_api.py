"""People, groups and roles: the People and groups page of a server's dashboard, and `latet server groups|roles`
through the administration API. Who asks was checked before: by the role's gate (ee/access.py) on the dashboard, by
the administration token for `latet server`; the license by the route's kind (ee/routes.py). Every store call runs in
the request's threads (``who.run``), every change in one transaction with its entries (``who.change``), and what its
answer says of the change is read in that transaction too: a change is answered as made, or not made.

A path names a person or a group by its id alone: the request log keeps every path, and a name or an e-mail is an
identity value. A body names a person by their id, their user name at the identity provider, or their e-mail; an
e-mail only when one enabled person has it and it was given by hand or by SCIM, never one an ID token brought (H1: the
provider may let its users set it), and never when it reads as another person's user name: such a name is refused, and
the person is named by their id. A person SCIM manages is named by their id alone (R2): a SCIM token sets their user
name and e-mail, so a leaked one could move them to an impostor account. Every answer that names someone says whom it
was taken for; no refusal quotes what the request said.

A code to connect a computer is made for a person who is not disabled, and from the dashboard only for someone who has
never connected one (M4): another code would connect a computer they never see; `latet server people code` makes it.
The command shown is E0c's: the server's address, its self-signed certificate's fingerprint when the instance serves
one, and the code apart, out of a shell's history.

A change that takes the administrator role from someone, or disables them, says the SCIM tokens they made that still
serve, which it does not end (O3): a token is the provider's credential, and ending it would stop its removals too."""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.ee.people import (
    GROUP_ID,
    Changing,
    Group,
    LastAdmin,
    Source,
    Taken,
    disable_person,
    enable_person,
    user_name_key,
)
from latet.ee.roles import GRANTED, Role
from latet.ee.routes import EeRoute, Refused, Who, json_body
from latet.server.identities import IDENTITY_ID, NAME_LENGTH, EmailTaken, clean

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.ee.people import People
    from latet.ee.scim import ScimTokens
    from latet.server.app import Server
    from latet.server.identities import Identity
    from latet.server.settings import ServerSettings

log = logging.getLogger(__name__)

_GRANTED = {role.value: role for role in GRANTED}
# Fixed words: a refusal never quotes what the request said (a path, a name, a body's text).
_EMAIL_TAKEN = "an enabled person on this server has this e-mail already"
_NO_PERSON = "there is no such person on this server"
_NO_GROUP = "there is no such group on this server"
_NO_SUBJECT = "there is no such person or group on this server"
_BY_ID = (
    "this names more than one person, or a person by an e-mail that may not name them (one an ID token brought, or "
    "one more than one person has): name them by their id"
)
_SCIM_BY_ID = (
    "this names a person SCIM manages, whose user name and e-mail a SCIM token can change: name them by their id"
)
_BOTH = "this names a person and a group: name it by its id"
_A_PERSON = "person is a person's id, user name or e-mail"
_A_SUBJECT = "subject is a person's id, user name or e-mail, or a group's id or name"
_CONNECTED = (
    "a computer of theirs is connected already: a code made here would connect another one they never see; "
    "`latet server people code` makes it"
)
_ENTERED = ("local", "scim")  # where an e-mail that names a person comes from: by hand, or SCIM (H1)


@dataclass(frozen=True)
class Subject:
    """What a role, or the control of a key, is given to: a person or a group."""

    kind: Literal["person", "group"]
    id: str
    name: str

    def view(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "name": self.name}


def person_by_id(server: Server, key: str) -> Identity:
    """The person a path names: by their id alone, as PostgreSQL has them now (404 in fixed words for anything else:
    a path is kept in the request log, and a name or an e-mail is never written there)."""
    identity = server.identities.current(key) if IDENTITY_ID.fullmatch(key) else None
    if identity is None or identity.kind != "person":
        raise Refused(404, _NO_PERSON)
    return identity


def group_by_id(people: People, key: str) -> Group:
    """The group a path names: by its id alone (``person_by_id``)."""
    group = people.group(key) if GROUP_ID.fullmatch(key) else None
    if group is None:
        raise Refused(404, _NO_GROUP)
    return group


def subject_by_id(server: Server, people: People, key: str) -> Subject:
    """The person or group a path names, by its id alone."""
    try:
        if GROUP_ID.fullmatch(key):
            group = group_by_id(people, key)
            return Subject("group", group.id, group.name)
        person = person_by_id(server, key)
    except Refused:
        raise Refused(404, _NO_SUBJECT) from None
    return Subject("person", person.id, person.name)


def person_of(server: Server, people: People, key: Any) -> Identity:
    """The person a body names: by their id, their user name at the provider or their e-mail, each with A to Z's case
    aside and nothing else folded (``user_name_key``, as ``People.find`` compares a user name: a look-alike, such as a
    Kelvin sign for a k, names nobody else), as PostgreSQL has them now: an e-mail only when one enabled person has it
    and it was given by hand or by SCIM (H1), and neither a user name nor an e-mail that names two people (409: by their
    id), nor one that names a person SCIM manages (409: by their id, ``_not_scims``). 400 for anything but text."""
    if not isinstance(key, str) or not key:
        raise Refused(400, _A_PERSON)
    identities = server.identities
    if IDENTITY_ID.fullmatch(key):
        return person_by_id(server, key)
    named: Identity | None = None
    if (found := people.find(user_name=key)) is not None:
        identity = identities.current(found)
        named = identity if identity is not None and identity.kind == "person" else None
    wanted = user_name_key(key)
    having = [
        person for person in identities.all(kind="person") if person.email and user_name_key(person.email) == wanted
    ]
    if having and named is not None and any(person.id != named.id for person in having):
        raise Refused(409, _BY_ID)  # a user name that reads as another person's e-mail
    if named is not None:
        return _not_scims(people, named)
    enabled = [person for person in having if not person.disabled]
    if len(enabled) == 1 and len(having) == 1 and enabled[0].source in _ENTERED:
        return _not_scims(people, enabled[0])
    if having:  # several people, one disabled, or an e-mail an ID token brought
        raise Refused(409, _BY_ID)
    raise Refused(404, _NO_PERSON)


def _not_scims(people: People, person: Identity) -> Identity:
    """``person``, found by a user name or an e-mail, unless SCIM manages them (R2): a SCIM token sets every value that
    names them, so a leaked one could move those values to an impostor account it made; such a person is named by their
    id alone (409, naming no one: the id of the account found could be the impostor's)."""
    account = people.account(person.id)
    if account is not None and account.scim_managed:
        raise Refused(409, _SCIM_BY_ID)
    return person


def subject_of(server: Server, people: People, key: Any) -> Subject:
    """The person (id, user name or e-mail) or group (id or name) a body names; a name that is both is given by its
    id. 400 for anything but text."""
    if not isinstance(key, str) or not key:
        raise Refused(400, _A_SUBJECT)
    group = people.group(key)
    try:
        person: Identity | None = person_of(server, people, key)
    except Refused as exc:
        if exc.status != 404:  # a name that names two people is no group's either
            raise
        person = None
    if group is not None and person is not None:
        raise Refused(409, _BOTH)
    if group is not None:
        return Subject("group", group.id, group.name)
    if person is not None:
        return Subject("person", person.id, person.name)
    raise Refused(404, _NO_SUBJECT)


def role_of(value: Any) -> Role:
    if not isinstance(value, str) or value not in _GRANTED:
        raise Refused(400, "a role is group-lead, security, auditor or admin")
    return _GRANTED[value]


def group_view(group: Group) -> dict[str, Any]:
    return {"id": group.id, "name": group.name, "source": group.source, "external_id": group.external_id}


def _person_view(person: Identity) -> dict[str, str]:
    return {"id": person.id, "name": person.name}


def _with_tokens(answer: dict[str, Any], orphaned: list[str]) -> JSONResponse:
    """``answer``, with the SCIM tokens that still serve though their maker no longer administers (O3), if any."""
    return JSONResponse({**answer, "scim_tokens": orphaned} if orphaned else answer)


def connect_command(settings: ServerSettings) -> str:
    """`latet connect` as E0c shows it (``latet.cli.server._connect_command``): the server's address, and the
    fingerprint of the self-signed certificate the instance serves, which a computer pins; the code is shown apart.
    None is named for a certificate of an authority (the company's, a public one: computers check it as they check
    any) or one that cannot be read (said in the log by its type): `latet server certificate` reads it then."""
    command = f"latet connect {settings.url.rstrip('/')}"
    if settings.certificate is None:
        return command
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from latet.engine.reviewer.certificate import fingerprint_of, is_authority

    try:
        pem = settings.certificate.read_text("utf-8")
        if is_authority(pem):  # never pinned: `latet connect` and `latet server certificate` refuse it
            return command
        cert = x509.load_pem_x509_certificates(pem.encode())[0]
    except (OSError, ValueError) as exc:
        log.warning("the server's certificate could not be read for the connect command (%s)", type(exc).__name__)
        return command
    if cert.issuer != cert.subject:  # an authority's: no fingerprint to pin
        return command
    return f"{command} --fingerprint {fingerprint_of(cert.public_bytes(serialization.Encoding.PEM).decode())}"


class PeopleApi:
    """The routes of people, groups and roles (ee/routes.py serves them to the dashboard and to `latet server`)."""

    def __init__(self, server: Server, people: People, scim_tokens: ScimTokens | None = None) -> None:
        self.server = server
        self.people = people
        self.scim_tokens = scim_tokens

    def routes(self) -> list[EeRoute]:
        return [
            EeRoute("GET", "/people", self.list_people),
            EeRoute("POST", "/people", self.add_person, kind="serve", answers=201),
            EeRoute("POST", "/people/{id}/code", self.code, kind="serve"),
            EeRoute("POST", "/people/{id}/disable", self.disable, kind="serve"),
            EeRoute("POST", "/people/{id}/enable", self.enable, kind="serve"),
            # H1: a person linked to the identity provider by their user name, or their subject taken off once its
            # issuer changed: `latet server people link|unlink` alone.
            EeRoute("POST", "/people/{id}/link", self.link, kind="serve", feature="sso", dashboard=False),
            EeRoute("DELETE", "/people/{id}/link", self.unlink, kind="serve", feature="sso", dashboard=False),
            EeRoute("GET", "/groups", self.list_groups),
            EeRoute("POST", "/groups", self.add_group, answers=201),
            EeRoute("PUT", "/groups/{id}", self.rename_group),
            EeRoute("DELETE", "/groups/{id}", self.remove_group),
            EeRoute("POST", "/groups/{id}/members", self.join),
            EeRoute("DELETE", "/groups/{id}/members/{person}", self.leave),
            EeRoute("GET", "/roles", self.list_roles),
            EeRoute("POST", "/roles", self.grant),
            EeRoute("DELETE", "/roles/{subject}/{role}", self.revoke),
        ]

    def _orphaned(self, conn: Connection[Any], makers: Collection[str]) -> list[str]:
        """In a change's transaction, once its work is done: the SCIM tokens that still serve made by one of ``makers``
        who is no longer an enabled administrator, as that transaction finds them: what a change that took the role
        from them, or disabled them, says (O3). Never one ended here."""
        if self.scim_tokens is None or not makers:
            return []
        now, admins = self.scim_tokens.clock(), self.people.admins(conn=conn)
        return sorted(
            token.id
            for token in self.scim_tokens.all(conn=conn)
            if token.made_by in makers
            and token.made_by not in admins
            and (token.expires is None or token.expires > now)
        )

    # --- people -----------------------------------------------------------------------------------------------

    def _people_view(self) -> dict[str, Any]:
        directory = self.people.directory()
        groups = {group.id: group for group in directory.groups}
        memberships: dict[str, list[tuple[Group, Source]]] = {}
        for group_id, members in directory.members.items():
            for identity_id, source in members:
                if group_id in groups:
                    memberships.setdefault(identity_id, []).append((groups[group_id], source))
        people = []
        for person in self.server.identities.all(kind="person"):
            account = directory.accounts.get(person.id)
            mine = sorted(memberships.get(person.id, []), key=lambda m: (m[0].name.casefold(), m[1]))
            granted = {
                (role.value, group.id, group.name)
                for group, _source in mine
                for role in directory.roles.get(group.id, ())
            }
            through = sorted(granted)
            people.append(
                {
                    "id": person.id,
                    "name": person.name,
                    "email": person.email,
                    "user_name": account.user_name if account is not None else None,
                    "disabled": person.disabled,
                    "source": person.source,
                    "last_seen": person.last_seen,
                    # Whether the provider's sign-in is linked to them (never what links it), SCIM manages them, and
                    # who disabled them: an administrator's disable is never lifted by SCIM (L7).
                    "linked": account is not None and account.subject is not None,
                    # Whether their first sign-in finds their account by its user name (an administrator or SCIM
                    # linked it, and nobody signed in to it since): `latet server people link --all` leaves them be.
                    "claimable": account is not None
                    and account.subject is None
                    and not account.deleted
                    and account.linked_by is not None,
                    # Whether their user name was changed (SCIM's rename) since a link or a sign-in confirmed it:
                    # their account waits for an administrator's link, which `latet server people link --all` never
                    # makes.
                    "renamed": account is not None and account.renamed,
                    "scim_managed": account is not None and account.scim_managed,
                    "disabled_by": account.disabled_by if account is not None and person.disabled else None,
                    "groups": [{"id": group.id, "name": group.name, "source": source} for group, source in mine],
                    "roles": sorted(role.value for role in directory.roles.get(person.id, ())),
                    "through": [{"role": role, "group": {"id": gid, "name": name}} for role, gid, name in through],
                }
            )
        people.sort(key=lambda view: (view["disabled"], view["name"].casefold()))
        return {"people": people, "enabled": self.server.identities.enabled_count()}

    async def list_people(self, request: Request, who: Who) -> Response:
        return JSONResponse(await who.run(self._people_view))

    async def add_person(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        name, email = data.get("name"), data.get("email")
        if not isinstance(name, str) or len(name) > NAME_LENGTH or not clean(name, NAME_LENGTH):
            raise Refused(400, f"a person needs a name, of {NAME_LENGTH} characters at most")
        if email is not None and (not isinstance(email, str) or "@" not in email or len(email) > 320):
            raise Refused(400, "an e-mail address, or none")
        wanted = (name.strip(), email.strip() if isinstance(email, str) else None)
        # Read before the change: no row is held while the certificate's file is read; a failure changes nothing.
        command = await who.run(connect_command, self.server.settings)

        def add(within: Changing) -> tuple[Identity, str]:
            try:
                person, code = self.server.identities.add_person(*wanted, within=within.changes)
            except EmailTaken:
                raise Refused(409, _EMAIL_TAKEN) from None
            within.note("person.added", person.id, {"source": "local"})
            return person, code

        person, code = await who.change(add)
        body = {"person": _person_view(person), "code": code, "command": command}
        return JSONResponse(body, status_code=201)

    async def code(self, request: Request, who: Who) -> Response:
        person = await who.run(person_by_id, self.server, request.path_params["id"])
        # Read before the change: the certificate's file is never read while the person's row is held.
        command = await who.run(connect_command, self.server.settings)

        def make(within: Changing) -> str:
            # Their row held: a computer's enrolment, or a disable, meanwhile waits for the code.
            held = self.server.identities.held_person(person.id, within=within.changes)
            if held is None:
                raise Refused(404, _NO_PERSON)
            if held.disabled:
                raise Refused(409, "this person is disabled: enable them first")
            if who.person and held.connected:
                raise Refused(409, _CONNECTED)
            code = self.server.identities.code(person.id, within=within.changes)
            if code is None:
                raise Refused(404, _NO_PERSON)
            within.note("person.code.made", person.id)  # whom it was made for, and by whom (O6): never the code
            return code

        code = await who.change(make)
        return JSONResponse({"code": code, "command": command, "person": _person_view(person)})

    async def disable(self, request: Request, who: Who) -> Response:
        person = await who.run(person_by_id, self.server, request.path_params["id"])

        def disable(within: Changing) -> list[str]:
            try:
                disable_person(self.people, person.id, within=within, keep_admin=who.person)  # by an administrator
            except LastAdmin as exc:
                raise Refused(409, str(exc)) from None
            return self._orphaned(within.conn, {person.id})

        orphaned = await who.change(disable)
        return _with_tokens({"ok": True, "person": _person_view(person)}, orphaned)

    async def enable(self, request: Request, who: Who) -> Response:
        person = await who.run(person_by_id, self.server, request.path_params["id"])

        def enable(within: Changing) -> None:
            try:
                enable_person(self.people, person.id, within=within)
            except EmailTaken:
                raise Refused(409, _EMAIL_TAKEN) from None

        await who.change(enable)
        return JSONResponse({"ok": True, "person": _person_view(person)})

    async def link(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        user_name, scim = data.get("user_name"), data.get("scim", False)
        if not isinstance(user_name, str) or not user_name.strip():
            raise Refused(400, "a person is linked by their user name at the identity provider")
        if not isinstance(scim, bool):
            raise Refused(400, "scim is true or false")
        person = await who.run(person_by_id, self.server, request.path_params["id"])

        def link(within: Changing) -> None:
            try:
                self.people.link_person(person.id, user_name=user_name.strip(), scim=scim, within=within)
            except Taken as exc:  # its words quote nothing given
                raise Refused(409, str(exc)) from None
            except KeyError:
                raise Refused(404, _NO_PERSON) from None
            except ValueError as exc:  # its words quote nothing given
                raise Refused(400, str(exc)) from None

        await who.change(link)
        return JSONResponse({"ok": True, "person": _person_view(person)})

    async def unlink(self, request: Request, who: Who) -> Response:
        person = await who.run(person_by_id, self.server, request.path_params["id"])
        if not await who.change(lambda within: self.people.unlink(person.id, within=within)):
            raise Refused(404, "this person's account names no sign-in of the identity provider")
        return JSONResponse({"ok": True, "person": _person_view(person)})

    # --- groups -----------------------------------------------------------------------------------------------

    def _groups_view(self) -> dict[str, Any]:
        directory = self.people.directory()
        names = {person.id: person.name for person in self.server.identities.all(kind="person")}
        groups = []
        for group in directory.groups:
            members = [
                {"id": identity_id, "name": names[identity_id], "source": source}
                for identity_id, source in directory.members.get(group.id, [])
                if identity_id in names
            ]
            members.sort(key=lambda member: (member["name"].casefold(), member["source"]))
            roles = sorted(role.value for role in directory.roles.get(group.id, ()))
            groups.append({**group_view(group), "members": members, "roles": roles})
        return {"groups": groups}

    async def list_groups(self, request: Request, who: Who) -> Response:
        return JSONResponse(await who.run(self._groups_view))

    async def add_group(self, request: Request, who: Who) -> Response:
        name = (await json_body(request, who)).get("name")
        if not isinstance(name, str):
            raise Refused(400, "a group needs a name")

        def add(within: Changing) -> Group:
            try:
                return self.people.add_group(name, within=within)
            except Taken as exc:  # its words quote nothing given
                raise Refused(409, str(exc)) from None
            except ValueError as exc:  # its words quote nothing given
                raise Refused(400, str(exc)) from None

        return JSONResponse({"group": group_view(await who.change(add))}, status_code=201)

    async def rename_group(self, request: Request, who: Who) -> Response:
        name = (await json_body(request, who)).get("name")
        if not isinstance(name, str):
            raise Refused(400, "a group needs a name")
        group = await who.run(group_by_id, self.people, request.path_params["id"])
        if group.source != "local":
            raise Refused(409, "this group comes from your identity provider: rename it there")

        def rename(within: Changing) -> Group:
            try:
                return self.people.update_group(group.id, name, group.external_id, within=within)
            except Taken as exc:
                raise Refused(409, str(exc)) from None
            except KeyError:
                raise Refused(404, _NO_GROUP) from None
            except ValueError as exc:
                raise Refused(400, str(exc)) from None

        return JSONResponse({"group": group_view(await who.change(rename))})

    async def remove_group(self, request: Request, who: Who) -> Response:
        group = await who.run(group_by_id, self.people, request.path_params["id"])
        if group.source == "scim":
            raise Refused(409, "this group comes from your identity provider through SCIM: it is removed there")

        def remove(within: Changing) -> list[str]:
            if who.person:  # the last administrator kept: their lock first, before the group's row
                self.people.hold_admins(within.conn)
            members = self.people.held_members(within.conn, group.id)  # whom it takes roles from (O3), held
            try:
                gone = self.people.remove_group(group.id, within=within, keep_admin=who.person)
            except LastAdmin as exc:
                raise Refused(409, str(exc)) from None
            if not gone:  # removed meanwhile, on any instance: nothing was changed here
                raise Refused(404, _NO_GROUP)
            return self._orphaned(within.conn, members)

        return _with_tokens({"ok": True}, await who.change(remove))

    async def join(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        group = await who.run(group_by_id, self.people, request.path_params["id"])
        person = await who.run(person_of, self.server, self.people, data.get("person"))

        def join(within: Changing) -> None:
            try:
                self.people.add_member(group.id, person.id, within=within)
            except KeyError:  # removed meanwhile
                raise Refused(404, "the group or the person is no longer there") from None

        await who.change(join)
        return JSONResponse({"ok": True, "person": _person_view(person)})

    async def leave(self, request: Request, who: Who) -> Response:
        group = await who.run(group_by_id, self.people, request.path_params["id"])
        person = await who.run(person_by_id, self.server, request.path_params["person"])

        def leave(within: Changing) -> tuple[bool, list[str]]:
            try:
                gone = self.people.remove_member(group.id, person.id, within=within, keep_admin=who.person)
            except LastAdmin as exc:
                raise Refused(409, str(exc)) from None
            return gone, (self._orphaned(within.conn, {person.id}) if gone else [])

        gone, orphaned = await who.change(leave)
        if gone:
            return _with_tokens({"ok": True, "person": _person_view(person)}, orphaned)
        if any(ref.id == person.id for ref in await who.run(self.people.members, group.id)):  # nothing changed
            detail = "this person is in this group through your identity provider: change their groups there"
            raise Refused(409, detail)
        raise Refused(404, "this person is not in this group")

    # --- roles ------------------------------------------------------------------------------------------------

    def _roles_view(self) -> dict[str, Any]:
        directory = self.people.directory()
        names = {person.id: ("person", person.name) for person in self.server.identities.all(kind="person")}
        names |= {group.id: ("group", group.name) for group in directory.groups}
        grants = [
            {"subject": {"kind": names[subject][0], "id": subject, "name": names[subject][1]}, "role": role.value}
            for subject, roles in directory.roles.items()
            if subject in names  # a person removed since, or a group gone
            for role in roles
        ]
        grants.sort(key=lambda grant: (grant["role"], grant["subject"]["name"].casefold()))
        return {"grants": grants}

    async def list_roles(self, request: Request, who: Who) -> Response:
        return JSONResponse(await who.run(self._roles_view))

    async def grant(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        role = role_of(data.get("role"))
        subject = await who.run(subject_of, self.server, self.people, data.get("subject"))

        def grant(within: Changing) -> frozenset[Role]:
            try:
                self.people.grant(subject.id, role, within=within)
            except KeyError:  # removed meanwhile
                raise Refused(404, _NO_SUBJECT) from None
            except ValueError as exc:  # its words quote nothing given
                raise Refused(400, str(exc)) from None
            return self.people.given(subject.id, conn=within.conn)

        given = await who.change(grant)
        return JSONResponse({"roles": sorted(role.value for role in given), "subject": subject.view()})

    async def revoke(self, request: Request, who: Who) -> Response:
        role = role_of(request.path_params["role"])
        subject = await who.run(subject_by_id, self.server, self.people, request.path_params["subject"])

        def revoke(within: Changing) -> tuple[bool, frozenset[Role], list[str]]:
            makers: list[str] = []  # who is demoted with it (O3): the person, or the group's members, held
            if role is Role.ADMIN and subject.kind == "person":
                makers = [subject.id]
            elif role is Role.ADMIN:
                if who.person:  # the last administrator kept: their lock first, before the group's row
                    self.people.hold_admins(within.conn)
                makers = self.people.held_members(within.conn, subject.id)
            try:
                gone = self.people.revoke(subject.id, role, within=within, keep_admin=who.person)
            except LastAdmin as exc:
                raise Refused(409, str(exc)) from None
            return gone, self.people.given(subject.id, conn=within.conn), self._orphaned(within.conn, makers)

        gone, given, orphaned = await who.change(revoke)
        if not gone:
            raise Refused(404, "this role was not given to this person or group")
        return _with_tokens({"roles": sorted(role.value for role in given), "subject": subject.view()}, orphaned)
