"""The backends of an identity's space on a Latet server (E0a's ``StateBackends``): PostgreSQL for what lasts, Valkey
for what is shared and short-lived, sealed with the identity's root key (its AWS sessions with the master key), and the
server's sign-ins, whose subject is the identity.

A change the other instances must apply (a value kept, its rule, a value forgotten, the identity's settings) bumps a
change count in PostgreSQL in its own transaction (the vault's, owner the identity's id; the settings', owner
``<id>:settings``), and is announced on ``identity:<id>`` once committed: ``{"kind": "entity" | "rule" | "forgotten",
"id", "from", "counter"}`` or ``{"kind": "settings", "from", "counter"}``, ``from`` naming the space that made it (its
instance's id and its own) and ``counter`` the count. A message holds ids, kinds and counts, never a value: Valkey's
readers see it. An announcement that Valkey fails is said in the log by its error's type, never raised into the change,
which has taken effect: the other instances find it by the count (the beat's check, ``Spaces.check``). Every count the
space's stores bump, told or not, is handed to ``counted`` with what it counts (``vault`` or ``settings``), so that the
space knows which changes it applied itself.

Who calls ``space_backends``: the thread that builds a space (it makes no round trip itself)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from latet.server.history import PgHistoryStore
from latet.server.live import (
    SpaceJobs,
    ValkeyAwsSessions,
    ValkeyConversations,
    ValkeyFeed,
    ValkeyGiven,
    ValkeySentBlocks,
)
from latet.server.queue import ValkeyApprovals
from latet.server.space_settings import PgSettings
from latet.server.vault import PgVaultStore
from latet.state import StateBackends

if TYPE_CHECKING:
    from latet.server.app import Server

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpaceBackends:
    """A space's backends, those of them the instance's ear hands messages to, and the vault store (its count)."""

    state: StateBackends
    conversations: ValkeyConversations
    approvals: ValkeyApprovals
    feed: ValkeyFeed
    approvals_feed: ValkeyFeed
    vault: PgVaultStore
    given: ValkeyGiven


def space_backends(
    server: Server,
    identity_id: str,
    root_key: bytes,
    *,
    origin: str | None = None,
    counted: Callable[[str, int], None] | None = None,
    stale: Callable[[], None] | None = None,
    kind: Literal["person", "key"] = "person",
) -> SpaceBackends:
    """The backends of ``identity_id``'s space, its stores sealed with ``root_key``. ``origin``: the space's name in
    what it announces (its instance's id when none is given); ``counted``: told every count its stores bump, with what
    it counts (``vault`` or ``settings``); ``stale``: called when a save of its settings was refused as stale (outside
    the save's transaction, under the runtime's lock: it hands the re-application over, it does not make it); ``kind``:
    an API key's settings keep its approved own part (``PgSettings``)."""
    source = origin or server.instance
    count = counted or (lambda _what, _counter: None)

    def changed(what: str, entity_id: str | None, counter: int) -> None:  # the vault's
        count("vault", counter)
        announce({"kind": what, "id": entity_id, "from": source, "counter": counter})

    def announce(message: dict[str, Any]) -> None:
        if message.get("id") is None:
            message.pop("id", None)
        try:
            server.bus.publish(f"identity:{identity_id}", message)
        except Exception as exc:  # it took effect: the other instances find it by its count
            log.warning(
                "a change to the space of %s could not be told to the other instances (%s): they find it by its count",
                identity_id,
                type(exc).__name__,
            )

    conversations = ValkeyConversations(server.bus, identity_id, root_key)
    approvals = ValkeyApprovals(server.bus, server.db, identity_id, root_key, instance=server.instance)
    feed = ValkeyFeed(server.bus, f"feed:{identity_id}", root_key)
    # The Live Reviewer's: a batch's "pending" names the batch, which an instance with viewers reads from the queue, in
    # the queue's thread (a record weighs 4 MiB: never on the event loop).
    approvals_feed = ValkeyFeed(server.bus, f"approvals:{identity_id}", root_key, batches=approvals.fetch)
    vault = PgVaultStore(server.db, identity_id, root_key, changed=changed, limit=server.settings.vault_limit)
    given = ValkeyGiven(server.bus, identity_id, root_key)
    settings = PgSettings(
        server.db, server.shared, identity_id, root_key, stale=stale, kind=kind,
        changed=lambda counter: announce({"kind": "settings", "from": source, "counter": counter}),
        counted=lambda counter: count("settings", counter),
    )  # fmt: skip
    state = StateBackends(
        vault=vault,
        history=PgHistoryStore(server.db, identity_id, root_key, limit=server.settings.history_limit_mb * 1024 * 1024),
        conversations=conversations,
        sent=ValkeySentBlocks(server.bus, identity_id, root_key),
        given=given,
        approvals=approvals,
        feed=feed,
        approvals_feed=approvals_feed,
        sign_ins=server.sign_ins,
        settings=settings,
        jobs=SpaceJobs(server.bus, identity_id),
        aws=ValkeyAwsSessions(server.bus, server.master, identity_id, clock=server.clock),
    )
    return SpaceBackends(state, conversations, approvals, feed, approvals_feed, vault, given)
