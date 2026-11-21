"""A server of the Enterprise line upgrades across Latet 0.10.0: one whose schema has the Community's four migrations
of 0.9.8 and every Enterprise table (100 to 301), as the Enterprise branch had them before 0.10.0 came in, takes the
Community's 005 to 012 after its Enterprise tables, then 0.15.0's column of the accounts (302), E4's, E5's and E6's
(from 400: the branch had none of them then), keeps what they hold (people, groups, roles, the control of a key, the accounts, the notices, a key's token and a
key's value kept before), and ends with the schema a new server makes, which applies every Community migration first and
the Enterprise ones after."""

import time

import pytest

from latet.server import db as db_module
from latet.server.db import Database, SchemaMismatch, check_schema, migrate

from .servers import DATABASE, MASTER_KEY, Instances, master, services

pytestmark = [pytest.mark.server, services]

COMMUNITY = "latet.server.migrations"
ENTERPRISE = "latet.ee.migrations"
# The Community's migrations the Enterprise branch had before 0.10.0 came in (0.9.8's), and those 0.9.10 and 0.10.0 add.
BEFORE = ("001_core", "002_admin_token_issuer", "003_audit_seek", "004_computer_key")
ADDED = (
    "005_vault_decoded",
    "006_token_permissions",
    "007_key_vaults",
    "008_vault_word_edges",
    "009_vault_held",
    "010_forget_permission",
    "011_added_token_permissions",
    "012_retired_fakes",
    # Latet 0.11's: a vault's prints, launch keys and the plugin's bindings, and the request exceptions' index.
    "013_vault_prints",
    "014_launch_authority",
    "015_legacy_plugin_binding",
    "016_plugin_identity_quota",
    "017_request_exception_index",
    # Latet 0.15.0's: what the administration log is told once (the default for new request members).
    "018_server_notes",
)
# The Enterprise migrations a server from before 0.10.0 had none of: 0.15.0's renamed accounts (302, in E3's hundred),
# and the later parts' (E4's from 400, E5's from 500, E6's from 600). It takes them after the Community's (in the order
# of the packages, as a new server does).
LATER = ("302_account_renamed", "400_events", "401_siem", "500_approvals", "600_terms")
EMILY, KEY, PLATFORM = "u_emilycarte", "u_billingci2", "g_platform23"


def _before_e4(name: str) -> bool:
    """An Enterprise migration of the branch before 0.10.0 came in: numbered below E4's hundred, and none of LATER."""
    short = name.removeprefix(f"{ENTERPRISE}:")
    return int(short[:3]) < 400 and short not in LATER


@pytest.fixture
def upgraded(clock, monkeypatch):
    """A server's schema as the Enterprise branch made it before 0.10.0 came in, migrated no further."""
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    every = db_module._migrations
    with monkeypatch.context() as before:
        before.setattr(
            db_module,
            "_migrations",
            lambda packages: [
                (name, text)
                for name, text in every(packages)
                if (name.startswith(f"{ENTERPRISE}:") and _before_e4(name))
                or name.removeprefix(f"{COMMUNITY}:") in BEFORE
            ],
        )
        opened = Instances(clock)
    yield opened
    opened.close()


@pytest.fixture
def fresh(clock, monkeypatch):
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    opened = Instances(clock)
    yield opened
    opened.close()


def _applied(db: Database) -> list[str]:
    with db.connection() as conn:
        return [row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY applied, name")]


def _catalog(db: Database) -> dict[str, object]:
    """The schema as PostgreSQL describes it, its own name written ``S``: each table's columns, the indexes, the
    constraints, the triggers and the functions."""
    schema = db.schema
    with db.connection() as conn:
        columns = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = %s",
            (schema,),
        ).fetchall()
        indexes = conn.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (schema,)).fetchall()
        constraints = conn.execute(
            "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c"
            " JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s",
            (schema,),
        ).fetchall()
        triggers = conn.execute(
            "SELECT event_object_table, trigger_name, action_statement FROM information_schema.triggers"
            " WHERE trigger_schema = %s",
            (schema,),
        ).fetchall()
        functions = conn.execute(
            "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = %s",
            (schema,),
        ).fetchall()

    def named(rows: list[tuple[object, ...]]) -> set[tuple[str, ...]]:
        return {tuple(str(value).replace(schema, "S") for value in row) for row in rows}

    return {
        "columns": named(columns),
        "indexes": named(indexes),
        "constraints": named(constraints),
        "triggers": named(triggers),
        "functions": named(functions),
    }


def _kept_before(db: Database) -> None:
    """What a server of the Enterprise branch kept before 0.10.0, written as its schema holds it: a person and an API
    key, the key's token (no permissions column yet) and a value of its vault (no decoded column yet), a group, its
    member, a role, the key's control given to the group, the person's account and up to which notice she saw."""
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO identities (id, kind, name, email, created) VALUES (%s, 'person', %s, %s, %s)",
            (EMILY, "Emily Carter", "emily.carter@acme.com", now),
        )
        conn.execute(
            "INSERT INTO identities (id, kind, name, created) VALUES (%s, 'key', %s, %s)", (KEY, "Billing CI", now)
        )
        conn.execute(
            "INSERT INTO tokens (id, identity_id, hash, route, label, created, tag)"
            " VALUES ('t_billing234', %s, 'hash', 'route', 'key', %s, 'tag')",
            (KEY, now),
        )
        conn.execute(
            "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate,"
            " surrogate_key, counter, created, rule) VALUES (%s, 'e_kept', 'EMAIL', 20, %s, %s, %s, %s, 0, %s,"
            " 'pii:email')",
            (KEY, b"sealed value", b"value key", b"sealed fake", b"fake key", now),
        )
        conn.execute(
            "INSERT INTO groups (id, name, source, created, modified) VALUES (%s, 'Platform', 'local', %s, %s)",
            (PLATFORM, now, now),
        )
        conn.execute("INSERT INTO members (group_id, identity_id, source) VALUES (%s, %s, 'local')", (PLATFORM, EMILY))
        conn.execute("INSERT INTO roles (subject, role) VALUES (%s, 'admin')", (EMILY,))
        conn.execute("INSERT INTO key_controllers (key_id, subject) VALUES (%s, %s)", (KEY, PLATFORM))
        conn.execute(
            "INSERT INTO accounts (identity_id, user_name, modified) VALUES (%s, 'emily.carter@acme.com', %s)",
            (EMILY, now),
        )
        conn.execute("INSERT INTO notified (identity_id, until) VALUES (%s, 7)", (EMILY,))


def test_a_server_of_the_enterprise_line_before_0_10_0_takes_its_migrations_and_keeps_what_it_holds(upgraded, fresh):
    db = Database(DATABASE, schema=upgraded.schema, max_size=2)
    try:
        before = _applied(db)
        assert [name for name in before if name.startswith(f"{COMMUNITY}:")] == [f"{COMMUNITY}:{n}" for n in BEFORE]
        assert {name.removeprefix(f"{ENTERPRISE}:") for name in before if name.startswith(f"{ENTERPRISE}:")} == {
            "100_enterprise",
            "201_policy",
            "300_people",
            "301_notices",
        }
        with pytest.raises(SchemaMismatch, match="older than this Latet"):
            check_schema(db)
        _kept_before(db)
        later = [f"{ENTERPRISE}:{name}" for name in LATER]
        assert migrate(db, master=master()) == [*(f"{COMMUNITY}:{name}" for name in ADDED), *later]  # Community first
        check_schema(db)
        with db.connection() as conn:
            assert conn.execute("SELECT permissions FROM tokens").fetchall() == [(None,)]  # scan, mask and unmask
            assert conn.execute("SELECT decoded FROM vault_entities").fetchall() == [(None,)]
            assert conn.execute("SELECT vault_limit FROM identities WHERE id = %s", (KEY,)).fetchone() == (None,)
            assert conn.execute("SELECT until FROM notified").fetchall() == [(7,)]
            assert conn.execute("SELECT user_name FROM accounts").fetchall() == [("emily.carter@acme.com",)]
        fresh_db = Database(DATABASE, schema=fresh.schema, max_size=2)
        try:
            assert _catalog(db) == _catalog(fresh_db)  # the schema a new server makes, whatever the order
            applied = _applied(fresh_db)
            community = [name for name in applied if name.startswith(f"{COMMUNITY}:")]
            assert community == [f"{COMMUNITY}:{name}" for name in (*BEFORE, *ADDED)]
            assert applied.index(community[-1]) < min(
                i for i, n in enumerate(applied) if n.startswith(f"{ENTERPRISE}:")
            )
        finally:
            fresh_db.close()
    finally:
        db.close()
    server = upgraded.open()  # an instance opens on the upgraded schema and reads E3's tables as they were
    directory = server.edition.people.directory()
    assert [group.name for group in directory.groups] == ["Platform"]
    assert directory.members == {PLATFORM: [(EMILY, "local")]} and directory.controllers == {KEY: [PLATFORM]}
    assert {role.value for role in directory.roles[EMILY]} == {"admin"}


def test_an_enterprise_migration_alone_waits_until_no_instance_is_alive(fresh):
    """Latet 0.11's migrate guard asks which migrations wait with the very packages it applies (``db.MIGRATIONS``, the
    Enterprise Edition's among them): an upgrade that brings an Enterprise migration alone is refused while an instance
    of the server is alive, nothing applied and nothing recorded, and applies it once none is."""
    from latet.server import app as app_module
    from latet.server import instance

    server = fresh.open()
    name = f"{ENTERPRISE}:301_notices"
    with server.db.connection() as conn:  # as after an upgrade that brings this Enterprise migration alone
        conn.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
        conn.execute("DROP INDEX audit_notices")
        conn.execute("DROP TABLE notified")
    app_module._alive(server)
    with pytest.raises(instance.InstancesAlive) as refused:
        instance.migrate_schema(server.settings, server.db, prefix=server.bus.prefix)
    assert "1 instance of the server is alive" in str(refused.value)
    assert "1 to apply" in (instance.schema_state(server.settings, server.db) or "")  # nothing applied
    assert server.audit.query(action="server.migrated") == []
    app_module._gone(server)
    assert instance.migrate_schema(server.settings, server.db, prefix=server.bus.prefix) == [name]
    assert instance.schema_state(server.settings, server.db) is None
