"""Term sync on a Latet server of two instances sharing PostgreSQL and Valkey: terms from a file, Entra ID and LDAP
masked in every space of every instance, a term gone from its source masked until it is removed, a sync asked for run
where the lease is and heard by the other instance at once, the sources, the license and a refresh heard at once, a
secret kept for another place never opened, a dead holder's lease ending, an instance started later masking what was
synced before, and the messages holding no term."""

import functools
import threading
import time
import uuid

import httpx
import pytest

from latet.edition import server_context
from latet.ee import edition as edition_module
from latet.ee.edition import EnterpriseEdition
from latet.ee.settings import TERM_FILES, TermSyncSettings, source_secret, terms_bound
from latet.ee.sync import service
from latet.ee.sync.graph import read_graph
from latet.ee.sync.ldap import read_ldap
from latet.ee.sync.metrics import TermMetrics
from latet.ee.sync.service import CHANNEL, LEASE
from latet.engine.types import SlotKind
from latet.floor import TermsUnavailable
from latet.server.app import open_server

from ..licenses import Clock as Ticker
from ..people.server_support import BY_HAND, SERVER_TESTS, settle
from .test_graph import SECRET, FakeGraph
from .test_ldap import BIND, PASSWORD, directory

pytestmark = SERVER_TESTS
# Seconds a dead holder's lease lasts: the test sets the sources and asks meanwhile (2 s or so), and a Valkey in a
# virtual machine (Docker Desktop's) whose clock is set forward by a few seconds now and then ends a key that much sooner.
DEAD = 10.0


def terms(server):
    return server.edition.terms  # E6's TermSync, in E1's EnterpriseEdition


def set_sources(cluster, *sources):
    """The sources, as `latet server terms sources set` saves them (E1's settings, by hand here; Task 11's route)."""
    section = TermSyncSettings.model_validate({"sources": list(sources)})
    cluster.server.edition.settings_store.set_section("term_sync", section.model_dump(mode="json"), actor=BY_HAND)
    for server in cluster.servers:
        server.edition.refresh()
    return section


def keep_secret(cluster, source, value):
    cluster.server.edition.secrets.save(source_secret(source), value, bound=terms_bound(source))


def run_all(cluster, name=None):
    """A sync asked for (Task 11's route does the same in its transaction), and waited for: whichever instance holds
    the lease runs it. The tests' clock stands still: it moves a second first, so that the sync is asked for after the
    source's last run began (the sources set wake every instance, which runs a source never run at once)."""
    cluster.clock.advance(1)
    sync = terms(cluster.server)
    asked = sync.ask(name, by=None)
    sync.asked()

    def done() -> bool:
        state = terms(cluster.server).status()["sources"]
        return all(s["asked"] is None and s["last"] is not None for s in state if s["name"] in asked)

    settle(done, seconds=10)
    return {s["name"]: s["last"] for s in terms(cluster.server).status()["sources"]}


def sent(cluster, index, identity, text):
    runtime = cluster.clients[index].portal.call(cluster.servers[index].spaces.runtime, identity)
    engine = runtime.built.engine
    return engine.mask_text(text, SlotKind.USER_TEXT, engine.session("e6")).text


@pytest.fixture
def cluster(cluster):
    """E3's cluster once every instance hears ee:terms (Valkey has confirmed each one's subscription): a message
    published before would reach none, and an instance would take the new terms only at its next tick (30 s)."""
    bus = cluster.server.bus
    settle(lambda: bus.client.pubsub_numsub(bus.key(CHANNEL))[0][1] >= len(cluster.servers))
    return cluster


@pytest.fixture
def exports(cluster):
    """The customers' export, in the server's term-sync folder (a shared volume in a deployment), a name of its own."""
    folder = cluster.server.data / TERM_FILES
    folder.mkdir(parents=True, exist_ok=True)
    name = f"customers-{uuid.uuid4().hex[:8]}.csv"
    (folder / name).write_text("company\nHalden Insurance\nKestrel Analytics\n")
    yield folder / name
    (folder / name).unlink(missing_ok=True)


@pytest.fixture
def quick(monkeypatch):
    """Every instance looks at what is due each second rather than every TICK (30 s): asked for before the cluster, so
    that its threads wait so from their first look."""
    monkeypatch.setattr(service, "TICK", 1.0)


def test_terms_from_a_file_entra_and_ldap_are_masked_in_every_space_of_every_instance(cluster, exports):
    """Done when (enterprise-edition.md, E6): a file, a fake Microsoft Graph and ldap3's own directory; a term gone from
    its source stays masked until it is removed."""
    graph = httpx.Client(transport=httpx.MockTransport(FakeGraph()))
    for server in cluster.servers:  # whichever instance runs it reads the fakes, from the first run the sources wake
        terms(server).readers["graph"] = functools.partial(read_graph, client=graph)
        terms(server).readers["ldap"] = functools.partial(read_ldap, folder=exports.parent, connect=directory)
    section = set_sources(
        cluster,
        {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}},
        {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
         "mapping": {"displayName": "PERSON", "mail": "EMAIL"}},
        {"name": "directory", "kind": "ldap", "url": "ldaps://dc1.acme.com", "bind": BIND,
         "base": "ou=staff,dc=acme,dc=com", "mapping": {"{givenName} {sn}": "PERSON"}},
    )  # fmt: skip
    keep_secret(cluster, section.sources[1], SECRET)
    keep_secret(cluster, section.sources[2], PASSWORD)
    runs = run_all(cluster)
    assert {name: last["outcome"] for name, last in runs.items()} == {
        "customers": "ok",
        "colleagues": "ok",
        "directory": "ok",
    }
    person, _code = cluster.server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    key, _secret = cluster.server.identities.add_key("Billing CI")
    text = "Kestrel Analytics wrote to Noah Brennan and Daniel Okafor."
    for index in (0, 1):
        for identity in (person, key):
            masked = functools.partial(sent, cluster, index, identity, text)
            settle(lambda masked=masked: not any(n in masked() for n in ("Kestrel", "Noah", "Okafor")))
    exports.write_text("company\nHalden Insurance\n")  # Kestrel Analytics leaves its source
    run_all(cluster, "customers")
    assert "Kestrel analytics" not in sent(cluster, 1, key, "Kestrel analytics called.")  # gone: still masked
    sync = terms(cluster.server)
    removed = sync.remove(source="customers")
    sync.removed(removed, source="customers", by=None)  # the administration's (Task 11's route writes its entry)
    settle(lambda: "KESTREL ANALYTICS" in sent(cluster, 1, key, "KESTREL ANALYTICS called."))
    # A spelling this space had masked stays in its vault until someone forgets it on the Vault page.
    assert "Kestrel analytics" not in sent(cluster, 1, key, "Kestrel analytics called.")
    graph.close()


def test_a_server_that_syncs_nothing_keeps_the_keys_of_0_11_in_every_space(cluster):
    """A-M4, at the server (the final review's I1): with no synced term, every space of every instance masks under no
    tag, so that the engine's cache, the texts each conversation was given and the final check keep their keys across
    the upgrade; the edition hands each space the live terms all the same."""
    person, _code = cluster.server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    for index, server in enumerate(cluster.servers):
        runtime = cluster.clients[index].portal.call(server.spaces.runtime, person)
        masker = runtime.built.engine.masker
        assert terms(server).loaded and any(d is terms(server).live for d in masker.detectors)
        assert (masker.tag, masker.stamp) == ("", "")  # the re-review's R1: no change ever, no stamp either


def test_a_secret_kept_for_another_place_is_never_sent_and_the_source_fails_no_secret(cluster):
    """A source's secret opens only for where it was kept for (B-I3): the source moved since, its secret is not opened
    (the master key refuses it), nothing is sent, and the source says how to set it again, never an error of Latet's."""
    fake = FakeGraph()
    graph = httpx.Client(transport=httpx.MockTransport(fake))
    for server in cluster.servers:
        terms(server).readers["graph"] = functools.partial(read_graph, client=graph)
    colleagues = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
                  "mapping": {"displayName": "PERSON"}}  # fmt: skip
    kept_for = TermSyncSettings.model_validate({"sources": [colleagues]}).sources[0]
    keep_secret(cluster, kept_for, SECRET)
    set_sources(cluster, colleagues | {"tenant": "contoso.onmicrosoft.com"})  # moved to another tenant
    last = run_all(cluster)["colleagues"]
    assert (last["outcome"], last["code"]) == ("failed", "no-secret") and fake.requests == []
    graph.close()


def test_a_secret_sealed_with_a_master_key_this_server_lacks_is_never_sent_and_the_source_fails_no_secret(cluster):
    """A source's secret sealed with a master key version this server does not hold (another instance's newer key, a
    row written by hand): the master key refuses it (``MasterKeyError``), nothing is sent, and the source fails
    `no-secret`, never as an error of Latet's."""
    fake = FakeGraph()
    graph = httpx.Client(transport=httpx.MockTransport(fake))
    for server in cluster.servers:
        terms(server).readers["graph"] = functools.partial(read_graph, client=graph)
    colleagues = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
                  "mapping": {"displayName": "PERSON"}}  # fmt: skip
    source = TermSyncSettings.model_validate({"sources": [colleagues]}).sources[0]
    # Kept and rewritten before the source is set: setting it wakes every instance, and a run woken between the two
    # would open the secret as it was first sealed, rightly (a newer key's seal is written at once, never after one of
    # this server's), and send it to the fake Graph.
    keep_secret(cluster, source, SECRET)
    with cluster.server.db.transaction() as conn:  # its row as a version 2 of the master key would have sealed it
        changed = conn.execute(
            "UPDATE ee_secrets SET sealed = set_byte(sealed, 1, 2) WHERE name = %s", (source_secret(source),)
        )
        assert changed.rowcount == 1
    set_sources(cluster, colleagues)
    last = run_all(cluster)["colleagues"]
    assert (last["outcome"], last["code"]) == ("failed", "no-secret") and fake.requests == []
    graph.close()


def test_a_sync_on_one_instance_masks_the_new_terms_in_a_space_another_built_before(cluster, exports):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    assert sent(cluster, 1, key, "Halden Insurance renewed.") == "Halden Insurance renewed."
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    run_all(cluster)
    settle(lambda: "Halden Insurance" not in sent(cluster, 1, key, "Halden Insurance renewed."))


def test_sources_saved_on_one_instance_are_synced_at_once_without_being_asked(cluster, exports):
    """The sources saved are heard by every instance (E1's ee:settings), whose term sync looks at once rather than at
    its next tick (30 s): a source never synced is due, and the other instance masks its terms."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    assert sent(cluster, 1, key, "Halden Insurance renewed.") == "Halden Insurance renewed."
    section = {
        "sources": [{"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}}]
    }
    cluster.server.edition.settings_store.set_section("term_sync", section, actor=BY_HAND)  # no refresh, no sync asked
    settle(lambda: "Halden Insurance" not in sent(cluster, 1, key, "Halden Insurance renewed."))


def test_a_license_removed_or_installed_reaches_the_terms_of_every_instance_at_once(cluster, exports):
    """The license heard by every instance (E1's ee:license, and the instance that changed it at once): the terms in
    use follow at once, never a tick later; without the license none is masked, installed again they all are."""
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    run_all(cluster)
    settle(lambda: all(terms(server).live.count == 2 for server in cluster.servers))
    cluster.server.edition.remove_license(actor=BY_HAND)
    settle(lambda: all(terms(server).live.count == 0 for server in cluster.servers))
    cluster.server.edition.install_license(cluster.signer.make(), actor=BY_HAND)
    settle(lambda: all(terms(server).live.count == 2 for server in cluster.servers))


def test_a_refresh_makes_the_term_sync_look_at_once(cluster, monkeypatch):
    """E1's refresh, the license and the settings read again on this instance with no message (`latet server` and the
    tests call it): its term sync looks now, rather than at its next tick."""
    woken = []  # in which thread: the listener's own wakes it too, on a message
    monkeypatch.setattr(terms(cluster.server), "wake", lambda: woken.append(threading.current_thread()))
    cluster.server.edition.refresh()
    assert threading.current_thread() in woken


def _term_threads() -> int:
    """The term sync threads alive in this process, every instance's: the schedule and the reload."""
    names = ("latet-term-sync", "latet-term-reload")
    return sum(1 for thread in threading.enumerate() if thread.name in names and thread.is_alive())


def test_a_closed_edition_leaves_no_term_sync_thread_running(cluster):
    b = cluster.servers[1]
    running = _term_threads()
    b.edition.close()
    assert b.edition.terms is None and _term_threads() == running - 2  # its two, stopped: no lease taken again


def test_the_term_sync_stops_even_when_its_metrics_cannot_be_let_go(cluster, monkeypatch):
    def refused(self, sync):
        raise RuntimeError("the registry is gone")

    b = cluster.servers[1]
    running = _term_threads()
    monkeypatch.setattr(TermMetrics, "unbind", refused)
    b.edition.close()
    assert _term_threads() == running - 2


def test_the_term_sync_starts_once_the_edition_is_whole(cluster, monkeypatch):
    """As E4's threads (I7): never while the edition's policy is being made; started last, once it is whole."""
    b = cluster.servers[1]
    b.edition.close()
    running, seen = _term_threads(), []
    real = edition_module.make_policy

    def slow(server, **options):
        seen.append(_term_threads())
        time.sleep(0.3)  # a policy made slowly: no term sync of this edition runs meanwhile
        seen.append(_term_threads())
        return real(server, **options)

    monkeypatch.setattr(edition_module, "make_policy", slow)
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))  # as E1's tests start one again
    assert seen == [running, running] and _term_threads() == running + 2


def test_an_edition_that_fails_to_start_leaves_no_term_sync_running_nor_bound(cluster, monkeypatch):
    b = cluster.servers[1]
    b.edition.close()
    running = _term_threads()
    made = []
    real = EnterpriseEdition._e6_on_server

    def kept(self, server):
        real(self, server)
        made.append((self, self.terms))

    def failing(server, **options):
        raise ConnectionError("the database at 10.0.0.5 does not answer")

    monkeypatch.setattr(EnterpriseEdition, "_e6_on_server", kept)
    monkeypatch.setattr(edition_module, "make_policy", failing)
    with pytest.raises(ConnectionError):
        EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))
    [(edition, sync)] = made
    metrics = TermMetrics.of(b.metrics)
    assert edition.terms is None and _term_threads() == running  # none started, none left behind (I7)
    assert metrics._bound is None and sync.counted != metrics.counted  # its metrics let go
    with pytest.raises(TermsUnavailable):
        edition.synced_terms(None)


def test_a_one_shot_commands_server_starts_no_term_sync(cluster, instances):
    """`latet server init` opens a server to issue its first token, and closes it: its edition starts no term sync,
    which could take the lease and leave it to run out when the command ends. Its terms are still there when asked."""
    running = _term_threads()
    once = open_server(
        instances.settings, clock=instances.clock, schema=instances.schema, prefix=instances.prefix, one_shot=True
    )
    instances.opened.append(once)  # closed with the others
    assert once.edition.terms is not None and _term_threads() == running
    assert once.edition.synced_terms(None).count == 0


def test_an_instance_closed_in_the_middle_of_a_read_gives_its_lease_back(cluster, exports, monkeypatch):
    """An instance that stops while a source is read (its read waited for a few seconds at most) gives the lease back
    at once: another instance syncs at its next look, not 15 minutes later; what the read gives is never applied."""
    monkeypatch.setattr(service, "CLOSE_WAIT", 0.5)
    reading, go_on = threading.Event(), threading.Event()

    def stuck(source, secret):
        reading.set()
        go_on.wait(30)
        return [{"company": "Halden Insurance"}]

    for server in cluster.servers:
        terms(server).readers["file"] = stuck
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    assert reading.wait(10)
    [holder] = [server for server in cluster.servers if terms(server)._holding is not None]
    sync = terms(holder)
    lease = sync._holding
    assert lease is not None and lease.held()
    holder.edition.close()
    assert not lease.held()  # given back, its read still under way: free for the other instance
    go_on.set()
    assert sync._thread is not None
    sync._thread.join(10)
    assert not sync._thread.is_alive() and sync.store.kept() == 0 and sync.store.runs() == {}  # nothing applied


def dead_holder(cluster):
    """The lease, as an instance that took it and stopped holds it: never given back, it ends in Valkey after DEAD
    seconds. Taken between two looks of the instances, which take it for a moment at each (every second here). Its
    context and the lease: the caller keeps the context, which let go would give the lease back."""
    for _ in range(250):
        dead = cluster.server.bus.lease(LEASE, DEAD)
        if lease := dead.__enter__():
            return dead, lease
        time.sleep(0.02)
    raise AssertionError("the instances never left the lease free")


def test_a_dead_holders_lease_ends_and_another_instance_syncs_then(quick, cluster, exports):
    dead, lease = dead_holder(cluster)  # an instance took it and stopped: never given back, it ends after DEAD s
    # Set while the lease is held: the sources set wake every instance, which would otherwise sync at once.
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    sync = terms(cluster.server)
    sync.ask(None, by=None)
    sync.asked()
    assert terms(cluster.servers[1]).status()["sources"][0]["last"] is None  # nobody may run it yet
    assert lease.held()  # read after: the dead holder's all along, never given back by the test
    settle(lambda: terms(cluster.server).status()["sources"][0]["last"] is not None, seconds=40)  # within a tick
    assert terms(cluster.server).status()["sources"][0]["last"]["outcome"] == "ok"
    assert dead is not None  # kept until here


def test_an_instance_started_after_a_sync_masks_what_was_synced(cluster, instances, exports):
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    run_all(cluster)
    later = instances.open()
    assert later.edition.synced_terms(None).count == 2
    later.edition.close()  # B-M9: a closed server's edition builds no space without its terms
    with pytest.raises(TermsUnavailable):
        later.edition.synced_terms(None)


def test_the_messages_say_no_term(cluster, exports, monkeypatch):
    told = []
    for server in cluster.servers:  # whichever instance runs the source tells the others: both are heard here
        sync = terms(server)

        def tapped(channel, message, real=sync._publish):
            told.append((channel, dict(message)))
            return real(channel, message)

        monkeypatch.setattr(sync, "_publish", tapped)
    set_sources(cluster, {"name": "customers", "kind": "file", "path": exports.name, "mapping": {"company": "CLIENT"}})
    run_all(cluster)
    assert told and all(channel == CHANNEL and set(message) <= {"version", "asked"} for channel, message in told)
    assert any("version" in message for _channel, message in told)  # the run's: the terms changed, never which
    assert "Halden" not in repr(told)
