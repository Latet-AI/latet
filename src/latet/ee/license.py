"""The Latet Enterprise license: a JSON file signed with Ed25519 by the licensor, checked here with the public keys
Latet carries. Nobody is asked anything: no activation, no call home.

A license names who bought it, how many users it covers, the features it switches on, its last day and its kind (a
subscription, or a trial of 32 days at most). Latet weighs it into a standing: valid; expiring (from 30 days before
the end; a trial from 7 days before its last day, and again from 1 day before); grace (ended, or more users than
bought: everything keeps working for 30 days; a trial has none); paused (what produces something new stops: reports,
events to the company's security systems, term sync); trial-ended (a trial past its last day: what produces stops,
and the Enterprise management with it: ``manageable`` and ``serving`` answer no, and what was set can be read, not
changed); invalid (a changed, unreadable or foreign file: what manages, produces or decides stops, as for a license
that ended, and what protects keeps the features the instance that installed it verified, else the last license
verified). What protects never depends on the days, the count or a file that cannot be used: a floor, rules for
everyone, where changes go stay on while the license in use, or the last one verified, lists them (``licensed``), a
trial's end included. For a paid license, what lets people in or decides for the company stays on too (``serving``).

The license is a Latet server's: its PostgreSQL keeps it (``latet.ee.server.DatabaseLicenses``), with, for every
instance, since when more users than bought are counted and which trials ended, so that a clock moved back revives
none."""

from __future__ import annotations

import base64
import binascii
import codecs
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from latet.logline import Paced

log = logging.getLogger(__name__)
# A store that does not answer fails at each look of what protects: said once a minute.
_UNANSWERED = Paced()

FEATURES: Final = ("locked-rules", "approvals", "reports", "siem", "term-sync", "sso", "roles")
# The licensor's public keys by id (32 bytes each): a license names the key that signed it, so a new key comes in a
# release while the licenses signed with an older one keep working.
PUBLIC_KEYS: dict[str, bytes] = {
    "2026a": base64.b64decode("d9W9nStS3SmB2Y5CnSW6+z6Fqcp6oSvMCQHY0GmtAIo="),
}
# A key's id as the licensor's script makes one: a refusal names the key only when its id has this shape.
KEY_ID: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
NOTICE_DAYS: Final = 30
TRIAL_DAYS: Final = 32  # a trial's longest run, its first and last days included
TRIAL_WARNING_DAYS: Final = (7, 1)  # a trial is warned from 7 days before its last day, then from 1 day before
GRACE_DAYS: Final = 30
USERS_NOTICE: Final = 0.9
MAX_BYTES: Final = 64 * 1024  # a license is about half a kilobyte
DAY: Final = 86400.0
# How long what a server's store said is taken as said: a store that would not keep a trial as ended (its own clock
# says the trial runs) is asked again after it, not at every look; a store that failed is asked again by what
# protects after it.
REFRESH_SECONDS: Final = 10.0

Source = Literal["server"]
_CHANGED: Final = "its signature does not match: the file was changed after it was made"
# A file saved again keeps its text under a byte order mark: Notepad's UTF-8, Windows PowerShell's UTF-16. UTF-32's
# come first, as UTF-16 LE's is the start of UTF-32 LE's.
_BOMS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


class LicenseError(ValueError):
    """Why a file is no license Latet can use. ``code`` (unreadable, format, no-keys, key, signature, version,
    content, ended) lets the dashboard say it in the reader's language; the message says it in English, never quoting
    what the file holds."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NotLicensed(Exception):
    """A feature the license in use does not switch on: the API answers it with 403 and this message."""

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"{feature} needs a Latet Enterprise license that includes it: `latet server license show` says which is "
            "installed"
        )
        self.feature = feature


class TrialEnded(Exception):
    """A Latet Enterprise trial that ended: a change of the Enterprise administration, and what lets people in or
    decides for the company, says so and does nothing (the API answers 403 with ``code: "trial-ended"``); a read of
    the Enterprise administration still answers, and what protects is unchanged."""

    def __init__(self, ended_on: date) -> None:
        super().__init__(
            f"the Latet Enterprise trial ended on {ended_on.isoformat()}: install a license, or remove the trial's "
            "file to go back to the Community Edition; protection is unchanged"
        )
        self.ended_on = ended_on


@dataclass(frozen=True)
class License:
    id: str
    licensee: str
    users: int
    features: frozenset[str]
    issued: date
    expires: date
    trial: bool = False  # the payload's kind: "trial", else "subscription"


class State(StrEnum):
    NONE = "none"
    VALID = "valid"
    EXPIRING = "expiring"
    GRACE = "grace"
    PAUSED = "paused"
    TRIAL_ENDED = "trial-ended"
    INVALID = "invalid"


_SEVERITY: Final = {State.VALID: 0, State.EXPIRING: 1, State.GRACE: 2, State.PAUSED: 3, State.TRIAL_ENDED: 4}
# The standings that may change the Enterprise settings: valid, ending soon, or in its grace.
MANAGEABLE: Final = (State.VALID, State.EXPIRING, State.GRACE)


@dataclass(frozen=True)
class Standing:
    """A license weighed now. ``notices``: codes for admins (expiring, grace, paused, users-near, users-over, invalid);
    ``source``: where it was read; ``reason`` and ``reason_code``: why a file cannot be used; ``users_in_use``: the
    people and API keys counted on a server, None where nothing counts them."""

    state: State
    license: License | None
    users_in_use: int | None
    days_left: int | None
    over_since: float | None
    reason: str | None
    notices: tuple[str, ...]
    source: Source | None = None
    reason_code: str | None = None

    def pauses_on(self) -> date | None:
        """The day what produces something new pauses, while a grace runs; None otherwise."""
        if self.state is not State.GRACE or self.license is None:
            return None
        days = []
        if self.days_left is not None and self.days_left < 0:
            days.append(self.license.expires + timedelta(days=GRACE_DAYS + 1))
        if self.over_since is not None:
            days.append(utc_day(self.over_since + GRACE_DAYS * DAY))
        return min(days, default=None)


def utc_day(moment: float) -> date:
    """The day of ``moment`` in UTC: a license's days are UTC days, the same on every computer."""
    return datetime.fromtimestamp(moment, UTC).date()


def canonical(payload: Mapping[str, Any]) -> bytes:
    """What a license's signature is made on: its payload as JSON with sorted keys and no spaces, in UTF-8. The file's
    own layout, line ends and key order do not count; one character changed does."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def file_text(data: bytes) -> str:
    """A license file's text, in the encoding its byte order mark names (UTF-8, UTF-16 or UTF-32, either byte order),
    else UTF-8. The signature covers the payload, not the file's bytes, so this accepts nothing changed. ValueError when
    the bytes are not that text."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding)
    return data.decode("utf-8")


def not_a_license(why: str) -> LicenseError:
    """The refusal of what is not a license file, saying ``why`` and never quoting what it holds."""
    return LicenseError("format", f"it is not a license file: {why}")


def _sayable(version: object) -> bool:
    """A version a refusal may say back: a whole number of a few digits, never a text, a bool or a long number."""
    return isinstance(version, int) and not isinstance(version, bool) and 0 <= version < 1_000_000


def parse_license(data: bytes, keys: Mapping[str, bytes] = PUBLIC_KEYS) -> License:
    """The license in ``data``, checked with ``keys``. LicenseError says why it is none, and never quotes what the
    file holds (only a key's id of the licensor's shape, or a version that is a small whole number)."""
    if len(data) > MAX_BYTES:
        raise not_a_license(f"it is larger than {MAX_BYTES // 1024} KB")
    try:
        document = json.loads(file_text(data))  # a BOM, as Notepad or PowerShell writes one, is not part of it
    except RecursionError:  # arrays or objects nested deeper than Python reads
        raise not_a_license("it nests too deeply") from None
    except ValueError:  # not text in the encoding its BOM names, or not JSON
        raise not_a_license("it is not JSON") from None
    payload = document.get("payload") if isinstance(document, dict) else None
    key_id = document.get("key") if isinstance(document, dict) else None
    signature = document.get("signature") if isinstance(document, dict) else None
    if not isinstance(payload, dict) or not isinstance(key_id, str) or not isinstance(signature, str):
        raise not_a_license("it has no payload, key and signature")
    version = payload.get("v")
    if not _sayable(version) or version != 1:
        said = f"version {version}" if _sayable(version) else "another version"
        raise LicenseError(
            "version", f"it is a license of {said}, and this Latet reads version 1: install the latest release"
        )
    if not keys:
        raise LicenseError(
            "no-keys",
            "this copy of Latet carries no key to check licenses with: install Latet from its official release",
        )
    public = keys.get(key_id)
    if public is None:
        if KEY_ID.fullmatch(key_id):
            unknown = f"the key {key_id!r}, which this version of Latet does not know"
        else:
            unknown = "a key Latet does not know"
        raise LicenseError("key", f"it is signed with {unknown}: install the latest release")
    try:
        raw = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):  # not base64
        raise LicenseError("signature", _CHANGED) from None
    try:
        signed = canonical(payload)
    except RecursionError:
        raise not_a_license("it nests too deeply") from None
    except ValueError:  # a lone surrogate, written as JSON's escape of one: no text UTF-8 holds
        raise not_a_license("it holds text that is not Unicode") from None
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(raw, signed)
    except InvalidSignature:
        raise LicenseError("signature", _CHANGED) from None
    return _license(payload)


def _license(payload: Mapping[str, Any]) -> License:
    """A signed payload read: its fields checked (a licensor's mistake is said, not taken)."""

    def text(name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise LicenseError("content", f"its {name} is missing")
        return value.strip()

    def day(name: str) -> date:
        value = text(name)
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise LicenseError("content", f"its {name} is not a date (YYYY-MM-DD)") from None

    users = payload.get("users")
    if not isinstance(users, int) or isinstance(users, bool) or users < 1:
        raise LicenseError("content", "its number of users is not a whole number of at least 1")
    features = payload.get("features")
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise LicenseError("content", "its features are not a list of names")
    kind = payload.get("kind")
    if kind not in ("subscription", "trial"):  # required: a trial is never read as a paid license by a key left out
        raise LicenseError("content", "its kind is not subscription or trial")
    issued, expires = day("issued"), day("expires")
    if expires < issued:
        raise LicenseError("content", "it ends before it starts")
    if kind == "trial" and (expires - issued).days + 1 > TRIAL_DAYS:
        raise LicenseError("content", f"a trial runs {TRIAL_DAYS} days at most")
    return License(text("id"), text("licensee"), users, frozenset(features), issued, expires, kind == "trial")


# --- where a license is kept --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    """A license as the server's store keeps it: the file's bytes (None: it cannot be read, ``problem`` says why),
    where it is, and what changes when it changes (the row's version and the digest of the document).
    ``features``: those the instance that installed it verified (None where nothing said them), which an instance that
    cannot verify the file protects with."""

    data: bytes | None
    source: Source
    version: tuple[object, ...]
    problem: str | None = None
    features: frozenset[str] | None = None


class LicenseStore(Protocol):
    # ``conn`` (each method that takes it): a connection in the caller's transaction, read or written there.
    def read(self, *, conn: Any = None) -> Stored | None: ...  # the license in use; None: there is none
    # ``features``: those the installer verified, which the server keeps with the file (``Stored.features``).
    def write(self, data: bytes, features: frozenset[str] | None = None, *, conn: Any = None) -> None: ...
    def delete(self, *, conn: Any = None) -> bool: ...  # whether there was one
    def over_since(self, *, conn: Any = None) -> float | None: ...  # since when more users than bought are counted
    def keep_over_since(self, moment: float | None) -> float | None: ...  # the first kept until cleared: the one kept
    def ended_trials(self, *, conn: Any = None) -> frozenset[str]: ...  # kept as ended, whatever a clock says now
    def keep_trial_ended(self, license_id: str, last_day: date) -> bool: ...  # kept when the store's own clock agrees


# --- the license weighed ------------------------------------------------------------------------------------------


def _nobody() -> int | None:
    return None


def _verified(stored: Stored, keys: Mapping[str, bytes]) -> License:
    """The license ``stored`` holds, checked with ``keys``: LicenseError for a file that is none, whatever it holds."""
    if stored.data is None:
        raise LicenseError("unreadable", stored.problem or "the license cannot be read")
    try:
        return parse_license(stored.data, keys)
    except LicenseError:
        raise
    except Exception as exc:  # whatever a file holds, it never stops the standing: a file that is no license
        log.warning("a license file could not be read (%s): taken as no license file", type(exc).__name__)
    raise not_a_license("it cannot be read as one")


class Licensing:
    """The license of this server: kept by ``store``, checked and weighed. ``count_users``: the users the license
    counts (the server's enabled people and API keys), or None where nothing counts them; ``clock``: now, in seconds.

    Its lock guards what it keeps in memory alone (the license last checked and verified, the features last read,
    when the store last failed, a trial the store would not keep): it is never held while the store is read or
    written, nor while the users are counted, so that what protects (``licensed``) never waits behind a weighing or
    a change, and nothing that holds the lock ever waits for a server's row."""

    def __init__(
        self,
        store: LicenseStore,
        *,
        clock: Callable[[], float] = time.time,
        count_users: Callable[[], int | None] = _nobody,
        keys: Mapping[str, bytes] = PUBLIC_KEYS,
    ) -> None:
        self.store = store
        self.clock = clock
        self.count_users = count_users
        self.keys = keys
        self._lock = threading.Lock()
        self._parsed: tuple[object, License | LicenseError] | None = None
        # The last license this Licensing verified: what protects keeps it while the file cannot be used.
        self._verified: License | None = None
        # The features ``licensed`` last read from the store (none: no license there): what protects keeps them while
        # the store does not answer (a server's database down). None until it read any.
        self._read: frozenset[str] | None = None
        # When ``licensed`` last found the store failing (``clock``): it asks it again REFRESH_SECONDS later, not at
        # every look, answering meanwhile from what it last read.
        self._failed_at: float | None = None
        # A trial the store would not keep as ended (its own clock disagreed), and when: asked again after
        # REFRESH_SECONDS, not at every look.
        self._not_kept: tuple[str, float] | None = None

    def standing(self) -> Standing:
        return self._weighed(self.store.read())

    def licensed(self, feature: str) -> bool:
        """The license in use lists ``feature``: ended, over the users, a trial that ended too. What protects asks it,
        so it is weighed from the file alone (read and checked): no count of the users, no trial kept as ended, no
        days. While the file cannot be used (a document written by hand, a key of a newer release), the features the
        instance that installed it verified answer (the server's store keeps them with the file), else the last
        license this Licensing verified, even one removed before: such a file only ever adds protection. Only no
        license at all, or a valid license without ``feature``, switches it off. While the store does not answer (the
        server's database down), the features it last
        answered (else those of the last license verified) answer for REFRESH_SECONDS before it is asked again, the
        failure said by its type and never raised: a store that fails never lowers protection either. The store is
        read outside this Licensing's lock: it never waits behind a weighing."""
        now = self.clock()
        with self._lock:
            failed_at = self._failed_at
            if failed_at is not None and 0 <= now - failed_at < REFRESH_SECONDS:
                return feature in self._last_read()
        try:
            stored = self.store.read()
        except Exception as exc:  # by its type: its words can name the database's address
            if count := _UNANSWERED.due():
                log.warning(
                    "the license could not be read (%s): what protects keeps the features last read%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            with self._lock:
                self._failed_at = now
                return feature in self._last_read()
        features = self._features(stored)
        with self._lock:
            self._failed_at = None
            self._read = features
        return feature in features

    def knows(self) -> bool:
        """Whether ``licensed`` answers from a license store read (or a license verified) by this Licensing: False only
        while the store never answered since it was made (a server's database down since the instance started), when
        ``licensed`` answers no feature whatever is installed. What protects then builds nothing below a policy it
        cannot weigh (latet.ee.policy_server, L4)."""
        with self._lock:
            return self._read is not None or self._verified is not None

    def _last_read(self) -> frozenset[str]:
        """Under the lock: the features last read, else those of the last license verified, else none."""
        if self._read is not None:
            return self._read
        return self._verified.features if self._verified is not None else frozenset()

    def _features(self, stored: Stored | None) -> frozenset[str]:
        """The features ``licensed`` answers with for ``stored`` (M1)."""
        if stored is None:
            return frozenset()
        try:
            return self._parse(stored).features
        except LicenseError:
            pass
        with self._lock:
            verified = self._verified
        features = stored.features
        if features is None:
            features = verified.features if verified is not None else frozenset()
        return features

    def producing(self, feature: str) -> bool:
        """Licensed, and neither paused nor a trial that ended: reports, events to a SIEM, term sync."""
        standing = self.standing()
        lic = standing.license
        return lic is not None and feature in lic.features and standing.state not in (State.PAUSED, State.TRIAL_ENDED)

    def manageable(self) -> bool:
        """Enterprise settings may be changed: valid, ending soon, or in its grace; never paused, nor a trial that
        ended (what it set stays as it is, frozen, and can still be read), nor a file that cannot be used."""
        return self.standing().state in MANAGEABLE

    def standing_in(self, conn: Any) -> Standing:
        """A server's: the standing as the caller's transaction (``conn``) finds the license, its row held there
        (shared) until that transaction ends, so that no change of the license lands before the caller's change
        commits. Read past this instance's copy, and weighed without this Licensing's lock (nothing that holds the row
        waits for it) and keeping nothing: since when more users than bought are counted, and the trials kept as ended,
        are the row's as read there; what a look would keep is left to the next look."""
        return self._weighed(self.store.read(conn=conn), held=conn)

    def protected_in(self, conn: Any) -> frozenset[str]:
        """A server's: the features ``licensed`` answers with for the license as the caller's transaction (``conn``)
        finds it, read past this instance's copy and keeping nothing: the file's once it verifies; for a file this
        instance cannot verify (a key of a newer release, a row damaged), those the instance that installed it verified,
        which the store keeps with it, else those of the last license this Licensing verified (M1). What a change of
        the license stops is weighed from them (``latet.ee.admin._ends_the_policy``, the final security review's L2):
        what such a file keeps in force stops with the change all the same."""
        stored = self.store.read(conn=conn)
        if stored is None:
            return frozenset()
        try:
            return _verified(stored, self.keys).features
        except LicenseError:
            pass
        if stored.features is not None:
            return stored.features
        with self._lock:
            verified = self._verified
        return verified.features if verified is not None else frozenset()

    def serving(self, feature: str) -> bool:
        """Licensed, and not a trial that ended: what lets people in or decides for the company (sign-in with SSO, the
        decisions of central approvals, an Enterprise page's actions). A paid license's end never stops it."""
        standing = self.standing()
        lic = standing.license
        return lic is not None and feature in lic.features and standing.state is not State.TRIAL_ENDED

    def trial_ended_on(self) -> date | None:
        """The last day of the trial in use when it ended; None while none did."""
        standing = self.standing()
        return standing.license.expires if standing.state is State.TRIAL_ENDED and standing.license else None

    def check(self, data: bytes) -> License:
        """What ``install`` checks, and nothing kept: ``data`` is a license of this Latet's keys, and neither has it
        ended nor is it a trial the store keeps as ended (LicenseError)."""
        return self._check(data)

    def install(self, data: bytes, *, conn: Any = None) -> License:
        """Check ``data`` (and refuse a license that has ended, or a trial the store keeps as ended), then keep it in
        the store, with the features verified here. ``conn``: a server's, a connection in the caller's transaction, in
        which it is written (the caller drops the copies once it commits); this Licensing takes it as verified only
        when it reads it back, since that transaction may roll back.

        Checked and written without this Licensing's lock: a server's write waits for the license's row, which a
        transaction removing it holds, and nothing that holds that lock ever waits for a row, so that what protects
        (``licensed``) never waits behind a change."""
        lic = self._check(data, conn)
        if conn is not None:
            self.store.write(data, lic.features, conn=conn)
            return lic
        self.store.write(data, lic.features)
        with self._lock:
            self._verified = lic
        return lic

    def _check(self, data: bytes, conn: Any = None) -> License:
        """``install``'s check; given ``conn``, the trials kept as ended are read through it, as the caller's
        transaction finds them (never a second connection, nor this instance's copy)."""
        lic = parse_license(data, self.keys)
        today = utc_day(self.clock())
        ended = self.store.ended_trials() if conn is None else self.store.ended_trials(conn=conn)
        if lic.trial and (lic.expires < today or lic.id in ended):
            raise LicenseError(
                "ended", f"this trial ended on {lic.expires.isoformat()}: install a Latet Enterprise license"
            )
        if lic.expires < today:
            raise LicenseError("ended", f"it ended on {lic.expires.isoformat()}: install the license that follows it")
        return lic

    def remove(self, *, conn: Any = None) -> bool:
        """Take the installed license away; whether there was one. ``conn``: as ``install``'s. It holds no lock of this
        Licensing's: it changes nothing kept here, and a server's removal holds the license's row meanwhile."""
        return self.store.delete() if conn is None else self.store.delete(conn=conn)

    def _weighed(self, stored: Stored | None, *, held: Any = None) -> Standing:
        """The standing of ``stored``, the lock taken only for what is kept in memory; or, ``held`` (a connection in
        whose transaction the license's row was read and is held), nothing cached nor kept, the row's own keeps read
        through it."""
        if stored is None:
            return Standing(State.NONE, None, None, None, None, None, ())
        try:
            lic = self._parse(stored) if held is None else _verified(stored, self.keys)
        except LicenseError as exc:
            return Standing(State.INVALID, None, None, None, None, str(exc), ("invalid",), stored.source, exc.code)
        return self._weigh(lic, stored.source, held)

    def _parse(self, stored: Stored) -> License:
        """The license ``stored`` holds, checked once while it and the keys stay the same; the last verified kept.
        Under the lock: checking is reading bytes already read, never the store's."""
        marker = (stored.version, frozenset(self.keys.items()))
        with self._lock:
            parsed = self._parsed
            if parsed is None or parsed[0] != marker:
                result: License | LicenseError
                try:
                    result = _verified(stored, self.keys)
                except LicenseError as exc:
                    result = exc
                parsed = self._parsed = (marker, result)
            if isinstance(parsed[1], LicenseError):
                raise LicenseError(parsed[1].code, str(parsed[1]))
            self._verified = parsed[1]
            return parsed[1]

    def _weigh(self, lic: License, source: Source, held: Any = None) -> Standing:
        """``held``: as ``_weighed``'s."""
        now = self.clock()
        days_left = (lic.expires - utc_day(now)).days
        if lic.trial:
            expiry, notices = self._trial(lic, days_left, now, held)
        else:
            if days_left < -GRACE_DAYS:
                expiry = State.PAUSED
            elif days_left < 0:
                expiry = State.GRACE
            else:
                expiry = State.EXPIRING if days_left <= NOTICE_DAYS else State.VALID
            notices = [
                code for state, code in ((State.EXPIRING, "expiring"), (State.GRACE, "grace")) if expiry is state
            ]
        in_use, over_since = self._count(lic, now, held)
        users = State.VALID
        if in_use is not None and in_use > lic.users:
            notices.append("users-over")
            users = State.PAUSED if over_since is not None and now - over_since >= GRACE_DAYS * DAY else State.GRACE
        elif in_use is not None and in_use >= USERS_NOTICE * lic.users:
            notices.append("users-near")
        state = max(expiry, users, key=_SEVERITY.__getitem__)
        if state is State.PAUSED:
            notices.append("paused")
        over = over_since if users is not State.VALID else None
        return Standing(state, lic, in_use, days_left, over, None, tuple(notices), source)

    def _trial(self, lic: License, days_left: int, now: float, held: Any = None) -> tuple[State, list[str]]:
        """A trial's standing by its days, with no grace: warned from 7 days before its last day, again from 1 day
        before, ended from the day after. Its end is the license's signed last day alone (no time of installation, no
        highest time seen), and once the store keeps it (a server, when PostgreSQL's clock agrees) a clock moved back
        revives it nowhere. A store that would not keep it is asked again after REFRESH_SECONDS, not at every look.
        ``held``: the trials kept as the caller's transaction finds them, and nothing kept."""
        ended = self.store.ended_trials() if held is None else self.store.ended_trials(conn=held)
        kept = lic.id in ended
        if held is None and days_left < 0 and not kept and not self._lately_not_kept(lic.id, now):
            kept = self.store.keep_trial_ended(lic.id, lic.expires)
            with self._lock:
                self._not_kept = None if kept else (lic.id, now)
        if days_left < 0 or kept:
            return State.TRIAL_ENDED, ["trial-ended"]
        week, day = TRIAL_WARNING_DAYS
        if days_left <= day:
            return State.EXPIRING, ["trial-day"]
        if days_left <= week:
            return State.EXPIRING, ["trial-week"]
        return State.VALID, []

    def _lately_not_kept(self, license_id: str, now: float) -> bool:
        """The store would not keep this trial as ended less than REFRESH_SECONDS ago (after a clock moved back: no)."""
        with self._lock:
            not_kept = self._not_kept
        if not_kept is None or not_kept[0] != license_id:
            return False
        return 0 <= now - not_kept[1] < REFRESH_SECONDS

    def _count(self, lic: License, now: float, held: Any = None) -> tuple[int | None, float | None]:
        """How many users the license counts now, and since when more than it covers: the store keeps it, the first
        count over starting it and a count under clearing it. (None, None) where nothing counts them. ``held``: since
        when, as the caller's transaction finds it, and nothing kept."""
        try:
            in_use = self.count_users()
        except Exception as exc:  # a count that fails never stops Latet: nothing is said of the users this time
            log.warning("could not count the users (%s): nothing is said of them this time", type(exc).__name__)
            return None, None
        if in_use is None:
            return None, None
        if held is not None:
            kept = self.store.over_since(conn=held)
            return in_use, kept if in_use > lic.users else None
        kept = self.store.over_since()
        if in_use > lic.users:
            return in_use, kept if kept is not None else self.store.keep_over_since(now)
        if kept is not None:
            self.store.keep_over_since(None)
        return in_use, None


def require(licensing: Licensing, feature: str) -> None:
    """Raise NotLicensed unless the license in use switches ``feature`` on (ended or over the users too), or the last
    license verified did while the file cannot be used."""
    if not licensing.licensed(feature):
        raise NotLicensed(feature)


def require_serving(licensing: Licensing, feature: str) -> None:
    """Raise NotLicensed unless the license in use switches ``feature`` on, and TrialEnded once it is a trial that
    ended: what lets people in or decides for the company asks it first (sign-in with SSO, the decisions of central
    approvals, an Enterprise page's actions). A change of the Enterprise administration goes through
    ``latet.ee.api.managing_route`` instead, where reads still answer once a trial ended."""
    standing = licensing.standing()
    lic = standing.license
    if lic is None or feature not in lic.features:
        raise NotLicensed(feature)
    if standing.state is State.TRIAL_ENDED:
        raise TrialEnded(lic.expires)


# --- what people are told -----------------------------------------------------------------------------------------


def feature_order(feature: str) -> tuple[int, str]:
    """The features Latet knows in their order, then the others by name."""
    return (FEATURES.index(feature), "") if feature in FEATURES else (len(FEATURES), feature)


def describe(standing: Standing) -> tuple[Literal["ok", "warn", "fail"], str]:
    """How a license that is there stands, in a line: `latet server status`, `latet server license show`."""
    lic = standing.license
    if lic is None:
        return "fail", _unusable(standing)
    name = f"{lic.licensee} ({lic.id}{', trial' if lic.trial else ''})"
    if standing.state is State.TRIAL_ENDED:
        return "fail", (
            f"{name}: the trial ended on {lic.expires.isoformat()}; the Enterprise settings can be read, not changed, "
            "until a license is installed or the trial's file removed, protection is unchanged"
        )
    if standing.state in (State.GRACE, State.PAUSED):
        if "users-over" in standing.notices:
            why = f"{standing.users_in_use} users are in use (people and API keys) and the license covers {lic.users}"
        else:
            why = f"it ended on {lic.expires.isoformat()}"
        if standing.state is State.PAUSED:
            return "fail", f"{name}: {why}; reports, events and term sync are paused, protection is unchanged"
        pauses = standing.pauses_on()
        when = f" on {pauses.isoformat()}" if pauses else ""
        return "warn", f"{name}: {why}; reports, events and term sync pause{when}"
    in_use = "" if standing.users_in_use is None else f", {standing.users_in_use} in use"
    line = f"{name}: {lic.users} users{in_use}, until {lic.expires.isoformat()}"
    if standing.state is State.EXPIRING:
        days = standing.days_left or 0
        ends = f"it ends in {days} day{'' if days == 1 else 's'}" if days else "it ends today"
        if lic.trial:  # warned 7 days and 1 day before its last day: no grace follows
            ends += ", with no grace: then the Enterprise management stops, protection is unchanged"
        return "warn", f"{line}; {ends}"
    return ("warn" if "users-near" in standing.notices else "ok"), line


def _unusable(standing: Standing) -> str:
    """The line of a license file that cannot be used, as `latet server status` and `latet server license show` say
    it: every instance protects with the features the instance that installed the file verified, which the server
    keeps with it (``Stored.features``), whatever an instance's restart. ``unreadable`` is the server's database not
    answering (``EnterpriseEdition.standing``): the server's store always has the document's bytes when it answers."""
    if standing.source == "server" and standing.reason_code == "unreadable":
        return (
            f"the license could not be weighed: {standing.reason}. What manages, produces or decides for the company "
            "is off until it answers; what protects is as the features the instance that installed the license "
            "verified set it, which that database keeps for every instance: check that the server's PostgreSQL answers"
        )
    return (
        f"the license file cannot be used: {standing.reason}. What manages, produces or decides for the company is "
        "off; every instance keeps what protects as the features the instance that installed it verified set it "
        "(the server keeps them with the file), until the file is fixed: the server's administrators install it "
        "again (`latet server license install <file>`)"
    )


def summary(standing: Standing) -> dict[str, Any] | None:
    """What /me says of the license (None without one): enough for the menu and the notice bar."""
    if standing.state is State.NONE:
        return None
    lic = standing.license
    pauses = standing.pauses_on()
    return {
        "state": standing.state.value,
        "licensee": lic.licensee if lic else None,
        "users": lic.users if lic else None,
        "users_in_use": standing.users_in_use,
        "expires": lic.expires.isoformat() if lic else None,
        "days_left": standing.days_left,
        "trial": lic.trial if lic else False,
        "notices": list(standing.notices),
        "pauses_on": pauses.isoformat() if pauses else None,
    }


def details(standing: Standing) -> dict[str, Any]:
    """What the License page and the administration API are told: the standing in full."""
    lic = standing.license
    pauses = standing.pauses_on()
    return {
        "state": standing.state.value,
        "license": None
        if lic is None
        else {
            "id": lic.id,
            "licensee": lic.licensee,
            "users": lic.users,
            "features": sorted(lic.features, key=feature_order),
            "issued": lic.issued.isoformat(),
            "expires": lic.expires.isoformat(),
            "trial": lic.trial,
        },
        "users_in_use": standing.users_in_use,
        "days_left": standing.days_left,
        "over_since": standing.over_since,
        "pauses_on": pauses.isoformat() if pauses else None,
        "reason": standing.reason,
        "reason_code": standing.reason_code,
        "notices": list(standing.notices),
        "source": standing.source,
    }


def standing_of(data: Mapping[str, Any]) -> Standing:
    """The standing ``details`` described: what `latet server license` reads from the administration API."""
    raw = data.get("license")
    lic = None
    if raw is not None:
        lic = License(
            raw["id"], raw["licensee"], raw["users"], frozenset(raw["features"]),
            date.fromisoformat(raw["issued"]), date.fromisoformat(raw["expires"]), raw["trial"],
        )  # fmt: skip
    return Standing(
        State(data["state"]), lic, data["users_in_use"], data["days_left"], data["over_since"], data["reason"],
        tuple(data["notices"]), data["source"], data["reason_code"],
    )  # fmt: skip
