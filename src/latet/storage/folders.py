"""A folder that is the person's alone, for a secret kept in a file there (this computer's token for a Latet server, a
server's TLS key, a server's service folder): who owns it and who may open it.

On POSIX: owned by the person, writable by nobody else. Darwin additionally requires private-directory mode0700-style
permissions and no ALLOW ACL entries; ancestors use the separate namespace rule. On Windows a file's access follows its
folder's, and a folder outside the person's profile takes its drive's, which lets every user read; and whoever owns a
folder may always read and rewrite its access list (READ_CONTROL and WRITE_DAC, whatever the list says), so another
account's folder is theirs to open at will. Owner and access list are read by SID with ctypes (``folder_grants``:
no new dependency), the reader a server's service folder is checked with too.

A parent folder (``namespace_folder_problem``) may also be changed, on Windows, by the accounts the person trusts in
``LATET_TRUSTED_ACCOUNTS`` (R26), read from the registry's environments alone (R30: ``trusted_values``), and by the
direct members of the local Administrators group (R27), as by the group itself; Latet's data folder and every folder in
it never, as the folder checked or as a parent (``trust``), nor the folder that is to be the person's alone
(``private_folder_problem``): it holds the vault's key. This trust reaches every caller's parents: the data folder's,
an agent's settings folder's, the helper's private chain above the data folder, the parents of the helper's
installation and of the Windows service's log folder."""

from __future__ import annotations

import contextlib
import errno
import functools
import logging
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from latet.logline import described

log = logging.getLogger(__name__)


class Grant(NamedTuple):
    """An entry of a Windows access list that allows: to whom (a SID; "" for a kind of entry not read here, as one that
    may allow anyone), what (its access mask), whether it applies only to what is made inside the folder, and whether
    it is inherited from a folder above (icacls marks it (I)), where it is removed."""

    sid: str
    mask: int
    inherit_only: bool
    inherited: bool = False


SYSTEM, ADMINISTRATORS, CREATOR_OWNER, OWNER_RIGHTS = "S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_NAMESPACE_SAFE = 0xA01200AD  # Read/traverse/standard-read/synchronize and add-subdirectory only.
_DARWIN_NAMESPACE_SAFE = 0x00100A8A  # List/search/read metadata/security/synchronize, never create or mutate.
# A sandboxed app's own SIDs (an app package, a capability): Windows asks them on top of the account's, so an entry for
# one opens nothing to anyone the account's entries do not.
_APP_SIDS = ("S-1-15-2-", "S-1-15-3-")


_OWN = "use a data folder of your own (LATET_DATA_DIR)"
_OWN_CHAIN = (
    "or use a data folder whose every parent only you, SYSTEM and the Administrators can change (LATET_DATA_DIR)"
)
TRUSTED_ACCOUNTS = "LATET_TRUSTED_ACCOUNTS"
_OWN_TRUSTED_CHAIN = (
    "or use a data folder whose every parent only you, SYSTEM, the Administrators and the accounts in "
    f"{TRUSTED_ACCOUNTS} can change (LATET_DATA_DIR)"
)
# The fixed reason of a parent folder another account may change (an access-list entry, a mode): in local mode a
# warning, on a connected computer a refusal (R28). Another owner, or an entry Latet cannot read, stays a refusal.
CHANGEABLE = "namespace can be changed by another account"
_SID = re.compile(r"\*?(S-1-[0-9]+(?:-[0-9]+)+)", re.IGNORECASE)  # icacls writes a SID *S-1-…
_MOST_ENTRIES, _LONGEST_ENTRY = 64, 256
# Where LATET_TRUSTED_ACCOUNTS is read on Windows (R30): the registry's environments, as `setx`, the system settings
# and group policy write them; never the process's environment.
ENVIRONMENTS = (("machine", "the computer's environment"), ("user", "your user environment"))


def _trustable(sid: str) -> bool:
    """Whether ``sid`` is of a form Latet trusts, named in LATET_TRUSTED_ACCOUNTS or as a direct member of the
    Administrators: an account or group of this computer or of a domain (S-1-5-21-…, never a domain's Domain Users,
    Domain Guests or Domain Computers, RIDs 513 to 515, nor the null domain S-1-5-21-0-0-0), of Entra ID
    (S-1-12-1-…) or a service's (S-1-5-80-…, never S-1-5-80-0, every service). Any other form (Everyone, Users,
    Authenticated Users, INTERACTIVE, NTLM Authentication, the identities S-1-18-… a logon asserts, Compounded
    Authentication, Claims Valid, CREATOR OWNER, an app's) stands for many accounts at once, or for none."""
    parts = sid.split("-")
    if sid.startswith("S-1-5-21-0-0-0-"):  # the null domain: Compounded Authentication, Claims Valid stand for many
        return False
    if sid.startswith("S-1-5-21-"):
        return len(parts) == 8 and parts[-1] not in ("513", "514", "515")
    if sid.startswith("S-1-12-1-"):
        return len(parts) == 8
    return sid.startswith("S-1-5-80-") and len(parts) == 9


def _valid_sid(text: str) -> bool:
    """Whether a text shaped as a SID (``_SID``) is one: an authority of 48 bits at most, then 1 to 15 sub-authorities
    of 32 bits each. Windows reads a larger number as the largest (4444444444 as 4294967295): another SID."""
    parts = text.split("-")[2:]
    if not 2 <= len(parts) <= 16 or any(len(part) > 15 for part in parts):  # before int(): no number of 4300 digits
        return False
    authority, *subs = (int(part) for part in parts)
    return authority < 2**48 and all(sub < 2**32 for sub in subs)


def _why(exc: OSError) -> str:
    """Why Windows refused, in its own fixed words for the error (no path, no value), or the words Latet raised."""
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int) and exc.strerror:
        return f"{exc.strerror.strip().rstrip('.')}, Windows error {winerror}"
    if exc.strerror:
        return exc.strerror
    return str(exc.args[0]) if exc.args else type(exc).__name__


def _unsayable(entry: str) -> str | None:
    """Why an entry of LATET_TRUSTED_ACCOUNTS is never written back in words, and trusts nothing: a control or format
    character (a terminal's escape, a right-to-left override that reorders what is shown, a zero-width space, a line
    separator), or a length no account's name has. None when it may be said."""
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp") for char in entry):
        return "has an entry with a control or formatting character"
    if len(entry) > _LONGEST_ENTRY:
        return f"has an entry longer than {_LONGEST_ENTRY} characters"
    return None


def environment_value(scope: str) -> str | None:
    """LATET_TRUSTED_ACCOUNTS in the registry's ``scope`` environment ("machine" or "user"), read only (R30); None
    when it is not set there. Raises OSError when it cannot be read. Tests double this reader: the registry is never
    written by Latet."""
    return _read_environment(scope)


def trusted_values() -> tuple[list[tuple[str, str]], list[str]]:
    """LATET_TRUSTED_ACCOUNTS as Latet reads it on Windows (R30): from the registry, the computer's environment
    (HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment) and the user's (HKCU\\Environment), both,
    as `setx`, the system settings and group policy write them; never from the process's own environment, which a
    repository's settings or an agent's session may set for what they start. So the helper, `latet agents apply` and
    `latet doctor` read the same value. Each value set, with where it is set; and the words for each that cannot be
    read."""
    values: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for scope, where in ENVIRONMENTS:
        try:
            text = environment_value(scope)
        except OSError as exc:
            unreadable.append(f"{TRUSTED_ACCOUNTS} in {where} cannot be read ({_why(exc)}): it trusts nothing")
            continue
        if text is not None and text.strip():
            values.append((where, text))
    return values, unreadable


class Named(NamedTuple):
    """An account or group LATET_TRUSTED_ACCOUNTS names that Windows finds: as written, its SID, and where it is set
    (``ENVIRONMENTS``)."""

    entry: str
    sid: str
    where: str


def trusted_accounts() -> tuple[tuple[Named, ...], tuple[str, ...]]:
    """LATET_TRUSTED_ACCOUNTS read (R26, R30: ``trusted_values``): the Windows accounts or groups it names, by name
    (``COMPUTER\\name``, ``DOMAIN\\name``, ``name``) or by SID (``S-1-5-…``, or ``*S-1-5-…`` as icacls writes it),
    separated by ``;`` or ``,``, 64 at most. Returns each that Windows finds and Latet trusts (``_trustable``), and the
    words for each that trusts nothing: a name Windows does not find, a SID that is none, a group that stands for many
    accounts, an entry with a control or format character or longer than any name (never written back). Read by the
    folder checks on Windows alone; elsewhere they are by owner and mode, and `latet doctor` says so."""
    values, unresolved = trusted_values()
    named: list[Named] = []
    seen: set[str] = set()
    for where, text in values:
        setting = f"{TRUSTED_ACCOUNTS} in {where}"
        for entry in (part.strip() for part in re.split(r"[;,]", text)):
            if not entry or entry.casefold() in seen:
                continue
            if len(seen) == _MOST_ENTRIES:
                unresolved.append(
                    f"{TRUSTED_ACCOUNTS} names more than {_MOST_ENTRIES} accounts: the others trust nothing"
                )
                return tuple(named), tuple(unresolved)
            seen.add(entry.casefold())
            if (unsaid := _unsayable(entry)) is not None:
                unresolved.append(f"{setting} {unsaid}: it trusts nothing")
                continue
            written = match.group(1) if (match := _SID.fullmatch(entry)) else None
            if written is not None and not _valid_sid(written):
                unresolved.append(f"{setting} names {entry}, which is not a SID: it trusts nothing")
                continue
            try:
                sid = canonical_sid(written) if written is not None else account_sid(entry)
            except OSError as exc:
                unresolved.append(
                    f"{setting} names {entry}, which Windows does not find ({_why(exc)}): it trusts nothing"
                )
                continue
            if not _trustable(sid):
                unresolved.append(
                    f"{setting} names {entry} ({sid}), which is no account or group of this computer, a domain, Entra "
                    "ID or a service, but stands for many accounts at once: it trusts nothing"
                )
                continue
            named.append(Named(entry, sid, where))
    return tuple(named), tuple(unresolved)


class Trusted(NamedTuple):
    """Whom a grant on a parent folder may be to, beyond the person, SYSTEM, the Administrators and TrustedInstaller:
    the accounts ``LATET_TRUSTED_ACCOUNTS`` names (R26, R30), and the direct members of the local Administrators group
    (R27); with the words for each entry of the variable that trusts nothing, and why the members could not be read
    (then none of them is trusted)."""

    named: tuple[Named, ...]
    members: frozenset[str]
    unresolved: tuple[str, ...]
    members_problem: str | None

    @property
    def sids(self) -> frozenset[str]:
        return self.members | {item.sid for item in self.named}

    def notes(self) -> list[str]:
        """What a refusal or a warning says of them: an entry that trusts nothing, members that cannot be read."""
        return [*self.unresolved, *([self.members_problem] if self.members_problem else [])]


def trusted() -> Trusted:
    """Whom a parent folder may be changed by beyond the person, SYSTEM, the Administrators and TrustedInstaller, on
    Windows (``Trusted``). The members are read by the group's SID (``administrators_members``); one of a form Latet
    does not trust (``_trustable``) is never trusted."""
    named, unresolved = trusted_accounts()
    try:
        members, problem = frozenset(sid for sid in administrators_members() if _trustable(sid)), None
    except OSError as exc:
        members = frozenset()
        problem = f"the members of the Administrators group cannot be read ({_why(exc)}): none of them is trusted"
    return Trusted(named, members, unresolved, problem)


def namespace_folder_problem(folder: Path, found: os.stat_result, *, trust: bool = True) -> str | None:
    """A single plain ancestor, visited from a trusted OS root: may another account change its namespace?

    This is weaker than secret-folder privacy. A trusted-owner sticky parent protects its existing trusted-owner
    child; the caller must check that next child's owner too, or privately create and inspect it before descent.
    The answer is a fixed reason; ``namespace_refusal`` says the same verdict in words that name the folder, who may
    change it and the fix. ``trust``: False for Latet's data folder and every folder in it, also as the parent of one
    in it, where neither LATET_TRUSTED_ACCOUNTS nor the Administrators' members are trusted (M3).
    """
    verdict = _namespace_verdict(folder, found, trust=trust)
    return verdict[0] if verdict is not None else None


def namespace_refusal(
    folder: Path, found: os.stat_result, *, private: bool = False, fix: str | None = None, trust: bool = True
) -> str | None:
    """``namespace_folder_problem``'s verdict in words a person can act on: the folder, why another account may change
    what is in it (its owner, the account and the rights its access list grants, or its mode) and the fix. These name
    no secret: a folder, an account and its rights. ``private``: the folder is one that is to be the person's alone
    (Latet's data folder), whose fix is `chmod 700`. ``fix``: the caller's own fix in place of a data folder's ("" for
    none: the caller says its own, as the helper's installation check does). ``trust``: as ``namespace_folder_problem``.
    None when the folder passes."""
    verdict = _namespace_verdict(folder, found, private=private, trust=trust)
    if verdict is None:
        return None
    _, reason, default = verdict
    chosen = default if fix is None else fix
    return f"{reason}: {chosen}" if chosen else reason


def _group_holds_alone(gid: int, me: Any, members: Sequence[str], accounts: Iterable[Any]) -> bool:
    """Whether group ``gid`` holds account ``me`` alone: it is that account's own group (``pw_gid``), no other account
    is listed in it, and no other account has it as its own. Ubuntu, Debian and Fedora give each account one."""
    if me.pw_gid != gid or any(member != me.pw_name for member in members):
        return False
    return not any(account.pw_gid == gid and account.pw_uid != me.pw_uid for account in accounts)


@functools.cache
def _group_is_mine_alone(gid: int) -> bool:
    """``_group_holds_alone`` for this account, read from the system's accounts and groups; any doubt (no such
    account or group, a lookup that fails) is no. Kept for the process: an account's groups change rarely."""
    if sys.platform == "win32":
        return False
    try:
        import grp
        import pwd

        return _group_holds_alone(gid, pwd.getpwuid(os.getuid()), grp.getgrgid(gid).gr_mem, pwd.getpwall())
    except (ImportError, KeyError, OSError):
        return False


def _namespace_verdict(
    folder: Path, found: os.stat_result, *, private: bool = False, trust: bool = True
) -> tuple[str, str, str] | None:
    """The fixed reason, the words of a namespace refusal (the folder, and why) and a data folder's fix, or None."""
    unavailable = "namespace access metadata unavailable"
    unread = f"the owner and access list of {folder} cannot be read"
    if sys.platform != "win32":
        if found.st_uid not in (0, os.getuid()):
            return (
                "namespace belongs to another account",
                f"{folder} belongs to another account of this computer (user id {found.st_uid}), which may change "
                "what is in it",
                _OWN,
            )
        if (
            found.st_mode & 0o022
            and not found.st_mode & stat.S_ISVTX
            # A group that holds this account alone may write: nobody else can (Ubuntu's `~/.local` is 0775).
            and (found.st_mode & 0o002 or not _group_is_mine_alone(found.st_gid))
        ):
            return (
                CHANGEABLE,
                f"{folder} can be changed by its group or by other users (mode {stat.S_IMODE(found.st_mode):04o})",
                f"`chmod {'700' if private else 'go-w'} {folder}`, {_OWN_CHAIN}",
            )
        if sys.platform == "darwin":
            try:
                if any(mask & (~_DARWIN_NAMESPACE_SAFE & 0xFFFFFFFFFFFFFFFF) for mask in _darwin_allows(folder, found)):
                    return (
                        CHANGEABLE,
                        f"{folder} has an access-list entry that lets another account change what is in it (`ls -led "
                        f"{folder}` shows it)",
                        f"remove it with `chmod -a`, {_OWN_CHAIN}",
                    )
            except OSError:
                return unavailable, unread, _OWN
        return None
    try:
        read = _read_namespace(folder, trust=trust)
    except OSError:
        return unavailable, unread, _OWN
    if read.owner not in (read.me, SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER):  # a grant's trust is never an owner's
        return (
            "namespace belongs to another account",
            f"{folder} belongs to another account ({account_name(read.owner) if read.owner else 'no owner'}), which "
            "may change what is in it whatever its access list says",
            f'make it yours, `icacls "{folder}" /setowner *{read.me}`, {_OWN_CHAIN}',
        )
    if read.unreadable:  # before any account: an entry Latet cannot read may allow anyone (never only warned, R28)
        return (
            unavailable,
            f'{folder} has an access-list entry Latet cannot read (`icacls "{folder}"` lists its entries)',
            _OWN_CHAIN.removeprefix("or "),
        )
    if not read.changing:
        return None
    grant = read.changing[0]
    who = account_name(grant.sid)
    if who == grant.sid:  # an account deleted since its entry was made leaves its SID alone in the access list
        who = f"{grant.sid}, an account Windows does not find (deleted, or a domain's it cannot reach),"
    reason = (
        f"{folder} lets {who} change what is in it (its access list grants {_rights(grant.mask)}; "
        f'`icacls "{folder}"` lists it)'
    )
    if grant.inherited:  # icacls /remove:g does nothing here: the entry is set on a folder above
        remove = (
            f'remove that entry on the folder above it comes from (it is inherited: `icacls "{folder}"` marks it (I))'
        )
    else:
        remove = f'remove that entry (`icacls "{folder}" /remove:g *{grant.sid}`)'
    trusting = f"trust that account ({TRUSTED_ACCOUNTS}), " if trust and _trustable(grant.sid) else ""
    return (
        CHANGEABLE,
        "; ".join([reason, *(read.trust.notes() if read.trust is not None else [])]),
        f"{remove}, {trusting}{_OWN_TRUSTED_CHAIN}",
    )


class _Namespace(NamedTuple):
    """A Windows folder's owner and access list as a parent folder's check reads them: this account's SID, the owner,
    whether an entry cannot be read, the entries that let an account nobody trusts change what is in the folder, those
    accepted because the person trusts their account or it is a direct member of the Administrators (R26, R27), and
    whom that trust was read for (None when no entry needed it)."""

    me: str
    owner: str | None
    unreadable: bool
    changing: tuple[Grant, ...]
    accepted: tuple[Grant, ...]
    trust: Trusted | None


def _read_namespace(folder: Path, *, trust: bool = True) -> _Namespace:
    """``folder``'s owner and access list read and sorted (``_Namespace``). Who else is trusted (``trusted``: a lookup
    of the names, a read of the Administrators' members) is asked only for an entry that needs it, and never with
    ``trust`` False (the data folder and what is in it). Raises OSError when they cannot be read."""
    me = my_sid()
    base = {me, SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER}
    owner, grants = folder_grants(folder)
    unreadable, changing, accepted, extra = False, [], [], None
    for grant in grants:
        if (
            not isinstance(grant.sid, str)
            or not grant.sid
            or type(grant.mask) is not int
            or not 0 <= grant.mask <= 0xFFFFFFFF
            or type(grant.inherit_only) is not bool
        ):
            unreadable = True
            continue
        if grant.inherit_only or grant.sid in base or grant.sid == OWNER_RIGHTS:
            continue
        if _app_grant(grant.sid):
            continue  # AppContainer rights intersect ordinary account/group rights; never trust an app as owner.
        if grant.sid.startswith("S-1-3-"):  # CREATOR OWNER and its kind stand for whoever makes something
            changing.append(grant)
        elif grant.mask & (~_NAMESPACE_SAFE & 0xFFFFFFFF):
            if trust:
                extra = extra if extra is not None else trusted()
            (accepted if extra is not None and grant.sid in extra.sids else changing).append(grant)
    return _Namespace(me, owner, unreadable, tuple(changing), tuple(accepted), extra)


def namespace_warning(folder: Path, found: os.stat_result) -> str | None:
    """A parent folder another account may change, in local mode (no connection: R28), where Latet goes on and `latet
    doctor` warns: the folder, the account and its rights (or the mode), why Latet goes on, and the ways out (remove
    the entry, trust the account in ``LATET_TRUSTED_ACCOUNTS``, or another data folder). None when it passes, and when
    its verdict stays a refusal in local mode too (another owner, an entry Latet cannot read)."""
    verdict = _namespace_verdict(folder, found)
    if verdict is None or verdict[0] != CHANGEABLE:
        return None
    _, reason, fix = verdict
    return (
        f"{reason}. Latet goes on, since this computer is not connected to a Latet server: an account that can change "
        f"this folder can already change everything of yours in it. To end this warning: {fix}"
    )


def members_trusted(folder: Path) -> tuple[str, ...]:
    """The direct members of the local Administrators group whose entry on ``folder`` Latet accepted, as it accepts
    the group's (R27), for `latet doctor` to say (R29); an account the person named in ``LATET_TRUSTED_ACCOUNTS`` is
    said as theirs. () when there is none, outside Windows, and when the folder cannot be read."""
    if sys.platform != "win32":
        return ()
    try:
        read = _read_namespace(folder)
    except OSError:
        return ()
    if read.trust is None:
        return ()
    named = {item.sid for item in read.trust.named}
    return tuple(
        dict.fromkeys(
            grant.sid for grant in read.accepted if grant.sid in read.trust.members and grant.sid not in named
        )
    )


def _rights(mask: int) -> str:
    """A Windows access mask as icacls names the usual ones: full control, modify, or write."""
    if mask & 0x10000000 or mask & 0x1F01FF == 0x1F01FF:  # GENERIC_ALL, or every standard and file right
        return f"full control, (F), mask 0x{mask:08x}"
    if mask & 0x00010000:  # DELETE beside a right to write
        return f"modify, (M), mask 0x{mask:08x}"
    return f"write, mask 0x{mask:08x}"


def account_name(sid: str) -> str:
    """An account's name and SID (``DOMAIN\\name (S-1-5-…)``) as Windows looks it up; the SID alone when it cannot be,
    and on POSIX."""
    try:
        name = _account(sid)
    except (OSError, ValueError):
        return sid
    return f"{name} ({sid})" if name else sid


def make_private_folder(folder: Path) -> Path:
    """``folder``, one of Latet's own folders for its data (the data folder, its logs, certificates, keys and state),
    made the person's alone: each missing component created 0700, whatever the umask (``Path.mkdir`` gives the parents
    it makes the umask's mode: 0755, or 0775 under a umask of 002); on Windows with the access list its parent gives.
    One that is there already is tightened (``tighten_folder``). Returns ``folder``. Raises OSError when it cannot be
    made, or when what is there is not a folder, or (on POSIX) another account's, or a link that is: whoever could put
    it there could read what Latet keeps in it. The person's own link to a folder of theirs is accepted, and never
    followed by ``tighten_folder``."""
    missing: list[Path] = []
    current = folder
    while not os.path.lexists(current) and current.parent != current:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        with contextlib.suppress(FileExistsError):  # made meanwhile: what it is is checked below and where it is used
            path.mkdir(mode=0o700)
    found = os.lstat(folder)
    if stat.S_ISLNK(found.st_mode) or getattr(found, "st_file_attributes", 0) & 0x400:  # a link or a junction
        # The person's own link to a folder of theirs (a data folder moved elsewhere) is kept working (R1); on POSIX
        # the link and the folder it leads to must both be theirs. Nothing is ever tightened through it.
        if sys.platform != "win32" and found.st_uid != os.getuid():
            raise OSError(f"{folder} is a link of another account of this computer (user id {found.st_uid}): {_OWN}")
        found = os.stat(folder)
    if not stat.S_ISDIR(found.st_mode):
        raise OSError(f"{folder} is not a folder: {_OWN}")
    if sys.platform != "win32" and found.st_uid != os.getuid():
        raise OSError(f"{folder} belongs to another account of this computer (user id {found.st_uid}): {_OWN}")
    tighten_folder(folder)
    return folder


def tighten_folder(folder: Path) -> bool:
    """On POSIX, ``folder`` made 0700 when nobody else could have written into it: a real folder (no link), owned by
    the person, whose only bits for its group and others are read and search (0755, as an older Latet made them, or
    the umask), and on Darwin with no access-list entry that lets another account change it. Taking those bits away
    loses nothing anybody else wrote, and the change is said in the log (it is made once: then there is nothing left to
    take away). A folder others may write into is never changed: it is refused, in words that name it. Whether it was
    changed; False on Windows, where an access list is never rewritten, and whenever it cannot be read."""
    if sys.platform == "win32":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(folder, flags)
    except OSError:
        return False
    try:
        found = os.fstat(descriptor)
        extra = found.st_mode & 0o077
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or not extra or extra & 0o022:
            return False
        if sys.platform == "darwin":
            try:
                if any(mask & (~_DARWIN_NAMESPACE_SAFE & 0xFFFFFFFFFFFFFFFF) for mask in _darwin_allows(folder, found)):
                    return False
            except OSError:
                return False
        os.fchmod(descriptor, 0o700)  # the folder opened and checked, never one a link put in its place
    except OSError:
        return False
    finally:
        os.close(descriptor)
    # The folder is not named: a path holds the person's user name, and a log line holds no personal data.
    log.warning(
        "a folder of Latet's data was readable by other users of this computer (mode %04o): Latet made it yours "
        "alone (chmod 700)",
        stat.S_IMODE(found.st_mode),
    )
    return True


def _app_grant(sid: str) -> bool:
    if not sid.startswith(_APP_SIDS):
        return False
    parts = sid.split("-")[3:]
    return 2 <= len(parts) <= 15 and all(
        part.isascii() and part.isdigit() and len(part) <= 10 and str(int(part)) == part and int(part) <= 0xFFFFFFFF
        for part in parts
    )


def _darwin_allows(folder: Path, found: os.stat_result) -> list[int]:
    """Bound directory ACL metadata through Darwin's public opaque libc accessors; never inspect file content."""
    import ctypes

    unavailable = "Darwin directory ACL metadata unavailable"
    required = tuple(getattr(os, name, 0) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"))
    if not all(required):
        raise OSError(unavailable)
    library, descriptor, acl = None, None, None
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        signatures = (
            ("acl_get_fd_np", (ctypes.c_int, ctypes.c_int), ctypes.c_void_p),
            ("acl_valid", (ctypes.c_void_p,), ctypes.c_int),
            ("acl_get_entry", (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)), ctypes.c_int),
            ("acl_get_tag_type", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)), ctypes.c_int),
            ("acl_get_permset_mask_np", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)), ctypes.c_int),
            ("acl_free", (ctypes.c_void_p,), ctypes.c_int),
        )
        for name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
        descriptor = os.open(folder, os.O_RDONLY | required[0] | required[1] | required[2])
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (
            found.st_dev,
            found.st_ino,
            found.st_uid,
            found.st_mode,
        ):
            raise OSError(unavailable)
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(descriptor, 0x100)
        if not acl:
            if ctypes.get_errno() != errno.ENOENT:
                raise OSError(unavailable)
            return []  # Only absent ACL metadata on this already-bound valid descriptor, never pathname ENOENT.
        if library.acl_valid(acl) != 0:
            raise OSError(unavailable)
        allows = []
        for index in range(129):  # At most128 entries, then exactly one opportunity to prove exhaustion.
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = library.acl_get_entry(acl, 0 if index == 0 else -1, ctypes.byref(entry))
            number = ctypes.get_errno()
            if result == -1 and number == errno.EINVAL:
                return allows
            if result != 0 or index == 128 or not entry.value:
                raise OSError(unavailable)
            tag, mask = ctypes.c_uint32(), ctypes.c_uint64()
            if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value not in (1, 2):
                raise OSError(unavailable)
            if library.acl_get_permset_mask_np(entry, ctypes.byref(mask)) != 0:
                raise OSError(unavailable)
            if tag.value == 1:
                allows.append(mask.value)  # DENY never cancels an ALLOW; no UUID/flag/membership exceptions.
        raise OSError(unavailable)
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        raise OSError(unavailable) from None
    finally:
        cleanup_failed = False
        try:
            if acl and library is not None and library.acl_free(acl) != 0:
                cleanup_failed = True
        except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
            cleanup_failed = True
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise OSError(unavailable) from None


def private_folder_problem(folder: Path) -> str | None:
    """Why ``folder`` is not the person's alone, for a secret kept in a file there, in words that name the folder and
    the fix; None when it is. On POSIX: owned by the person, writable by nobody else. Darwin private directories also
    permit no group/other mode bits or ALLOW ACL entries (no/empty/known-DENY-only ACLs pass). On Windows: owned by the
    person, SYSTEM or the Administrators, with an access list that allows nobody else (as Windows reads them, CREATOR
    OWNER's entry for what is made inside stands for whoever makes it, OWNER RIGHTS for the owner, and a sandboxed app's
    own SIDs allow nobody on their own)."""
    if sys.platform == "win32":
        return _windows_folder_problem(folder)
    try:
        found = os.stat(folder)
    except OSError as exc:
        if sys.platform == "darwin":
            return f"the access list of {folder} cannot be read: {_OWN}"
        return f"{folder} cannot be read ({exc.strerror or described(exc)})"
    if not stat.S_ISDIR(found.st_mode):
        return f"{folder} is not a folder"
    if found.st_uid != os.getuid():
        return f"{folder} belongs to another user of this computer: use a data folder of your own (LATET_DATA_DIR)"
    if found.st_mode & 0o022:
        return f"{folder} can be written by its group or by other users: `chmod 700 {folder}`"
    if sys.platform == "darwin":
        if found.st_mode & 0o077:
            return (
                f"{folder} can be read by its group or by other users (mode {stat.S_IMODE(found.st_mode):04o}): "
                f"`chmod 700 {folder}`"
            )
        try:
            if _darwin_allows(folder, found):
                return (
                    f"{folder} has an access-list entry that lets another account in (`ls -led {folder}` shows it): "
                    f"remove its entries with `chmod -N {folder}`, or {_OWN}"
                )
        except OSError:
            return f"the access list of {folder} cannot be read: {_OWN}"
    return None


def _windows_folder_problem(folder: Path) -> str | None:
    try:
        me = my_sid()
        owner, grants = folder_grants(folder)
    except OSError as exc:
        return f"the access list of {folder} cannot be read ({described(exc)})"
    trusted = {me, SYSTEM, ADMINISTRATORS}
    if owner not in trusted:  # its owner may rewrite its access list at will, whatever the list says now
        return (
            f"{folder} belongs to another account ({owner or 'no owner'}), which may open it whatever its access list "
            f'says: make it yours, `icacls "{folder}" /setowner *{me}`, or use a data folder of your own '
            "(LATET_DATA_DIR)"
        )
    for grant in grants:
        if grant.sid in trusted or grant.sid == OWNER_RIGHTS or grant.sid.startswith(_APP_SIDS):
            continue
        if grant.sid == CREATOR_OWNER and grant.inherit_only:
            continue
        who = account_name(grant.sid) if grant.sid else "an entry of a kind Latet does not read"
        return (
            f"{folder} is open to other users of this computer (its access list allows {who}, beside your account, "
            f'SYSTEM and the Administrators): make it yours alone, `icacls "{folder}" /inheritance:r /grant:r '
            f"*{me}:(OI)(CI)F *{SYSTEM}:(OI)(CI)F *{ADMINISTRATORS}:(OI)(CI)F`, then remove any other entry it "
            "lists; or use a data folder in your profile (LATET_DATA_DIR)"
        )
    return None


if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    _ENVIRONMENT_KEYS = {
        "machine": (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        "user": (winreg.HKEY_CURRENT_USER, "Environment"),
    }

    def _read_environment(scope: str) -> str | None:
        """LATET_TRUSTED_ACCOUNTS in the registry's ``scope`` environment, opened for reading alone: its text as set
        (a %variable% in it is never expanded: that would read a process's environment), or None when it is not set.
        Raises OSError when it cannot be read, or holds no text."""
        hive, key = _ENVIRONMENT_KEYS[scope]
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_QUERY_VALUE) as opened:
                value, kind = winreg.QueryValueEx(opened, TRUSTED_ACCOUNTS)
        except FileNotFoundError:
            return None
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
            raise OSError("it holds no text")
        return value

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION, _DACL_SECURITY_INFORMATION = 0x1, 0x4
    _TOKEN_QUERY, _TOKEN_USER = 0x0008, 1
    _ALLOWED = (0x00, 0x09)  # ACCESS_ALLOWED_ACE and ACCESS_ALLOWED_CALLBACK_ACE: the SID follows the mask
    _DENIED = (0x01, 0x06, 0x0A, 0x0C)  # what denies takes nothing away from what is refused here
    _INHERIT_ONLY, _INHERITED = 0x08, 0x10
    _EVERYONE_ALL = Grant("S-1-1-0", 0x1F01FF, False)  # a folder with no access list: anyone, everything

    class _Acl(ctypes.Structure):
        _fields_ = (
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort),
            ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        )

    class _AceHeader(ctypes.Structure):
        _fields_ = (("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort))

    @functools.cache
    def _windows() -> tuple[Any, Any]:
        """advapi32 and kernel32, with the signatures of the few functions read here."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(ctypes.c_void_p)
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer, pointer,
        ]  # fmt: skip
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
        advapi32.GetAce.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]  # fmt: skip
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer]
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ]  # fmt: skip
        advapi32.LookupAccountSidW.restype = wintypes.BOOL
        advapi32.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ]  # fmt: skip
        advapi32.LookupAccountNameW.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return advapi32, kernel32

    def _sid_text(sid: int | None) -> str:
        advapi32, kernel32 = _windows()
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value or ""
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    @functools.cache
    def _account(sid: str) -> str:
        """``DOMAIN\\name`` of a SID, as Windows looks it up here (a local account, a group, a domain's when its
        controller answers); "" when it has no name. Raises OSError when it cannot be looked up."""
        advapi32, kernel32 = _windows()
        binary = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(binary)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            names, domains, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not advapi32.LookupAccountSidW(
                None, binary, name, ctypes.byref(names), domain, ctypes.byref(domains), ctypes.byref(use)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            kernel32.LocalFree(binary)

    def canonical_sid(text: str) -> str:
        """A SID written ``S-1-…`` as Windows writes it back (``s-1-5-32-544`` is ``S-1-5-32-544``). Raises OSError
        when Windows reads no SID in it."""
        advapi32, kernel32 = _windows()
        binary = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(text, ctypes.byref(binary)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return _sid_text(binary.value)
        finally:
            kernel32.LocalFree(binary)

    # What LookupAccountNameW may name that is an account or a group: a user, a domain's group, a local group (alias),
    # a well-known group, a computer. A domain, a deleted account, a label or a logon session is none.
    _ACCOUNT_USES = (1, 2, 4, 5, 9)

    _looked_up: dict[str, str | OSError] = {}

    def account_sid(name: str) -> str:
        """The SID of the account or group ``name`` (``COMPUTER\\name``, ``DOMAIN\\name`` or ``name``) as Windows
        looks it up here (``_lookup_account``). Each name is looked up once per process, whether Windows finds it or
        not: a domain whose controller does not answer may take seconds each time, within the helper's deadline. Raises
        OSError when Windows finds none, or finds what is not an account or a group (a domain)."""
        if name not in _looked_up:
            try:
                _looked_up[name] = _lookup_account(name)
            except OSError as exc:
                _looked_up[name] = exc
        found = _looked_up[name]
        if isinstance(found, OSError):
            raise OSError(*found.args)
        return found

    def _lookup_account(name: str) -> str:
        """``LookupAccountNameW``: a local account, a group, a domain's when its controller answers."""
        advapi32, _ = _windows()
        size, domains, use = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD()
        advapi32.LookupAccountNameW(
            None, name, None, ctypes.byref(size), None, ctypes.byref(domains), ctypes.byref(use)
        )
        error = ctypes.get_last_error()
        if error != 122 or not 8 <= size.value <= 68:  # ERROR_INSUFFICIENT_BUFFER, with a SID's size: it was found
            raise ctypes.WinError(error or 1332)  # ERROR_NONE_MAPPED
        sid = ctypes.create_string_buffer(size.value)
        domain = ctypes.create_unicode_buffer(max(domains.value, 1))
        if not advapi32.LookupAccountNameW(
            None, name, sid, ctypes.byref(size), domain, ctypes.byref(domains), ctypes.byref(use)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if use.value not in _ACCOUNT_USES:
            raise OSError(f"it names no account or group (a kind {use.value} of name)")
        return _sid_text(ctypes.addressof(sid))

    @functools.cache
    def _netapi() -> Any:
        """netapi32, with the signatures of the two functions read here."""
        netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
        netapi32.NetLocalGroupGetMembers.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t),
        ]  # fmt: skip
        netapi32.NetLocalGroupGetMembers.restype = wintypes.DWORD
        netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
        netapi32.NetApiBufferFree.restype = wintypes.DWORD
        return netapi32

    @functools.cache
    def administrators_members() -> tuple[str, ...]:
        """The SIDs of the direct members of the local Administrators group (``NetLocalGroupGetMembers``, level 0),
        nested groups not followed. The group is the one whose SID is S-1-5-32-544: its name, which Windows translates
        (Administratoren, Administrateurs), is the one Windows gives that SID here, never one written in Latet. Read
        once per process and kept for its life: a member removed since is still trusted by a process that started
        before, until it ends. Raises OSError when they cannot be read (and then they are read again when asked)."""
        netapi32 = _netapi()
        group = _account(ADMINISTRATORS).rpartition("\\")[2]
        if not group:
            raise OSError("the Administrators group has no name here")
        members: list[str] = []
        resume = ctypes.c_size_t(0)
        for _ in range(10_000):  # each call gives what fits; a computer has not that many members to give
            buffer, read, total = ctypes.c_void_p(), wintypes.DWORD(), wintypes.DWORD()
            status = netapi32.NetLocalGroupGetMembers(
                None, group, 0, ctypes.byref(buffer), 0xFFFFFFFF, ctypes.byref(read), ctypes.byref(total),
                ctypes.byref(resume),
            )  # fmt: skip
            try:
                if status not in (0, 234):  # NERR_Success, ERROR_MORE_DATA
                    raise OSError(f"NetLocalGroupGetMembers refused, status {status}")
                if read.value and not buffer.value:
                    raise OSError("NetLocalGroupGetMembers gave no members' list")
                entries = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))  # LOCALGROUP_MEMBERS_INFO_0: a SID
                members += [_sid_text(entries[index]) for index in range(read.value)]
            finally:
                if buffer.value:
                    netapi32.NetApiBufferFree(buffer)
            if status == 0:
                return tuple(dict.fromkeys(members))
        raise OSError("NetLocalGroupGetMembers did not finish")

    @functools.cache
    def my_sid() -> str:
        """The SID of the account this process runs as."""
        advapi32, kernel32 = _windows()
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return _sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])  # TOKEN_USER starts with the SID
        finally:
            kernel32.CloseHandle(token)

    def folder_grants(folder: Path) -> tuple[str | None, list[Grant]]:
        """The owner of ``folder`` (a SID, None when it has none) and the entries of its access list that allow, read by
        SID. Raises OSError when they cannot be read."""
        advapi32, kernel32 = _windows()
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = advapi32.GetNamedSecurityInfoW(
            str(folder), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )  # fmt: skip
        if error:
            raise ctypes.WinError(error)
        try:
            who = _sid_text(owner.value) if owner.value else None
            if not dacl.value:
                return who, [_EVERYONE_ALL]
            acl = _Acl.from_address(dacl.value)
            if acl.AclRevision not in (2, 4) or acl.AclSize < ctypes.sizeof(_Acl):
                raise OSError("invalid access list layout")
            end, previous = dacl.value + acl.AclSize, dacl.value + ctypes.sizeof(_Acl)
            if acl.AceCount > (end - previous) // ctypes.sizeof(_AceHeader):
                raise OSError("invalid access list layout")
            grants = []
            for index in range(acl.AceCount):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise ctypes.WinError(ctypes.get_last_error())
                if ace.value < previous or ace.value + ctypes.sizeof(_AceHeader) > end:
                    raise OSError("invalid access entry layout")
                header = _AceHeader.from_address(ace.value)
                if header.AceSize < ctypes.sizeof(_AceHeader) or ace.value + header.AceSize > end:
                    raise OSError("invalid access entry layout")
                previous = ace.value + header.AceSize
                if header.AceType not in (*_ALLOWED, *_DENIED):
                    grants.append(Grant("", 0, bool(header.AceFlags & _INHERIT_ONLY)))
                    continue  # Unknown layouts have no safely decoded mask/SID; every caller refuses this sentinel.
                offset = 8
                if header.AceType in (0x06, 0x0C):  # Object-deny ACE: optional GUIDs precede its SID.
                    if header.AceSize < 12:
                        raise OSError("invalid access entry layout")
                    flags = wintypes.DWORD.from_address(ace.value + 8).value
                    if flags & ~0x3:
                        raise OSError("invalid access entry layout")
                    offset = 12 + 16 * bool(flags & 0x1) + 16 * bool(flags & 0x2)
                if header.AceSize < offset + 8:
                    raise OSError("invalid access entry layout")
                revision = ctypes.c_ubyte.from_address(ace.value + offset).value
                count = ctypes.c_ubyte.from_address(ace.value + offset + 1).value
                if revision != 1 or count > 15 or header.AceSize < offset + 8 + 4 * count:
                    raise OSError("invalid access entry layout")
                if header.AceType in _DENIED:
                    continue
                mask = wintypes.DWORD.from_address(ace.value + 4).value
                sid = _sid_text(ace.value + 8) if header.AceType in _ALLOWED else ""
                inherit_only, inherited = bool(header.AceFlags & _INHERIT_ONLY), bool(header.AceFlags & _INHERITED)
                grants.append(Grant(sid, mask, inherit_only, inherited))
            return who, grants
        finally:
            kernel32.LocalFree(descriptor)

else:

    def _account(sid: str) -> str:
        raise OSError("SIDs are Windows'")

    def my_sid() -> str:
        raise OSError("SIDs are Windows'")

    def canonical_sid(text: str) -> str:
        raise OSError("SIDs are Windows'")

    def account_sid(name: str) -> str:
        raise OSError("SIDs are Windows'")

    def administrators_members() -> tuple[str, ...]:
        raise OSError("local groups are Windows'")

    def _read_environment(scope: str) -> str | None:
        raise OSError("the registry is Windows'")

    def folder_grants(folder: Path) -> tuple[str | None, list[Grant]]:
        raise OSError("access lists are Windows'")
