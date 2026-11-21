"""Stand-ins for what term sync is given on a server: the license, a clock, Valkey's lease (E4's MemoryLocks), the
messages, the event log, the sources and their secrets. One MemoryLocks is shared by the instances of a test."""

from latet.ee.settings import TermSyncSettings
from latet.ee.sync.service import TermSync
from latet.ee.sync.store import MemoryTermStore, term_id

START = 1_800_000_000.0


class FakeLicense:
    def __init__(self) -> None:
        self.licensed_ = self.producing_ = self.knows_ = True

    def licensed(self, feature):
        return self.licensed_

    def producing(self, feature):
        return self.producing_

    def knows(self):
        return self.knows_


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self):
        return self.now


class Published(list):
    def __call__(self, channel, message):
        self.append((channel, dict(message)))
        return True


def id_of(store, source: str, etype: str, value: str) -> str:
    """The id a term has, or would have, in ``store``, keyed with the store's own key of the ids: in memory its own; on a
    server the one its first run made, sealed in ee_terms_state (`LookupError` before it). The tests' alone: no route
    or run asks a term's id but by its rows (the final review's M3)."""
    if isinstance(store, MemoryTermStore):
        return store.term_id(source, etype, value)
    from latet.ee.sync.pgstore import _sealed_ids_key

    with store._db.connection() as conn:
        sealed = _sealed_ids_key(conn)
    if sealed is None:
        raise LookupError("no synced term has been kept yet: the key of their ids is made by the first run")
    return term_id(store._opened_ids_key(sealed), source, etype, value)


def sources(*settings) -> TermSyncSettings:
    return TermSyncSettings.model_validate({"sources": list(settings)})


def make_sync(store=None, *, locks, licensing=None, settings=None, events=None, readers=None, secrets=None, clock=None,
              published=None):  # fmt: skip
    """One instance's term sync, on ``store`` (one in memory when not given) and ``locks``; ``settings`` a dict holding
    the sources (``settings["now"]``), shared by the instances of a test as the Enterprise settings are."""
    held = settings if settings is not None else {"now": TermSyncSettings()}
    kept = secrets if secrets is not None else {}
    return TermSync(
        store if store is not None else MemoryTermStore(),
        settings=lambda: held["now"],
        secret=lambda source: kept.get(source.name),
        licensing=licensing or FakeLicense(),
        lease=locks.lease,
        publish=published if published is not None else Published(),
        record=(events if events is not None else []).append,
        readers=readers or {},
        clock=clock or locks.clock,
    )
