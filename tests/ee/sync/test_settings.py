"""The section `term_sync` of the Enterprise settings: each kind of source as written, what is refused, saying why and
never what it was given, and where each source's secret may go."""

import itertools

import pytest
from pydantic import ValidationError

from latet.ee.settings import (
    EnterpriseSettings,
    EnterpriseSettingsError,
    TermSyncSettings,
    needs_secret,
    source_destinations,
    source_local,
    source_secret,
    terms_bound,
    with_section,
)

SOURCES = [
    {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}},
    {"name": "projects", "kind": "url", "url": "https://intranet.acme.com/api/projects", "records": "items",
     "headers": {"Authorization": "Bearer {secret}"}, "mapping": {"code_name": "PROJECT"}},
    {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
     "mapping": {"displayName": "PERSON", "mail": "EMAIL"}},
    {"name": "directory", "kind": "ldap", "url": "ldaps://dc1.acme.com", "bind": "CN=latet,OU=Service,DC=acme,DC=com",
     "base": "OU=Staff,DC=acme,DC=com", "mapping": {"{givenName} {sn}": "PERSON"}},
]  # fmt: skip


def test_each_kind_of_source_reads_as_written_in_the_section_of_the_enterprise_settings():
    section = TermSyncSettings.model_validate({"sources": SOURCES})
    assert [s.kind for s in section.sources] == ["file", "url", "graph", "ldap"]
    assert section.sources[0].every_minutes == 60 and section.sources[3].start_tls
    kept = EnterpriseSettings.model_validate({"term_sync": section.model_dump(mode="json")})
    assert kept.term_sync == section and EnterpriseSettings().term_sync.sources == []
    assert source_secret(section.sources[2]) == "term-sync-colleagues"


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ({"kind": "file", "path": "/srv/exports/customers.csv", "mapping": {"company": "CLIENT"}}, "inside"),
        ({"kind": "file", "path": "exports/customers.csv", "mapping": {"company": "CLIENT"}}, "no folder"),  # B-M8
        ({"kind": "file", "path": "../secrets/master.key", "mapping": {"company": "CLIENT"}}, "inside"),
        ({"kind": "url", "url": "http://crm.acme.com/a.csv", "mapping": {"company": "CLIENT"}}, "https"),
        ({"kind": "url", "url": "http://localhost:8080/a.csv", "mapping": {"company": "CLIENT"}}, "https"),
        ({"kind": "url", "url": "https://crm.acme.com/a.csv", "mapping": {}}, "at least 1"),
        ({"kind": "url", "url": "https://crm.acme.com/a.csv", "mapping": {"company": "client"}}, "capitals"),
        ({"kind": "url", "url": "https://crm.acme.com/a.csv", "headers": {"Authorization": "Bearer s3cr3t"},
          "mapping": {"company": "CLIENT"}}, "{secret}"),
        ({"kind": "url", "url": "https://user:pw@crm.acme.com/a.csv", "mapping": {"company": "CLIENT"}}, "no user"),
        ({"kind": "url", "url": "https://127.1/a.csv", "mapping": {"company": "CLIENT"}}, "never reaches"),
        ({"kind": "ldap", "url": "ldap://dc1.acme.com", "start_tls": False, "base": "DC=acme,DC=com",
          "mapping": {"cn": "PERSON"}}, "TLS"),
        ({"kind": "ldap", "url": "ldaps://dc1.acme.com", "base": "DC=acme,DC=com", "filter": "(cn=*",
          "mapping": {"cn": "PERSON"}}, "parenthes"),
        ({"kind": "graph", "tenant": "acme", "client_id": "x", "objects": "groups", "group": "g1",
          "mapping": {"displayName": "TEAM"}}, "users"),
        ({"kind": "crm", "mapping": {"name": "CLIENT"}}, "kind"),
        ({"kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}, "colour": "blue"}, "colour"),
    ],
)  # fmt: skip
def test_a_source_written_wrong_is_refused_saying_why_and_never_what_it_holds(source, why):
    with pytest.raises(ValidationError, match=why) as caught:
        TermSyncSettings.model_validate({"sources": [{"name": "s", **source}]})
    assert "s3cr3t" not in str(caught.value) and "master.key" not in str(caught.value)


def test_two_sources_with_one_name_or_a_name_too_long_for_its_secret_are_refused():
    source = {"kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
    with pytest.raises(ValidationError, match="name of its own"):
        TermSyncSettings.model_validate({"sources": [{"name": "crm", **source}, {"name": "crm", **source}]})
    with pytest.raises(ValidationError):
        TermSyncSettings.model_validate({"sources": [{"name": "c" * 41, **source}]})


def test_a_secret_is_bound_to_where_it_goes_and_to_nothing_else():
    url, graph, ldap = (TermSyncSettings.model_validate({"sources": [s]}).sources[0] for s in SOURCES[1:])
    moved = url.model_copy(update={"url": "https://elsewhere.example.com/api/projects"})
    assert terms_bound(moved) != terms_bound(url)
    assert terms_bound(url.model_copy(update={"every_minutes": 5})) == terms_bound(url)
    assert terms_bound(graph.model_copy(update={"login_url": "https://login.evil.example"})) != terms_bound(graph)
    assert terms_bound(ldap.model_copy(update={"bind": "CN=other,DC=acme,DC=com"})) != terms_bound(ldap)


def test_a_secret_is_bound_to_what_its_source_reads_too():
    """The design security review's B-I3 and B-M6: whoever edits the sources never reuses a secret to read other data
    with it (another filter, base, set of objects, records or fields, or another Graph); what is not read with it (the
    schedule, the values left out, a type) leaves it as it is."""
    url, graph, ldap = (TermSyncSettings.model_validate({"sources": [s]}).sources[0] for s in SOURCES[1:])
    for source, changes in (
        (ldap, [{"filter": "(objectClass=computer)"}, {"base": "DC=acme,DC=com"}, {"mapping": {"homePhone": "PHONE"}}]),
        (graph, [{"objects": "groups"}, {"group": "6f1c2b9e-0001"}, {"filter": "department eq 'HR'"},
                 {"mapping": {"mobilePhone": "PHONE"}}, {"graph_url": "https://graph.evil.example"}]),
        (url, [{"records": "data.salaries"}, {"mapping": {"salary": "AMOUNT"}},
               {"headers": {"Authorization": "Bearer {secret}", "X-Tenant": "board"}}]),  # every header (B-R2)
    ):  # fmt: skip
        for change in changes:
            assert terms_bound(source.model_copy(update=change)) != terms_bound(source), change
        for same in ({"every_minutes": 5}, {"exclude": ["Sales"]}, {"min_length": 6}):
            assert terms_bound(source.model_copy(update=same)) == terms_bound(source), same
    retyped = {field: "CLIENT" for field in ldap.mapping}  # the same fields read: another type is no other data
    assert terms_bound(ldap.model_copy(update={"mapping": retyped})) == terms_bound(ldap)


def test_a_source_at_the_servers_own_loopback_is_local_and_where_each_connects_is_said():
    local = TermSyncSettings.model_validate({"sources": [
        {"name": "a", "kind": "url", "url": "https://localhost:8443/a.json", "mapping": {"x": "CLIENT"}},
        {"name": "b", "kind": "ldap", "url": "ldap://127.0.0.1:1389", "start_tls": False, "base": "DC=acme",
         "mapping": {"cn": "PERSON"}},
    ]}).sources  # fmt: skip
    assert all(source_local(s) for s in local)
    url, graph, ldap = (TermSyncSettings.model_validate({"sources": [s]}).sources[0] for s in SOURCES[1:])
    assert not any(source_local(s) for s in (url, graph, ldap))
    assert source_destinations(url) == {("https", "intranet.acme.com", 443)}
    assert source_destinations(ldap) == {("ldaps", "dc1.acme.com", 636)} and len(source_destinations(graph)) == 2


def test_a_scheme_s_own_port_is_one_for_every_place_that_says_it():
    """The final review's M2: where a source connects (the destinations counted, the place a secret is kept for, an
    LDAP directory's port and Graph's origin) takes a scheme's own port from one place."""
    from latet.ee.settings import default_port

    assert [default_port(s) for s in ("https", "http", "ldaps", "ldap", "tls", "gopher")] == [443, 80, 636, 389, 0, 0]


def test_a_save_refuses_an_address_the_server_never_reaches():
    link_local = {"name": "x", "kind": "url", "url": "https://169.254.169.254/latest", "mapping": {"a": "CLIENT"}}
    with pytest.raises(EnterpriseSettingsError, match=r"term_sync\.sources\.0"):
        with_section(EnterpriseSettings(), "term_sync", {"sources": [link_local]})


# --- beyond the brief: what the review of this task's code found -------------------------------------------------------


def test_a_kind_no_source_has_is_refused_without_quoting_it():
    """Pydantic's words for a tagged union quote the tag given: a kind is said as the four there are."""
    for check in (
        lambda wrong: TermSyncSettings.model_validate({"sources": [wrong]}),
        lambda wrong: with_section(EnterpriseSettings(), "term_sync", {"sources": [wrong]}),
    ):
        with pytest.raises(ValueError, match="file, url, graph or ldap") as caught:
            check({"name": "s", "kind": "hush-k3y-e4f1", "mapping": {"name": "CLIENT"}})
        assert "k3y" not in str(caught.value)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("ldaps://dc1.acme.com" + chr(10), "visible ASCII"),  # urllib drops a line break: the address kept holds it
        ("ldaps://dc1.acme" + chr(9) + ".com", "visible ASCII"),
        ("ldaps://[fe80::1%25eth0]", "a host is"),  # a zone picks one of the server's interfaces
        # Fix round 1's 3: nothing a parser other than urllib's could read otherwise.
        ("ldaps://dc1.acme.com#eth0", "no path, query or fragment"),
        ("ldaps://dc1.acme.com#", "no path, query or fragment"),
        ("ldaps://dc1.acme.com?", "no path, query or fragment"),
        ("ldaps://dc1.acme.com/?eth0", "no path, query or fragment"),
        ("ldaps://@dc1.acme.com", "no user or password"),
        ("ldaps://latet:eth0@dc1.acme.com", "no user or password"),
    ],
)
def test_a_directory_s_address_is_checked_as_it_is_kept(url, why):
    source = {"name": "d", "kind": "ldap", "url": url, "base": "DC=acme,DC=com", "mapping": {"cn": "PERSON"}}
    with pytest.raises(ValidationError, match=why) as caught:
        TermSyncSettings.model_validate({"sources": [source]})
    assert "eth0" not in str(caught.value)


# --- fix round 1: the review of this task's code -------------------------------------------------------------------------


def _one(**source):
    return TermSyncSettings.model_validate({"sources": [{"name": "s", **source}]}).sources[0]


def test_the_review_s_two_directories_have_bindings_of_their_own():
    """Important 1: an IPv6 host whose last group is digits, a port, a bind DN holding colons: written as one joined
    string, the two were one place, and the secret of the one opened for the other."""
    one = _one(kind="ldap", url="ldaps://[fd00::1:10]", bind="CN=latet,DC=acme,DC=com", base="DC=acme,DC=com",
               mapping={"cn": "PERSON"})  # fmt: skip
    other = _one(kind="ldap", url="ldaps://[fd00::1]:10", bind="636:CN=latet,DC=acme,DC=com", base="DC=acme,DC=com",
                 mapping={"cn": "PERSON"})  # fmt: skip
    assert terms_bound(one) != terms_bound(other)


def _directories():
    for scheme, host, port, bind, certificate, base, mapping in itertools.product(
        ("ldaps", "ldap"),
        ("fd00::1", "fd00::1:10", "fd00::1:10:636", "dc1.acme.com"),
        (None, 10, 3269),  # neither scheme's own port: ldap://h and ldap://h:389 are one place, and one binding
        (None, "CN=latet,DC=acme,DC=com", "636:CN=latet,DC=acme,DC=com", "10:636:CN=latet,DC=acme,DC=com"),
        (None, "ca.pem", "ca-2.pem"),
        ("DC=acme,DC=com", "OU=Staff,DC=acme,DC=com"),
        ({"cn": "PERSON"}, {"{givenName} {sn}": "PERSON"}),
    ):
        where = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
        yield _one(kind="ldap", url=f"{scheme}://{where}", bind=bind, certificate=certificate, base=base,
                   mapping=mapping)  # fmt: skip


def _addresses():
    headers = ({}, {"Authorization": "Bearer {secret}"}, {"X-Tenant": "board"},
               {"Authorization": "Bearer {secret}", "X-Tenant": "board"})  # fmt: skip
    for host, port, path, query, sent, records, mapping in itertools.product(
        ("intranet.acme.com", "fd00::1", "fd00::1:8443"),
        (None, 8443),
        ("", "/a", "/a/b"),
        ("", "x=1", "x=1&y=2"),
        headers,
        (None, "items", "data.items"),
        ({"code_name": "PROJECT"}, {"code": "PROJECT"}),
    ):
        where = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
        url = f"https://{where}{path}" + (f"?{query}" if query else "")
        yield _one(kind="url", url=url, headers=sent, records=records, mapping=mapping)


def _graphs():
    for login, tenant, client, (objects, group), odata, graph, mapping in itertools.product(
        ("https://login.microsoftonline.com", "https://login.microsoftonline.us"),
        ("acme", "acme.onmicrosoft.com"),
        ("6f1c2b9e", "6f1c-2b9e"),
        (("users", None), ("users", "g1"), ("groups", None)),
        (None, "department eq 'HR'"),
        ("https://graph.microsoft.com", "https://graph.microsoft.us"),
        ({"displayName": "PERSON"}, {"mail": "EMAIL"}),
    ):
        yield _one(kind="graph", login_url=login, tenant=tenant, client_id=client, objects=objects, group=group,
                   filter=odata, graph_url=graph, mapping=mapping)  # fmt: skip


def test_no_two_sources_that_differ_in_a_bound_field_share_a_binding():
    """Every source of every kind, one name for all, each differing from every other in a field its secret is bound to
    (where it goes, what it reads with it, its kind): each binding is its own."""
    sources = [
        *_directories(),
        *_addresses(),
        *_graphs(),
        _one(kind="file", path="customers.csv", mapping={"a": "CLIENT"}),
    ]
    assert len({terms_bound(source) for source in sources}) == len(sources)


def test_a_directory_s_certificate_is_bound_too():
    """Fix round 1's 2: the authority that decides who receives the bind password."""
    directory = _one(kind="ldap", url="ldaps://dc1.acme.com", bind="CN=latet,DC=acme,DC=com", base="DC=acme,DC=com",
                     mapping={"cn": "PERSON"})  # fmt: skip
    trusted = directory.model_copy(update={"certificate": "ca.pem"})
    assert len({terms_bound(directory), terms_bound(trusted),
                terms_bound(trusted.model_copy(update={"certificate": "other-ca.pem"}))}) == 3  # fmt: skip


# DEL, a C1 control, a bidirectional override and isolate, a zero-width space, two spaces other than ASCII's, a lone
# surrogate.
UNPRINTABLE = (0x7F, 0x85, 0x202E, 0x2066, 0x200B, 0xA0, 0x3000, 0xD800)


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        *(("encoding", name, "text encoding") for name in ("zlib", "base64", "rot13", "hex", "undefined")),
        ("encoding", "utf" + chr(10) + "8", "text encoding"),  # codecs.lookup reads it as utf-8: kept as typed
        ("encoding", "u" * 41, "text encoding"),
        *(("delimiter", sign, "separator") for sign in ('"', chr(10), chr(13), chr(0), chr(0xE9))),
        *(("records", value, "records") for value in ("", "data" + chr(10) + "items", "data" + chr(0x2028))),
        # Printable Unicode is taken (below), never a character that cannot reach a reason safely: a control character
        # (DEL, a C1 one), a bidirectional override or isolate, a zero-width space, a space other than ASCII's, a lone
        # surrogate (E6's deferred minor).
        *(("records", "data" + sign + "items", "records") for sign in map(chr, UNPRINTABLE)),
    ],
)
def test_a_document_s_settings_that_could_never_be_read_are_refused(field, value, why):
    """Fix round 1's 4: an encoding of text (never zlib or rot13), a separator the CSV reader takes, and where the
    records are in visible characters, before any of them can reach a reason or a log."""
    with pytest.raises(ValidationError, match=why) as caught:
        _one(kind="file", path="customers.csv", mapping={"company": "CLIENT"}, **{field: value})
    assert len(value) < 2 or value not in str(caught.value)


def test_a_document_s_usual_settings_still_save():
    for field, value in (("encoding", "cp1252"), ("encoding", "UTF-16LE"), ("encoding", "iso8859_15"),
                         ("delimiter", ";"), ("delimiter", chr(9)), ("delimiter", "|"), ("records", "data.items")):  # fmt: skip
        assert (
            getattr(_one(kind="file", path="customers.csv", mapping={"a": "CLIENT"}, **{field: value}), field) == value
        )


@pytest.mark.parametrize("records", ["données", "données.éléments", "Kunden.Größe", "データ.項目"])
def test_records_at_keys_of_printable_unicode_save(records):
    """E6's deferred minor: where the records are may be any printable text (a key such as `données`), not visible
    ASCII alone; the reader never quotes it in a reason unless it is visible ASCII (``records._parsed``)."""
    for kind, where in (("file", {"path": "customers.json"}), ("url", {"url": "https://intranet.acme.com/api"})):
        assert _one(kind=kind, mapping={"a": "CLIENT"}, records=records, **where).records == records


@pytest.mark.parametrize(
    "address", ["https://localhost", "https://127.0.0.1:8443", "https://[::1]", "https://LocalHost."]
)
def test_microsoft_s_addresses_are_never_the_server_itself(address):
    """Fix round 1's 5: Microsoft's clouds are public; egress refuses the loopback to a graph source when it connects,
    so its settings refuse it when they are written."""
    for field in ("login_url", "graph_url"):
        with pytest.raises(ValidationError, match="never the server itself"):
            _one(
                kind="graph", tenant="acme", client_id="6f1c2b9e", mapping={"displayName": "PERSON"}, **{field: address}
            )


def test_which_sources_read_with_a_secret():
    """Fix round 1's 7."""
    assert not needs_secret(_one(kind="file", path="customers.csv", mapping={"a": "CLIENT"}))
    assert not needs_secret(_one(kind="url", url="https://intranet.acme.com/a.csv", mapping={"a": "CLIENT"}))
    assert needs_secret(_one(kind="url", url="https://intranet.acme.com/{secret}/a.csv", mapping={"a": "CLIENT"}))
    assert needs_secret(_one(kind="url", url="https://intranet.acme.com/a.csv", headers={"X-Api-Key": "{secret}"},
                             mapping={"a": "CLIENT"}))  # fmt: skip
    assert needs_secret(_one(kind="graph", tenant="acme", client_id="6f1c2b9e", mapping={"mail": "EMAIL"}))
    ldap = {"kind": "ldap", "url": "ldaps://dc1.acme.com", "base": "DC=acme,DC=com", "mapping": {"cn": "PERSON"}}
    assert needs_secret(_one(**ldap, bind="CN=latet,DC=acme,DC=com")) and not needs_secret(_one(**ldap))


@pytest.mark.parametrize(
    ("source", "reached"),
    [
        ({"kind": "graph", "graph_url": "https://10.0.0.5"}, False),  # Microsoft's clouds are public
        ({"kind": "graph", "login_url": "https://[fd00::1]"}, False),
        ({"kind": "graph", "graph_url": "https://20.190.128.1"}, True),
        ({"kind": "ldap", "url": "ldaps://10.0.0.5"}, True),  # a directory inside the company's network
        ({"kind": "ldap", "url": "ldaps://[fd00::1:10]"}, True),
        ({"kind": "ldap", "url": "ldaps://169.254.169.254"}, False),  # the cloud's metadata address
        ({"kind": "ldap", "url": "ldaps://[::ffff:10.0.0.5]"}, False),  # an IPv6 form that carries an IPv4 one
    ],
)
def test_a_save_judges_a_graph_s_and_a_directory_s_literal_address_as_egress_does(source, reached):
    """Fix round 1's 7: `terms_problem` for the two kinds the brief's tests left out."""
    filled = {"tenant": "acme", "client_id": "6f1c2b9e", "mapping": {"mail": "EMAIL"}} if source["kind"] == "graph" else {
        "base": "DC=acme,DC=com", "mapping": {"cn": "PERSON"}}  # fmt: skip
    section = {"sources": [{"name": "s", **filled, **source}]}
    if reached:
        assert with_section(EnterpriseSettings(), "term_sync", section).term_sync.sources[0].kind == source["kind"]
    else:
        with pytest.raises(EnterpriseSettingsError, match=r"term_sync\.sources\.0: the server never reaches"):
            with_section(EnterpriseSettings(), "term_sync", section)


def test_a_url_source_s_reserved_header_is_refused_as_a_url_source_s():
    """Fix round 1's 8: the shared words name a url source as such, an output's staying as E4 wrote them."""
    with pytest.raises(ValidationError, match="is not a url source's"):
        _one(kind="url", url="https://intranet.acme.com/a.csv", headers={"X-Forwarded-For": "10.0.0.1"},
             mapping={"a": "CLIENT"})  # fmt: skip


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"tenant": "."}, "a tenant is its id or a domain"),
        ({"tenant": ".."}, "a tenant is its id or a domain"),
        ({"tenant": "..."}, "a tenant is its id or a domain"),
        ({"login_url": "https://login.microsoftonline.com#"}, "an origin"),
        ({"login_url": "https://login.microsoftonline.com?"}, "an origin"),
        ({"login_url": "https://login.microsoftonline.com/?#"}, "an origin"),
        ({"graph_url": "https://graph.microsoft.com#"}, "an origin"),
        ({"graph_url": "https://graph.microsoft.com?"}, "an origin"),
        ({"graph_url": "https://graph.microsoft.com/?"}, "an origin"),
    ],
)
def test_a_graph_source_that_would_move_its_token_or_its_pages_is_refused_at_save(changed, why):
    """Task 8's review: a tenant that is a dot segment, or a query or a fragment after Microsoft's addresses, even an
    empty one, moves the token's request or the pages to another path of the host; each is refused when the sources are
    saved, in fixed words that never repeat what was given."""
    graph = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
             "mapping": {"displayName": "PERSON"}}  # fmt: skip
    with pytest.raises(EnterpriseSettingsError, match=why) as caught:
        with_section(EnterpriseSettings(), "term_sync", {"sources": [graph | changed]})
    said = str(caught.value)
    assert "login.microsoftonline" not in said and "graph.microsoft" not in said  # what was given, never said
    assert "input" not in said and "'.'" not in said and "'..'" not in said
