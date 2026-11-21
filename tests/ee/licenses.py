"""Licenses for the tests, made as the licensor's script makes them (make_license.py): the payload's canonical JSON
signed with Ed25519, the signature in base64. The key pair is the tests' own. And a license store in memory, which keeps
what a server's database keeps."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from latet.ee.license import Stored

PAYLOAD: dict[str, Any] = {
    "v": 1,
    "id": "L-2027-0001",
    "licensee": "Acme Corp",
    "users": 300,
    "features": ["locked-rules", "approvals", "reports", "siem", "term-sync", "sso", "roles"],
    "issued": "2026-11-01",
    "expires": "2027-10-31",
    "kind": "subscription",
}
# A trial of 32 days, both ends included, for tests with a clock of their own (its last day 2026-12-02); and one that
# ended whatever the calendar says, which a test writes to its store (an ended license is not installed).
TRIAL: dict[str, Any] = {"kind": "trial", "issued": "2026-11-01", "expires": "2026-12-02"}
ENDED_TRIAL: dict[str, Any] = {"kind": "trial", "issued": "2020-01-01", "expires": "2020-02-01"}


def at(day: str, time: str = "12:00:00") -> float:
    """The moment ``day`` ``time`` in UTC, as a clock answers it."""
    return datetime.fromisoformat(f"{day}T{time}+00:00").timestamp()


class Clock:
    """A clock the test moves: a license's clock (``at``), or the seconds an instance keeps its copies."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class Signer:
    """The licensor, in the tests: a key pair of its own, and licenses signed with it."""

    key_id: str = "test"
    private: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    @property
    def public(self) -> bytes:
        return self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def keys(self) -> dict[str, bytes]:
        return {self.key_id: self.public}

    def document(self, **changes: Any) -> dict[str, Any]:
        """A license as a JSON document: PAYLOAD with ``changes`` (None takes a key away)."""
        payload = {k: v for k, v in {**PAYLOAD, **changes}.items() if v is not None}
        signed = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        signature = base64.b64encode(self.private.sign(signed)).decode()
        return {"payload": payload, "key": self.key_id, "signature": signature}

    def make(self, **changes: Any) -> bytes:
        """A license file, as the script writes it: indented, in UTF-8."""
        return json.dumps(self.document(**changes), indent=2, ensure_ascii=False).encode("utf-8")


class MemoryLicenses:
    """A license store in memory, as a server's database keeps one: the document, the features the installer verified
    and its version, since when more users than bought are counted, and the trials that ended, which removing the
    license keeps. ``today`` is the store's own clock, as a server's PostgreSQL has one: a trial is kept as ended only
    when it agrees (None: it always does)."""

    def __init__(self) -> None:
        self.data: bytes | None = None
        self.features: frozenset[str] | None = None
        self.version = 0
        self.since: float | None = None
        self.ended: set[str] = set()
        self.today: date | None = None

    def ended_trials(self) -> frozenset[str]:
        return frozenset(self.ended)

    def keep_trial_ended(self, license_id: str, last_day: date) -> bool:
        if self.today is None or self.today > last_day:
            self.ended.add(license_id)
        return license_id in self.ended

    def read(self) -> Stored | None:
        """The license, its version the write's number and the digest of the document, as the server's: a document a
        test puts in ``data`` by hand (written in the database by hand) is read as it is, never as the one before."""
        if self.data is None:
            return None
        digest = hashlib.sha256(self.data).hexdigest()
        return Stored(self.data, "server", ("memory", self.version, digest), features=self.features)

    def write(self, data: bytes, features: frozenset[str] | None = None) -> None:
        self.data, self.features, self.version = data, features, self.version + 1

    def delete(self) -> bool:
        had = self.data is not None
        self.data, self.features, self.version = None, None, self.version + 1
        return had

    def over_since(self) -> float | None:
        return self.since

    def keep_over_since(self, moment: float | None) -> float | None:
        if moment is None or self.since is None:  # the first count over is kept; a count under clears it
            self.since = moment
        return self.since
