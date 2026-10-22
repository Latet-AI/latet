import unicodedata

import re2

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.detectors.pii import (
    PiiDetector,
    fiscal_code_check_char,
    iban_valid,
    luhn_valid,
    vat_code_valid,
)
from latet.engine.detectors.structural import AssignmentDetector, CredentialDetector, PrivateKeyDetector
from latet.engine.detectors.terms import (
    WORD_INDEX_FROM,
    IdentityDetector,
    KnownValuesDetector,
    LocalIdentity,
    Term,
    TermsDetector,
)
from latet.engine.merge import merge_spans
from latet.engine.types import EntityClass, SlotKind, Span

CODE = DetectContext(SlotKind.TOOL_RESULT)
PROSE = DetectContext(SlotKind.USER_TEXT)


def found(detector, text, ctx=CODE):
    return [(text[s.start : s.end], s.etype) for s in detector.detect(text, ctx)]


# Fake credentials built at runtime so the repository itself never contains a key-shaped literal.
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
AWS_KEY = "AKIA" + "Q7WZ2K4NBX5PL3MD"
STRIPE = "sk_live_" + "51Hq8ZtLm3Xc9Vb2Nn4Kp7Rr1Ss6Tt8Uu0Ww"


def test_gitleaks_finds_provider_tokens():
    det = GitleaksDetector()
    text = f'token = "{GH_PAT}"\naws_access_key_id = {AWS_KEY}\nSTRIPE_KEY={STRIPE}\n'
    hits = dict(found(det, text))
    assert GH_PAT in hits
    assert AWS_KEY in hits
    assert STRIPE in hits


def test_gitleaks_allowlist_and_entropy():
    det = GitleaksDetector()
    assert found(det, "aws_access_key_id = AKIAIOSFODNN7EXAMPLE") == []  # rule allowlist: .+EXAMPLE$
    assert found(det, "api_key = 'aaaaaaaaaaaaaaaaaaaa'") == []  # global allowlist (repeated letter)


def test_assignment_env_unquoted_and_line_number_prefix():
    det = AssignmentDetector()
    text = "     1\tDB_PASSWORD=hunter2!\n     2\tAPI_TOKEN=abcdef123456\n     3\tTOKEN_TTL=3600\n"
    assert found(det, text) == [("hunter2!", "PASSWORD"), ("abcdef123456", "ASSIGNED_SECRET")]


def test_assignment_json_yaml_powershell():
    det = AssignmentDetector()
    text = '{\n  "client_secret": "s3cr3t-value-x",\n  "token_url": "https://x"\n}\npassword: pa55word\n$env:API_KEY = "k-123456789"\n'
    assert found(det, text) == [
        ("s3cr3t-value-x", "ASSIGNED_SECRET"),
        ("pa55word", "PASSWORD"),
        ("k-123456789", "ASSIGNED_SECRET"),
    ]


def test_assignment_value_followed_by_a_comment_and_short_password_names():
    det = AssignmentDetector()
    text = "\n".join(
        [
            'SMTP_PASS = "ciliegia-Rossa-42"   # TODO move to vault',
            "DB_PASS=s3cr3t-Value # prod",
            'const adminPass = "Orchid-77";  // rotate',
            'bypass = "abc12345"',
            "pass_count = 3",
            'passthrough = "yes-please"',
        ]
    )
    assert found(det, text) == [
        ("ciliegia-Rossa-42", "PASSWORD"),
        ("s3cr3t-Value", "PASSWORD"),
        ("Orchid-77", "PASSWORD"),
    ]


def test_assignment_skips_references_and_placeholders():
    det = AssignmentDetector()
    text = (
        "token = authToken\nAPI_KEY=${API_KEY}\npassword = os.getenv('PW')\nsecret: changeme\n"
        "api_key: <your-api-key>\nmax_tokens = 4096\nauthor = someone\n"
    )
    assert found(det, text) == []


def test_a_connection_string_masks_its_credentials_only():
    det = AssignmentDetector()
    oracle = (
        'string connectionString = "Data Source=(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=hostname)(PORT=1521))'
        '(CONNECT_DATA=(SERVICE_NAME=servicename)));User Id={{username}};Password={{password}};";'
    )
    assert found(det, oracle) == []  # placeholders all along: nothing to mask
    text = "\n".join(
        [
            'DB_CONNECTION_STRING="Server=db;User Id=app;Password=Sup3rS3cret!;"',
            'conn_str: "DefaultEndpointsProtocol=https;AccountName=acme;AccountKey=bW9jay1rZXk=;EndpointSuffix=x"',
            "connectionString = \"Driver={ODBC 18};PWD={pa;ss word};Jet OLEDB:Database Password='x;y z';\"",
            'connection_string = "postgres://app:Hunt3r-22@db.internal/shop"',
            'connection_string = "postgres://app:{{password}}@db.internal/shop"',
        ]
    )
    assert found(det, text) == [
        ("Sup3rS3cret!", "PASSWORD"),
        ("bW9jay1rZXk=", "ASSIGNED_SECRET"),
        ("pa;ss word", "PASSWORD"),
        ("x;y z", "PASSWORD"),
        ("Hunt3r-22", "URL_PASSWORD"),
    ]
    # Neither form, and a DSN: the value is a secret as a whole.
    whole = 'connection_string = "host=db user=app password=Sup3r"\nSENTRY_DSN=https://k3y@o1.sentry.io/2'
    assert found(det, whole) == [
        ("host=db user=app password=Sup3r", "ASSIGNED_SECRET"),
        ("https://k3y@o1.sentry.io/2", "ASSIGNED_SECRET"),
    ]


def test_private_key_body_masked_header_kept():
    det = PrivateKeyDetector()
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nabcDEF123=="
    text = f"x\n-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\ny"
    [(value, etype)] = found(det, text)
    assert etype == "PRIVATE_KEY"
    assert value.strip() == body
    assert "BEGIN" not in value and "END" not in value


def test_credentials_url_password_jwt_bearer():
    det = CredentialDetector()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    text = f"postgres://app:Sup3rS3cret@db.internal:5432/x\nAuthorization: Bearer {jwt}\n"
    hits = found(det, text)
    assert ("Sup3rS3cret", "URL_PASSWORD") in hits
    assert (jwt, "JWT") in hits
    assert (jwt, "BEARER_TOKEN") in hits  # merged later with the JWT span


def test_pii_email_reserved_and_system_addresses_skipped():
    det = PiiDetector()
    text = "mario.rossi@acme.it, git@github.com:x/y.git, test@example.com, icon@2x.png"
    assert found(det, text) == [("mario.rossi@acme.it", "EMAIL")]


def test_pii_phone_needs_formatting_in_code():
    det = PiiDetector(enabled=["PHONE"])
    assert found(det, "id = 3331234567", CODE) == []
    assert found(det, "call +39 333 123 4567", CODE) == [("+39 333 123 4567", "PHONE")]
    assert found(det, "il mio numero è 333 1234567", PROSE) == [("333 1234567", "PHONE")]
    assert found(det, "date 2026-10-06 and v1.2.3.4", CODE) == []


def test_pii_checksums():
    assert iban_valid("IT60X0542811101000000123456")
    assert not iban_valid("IT60X0542811101000000123457")
    assert luhn_valid("4111111111111111")
    assert fiscal_code_check_char("RSSMRA85T10A562") == "S"
    assert vat_code_valid("07643520567")


def test_pii_iban_card_cf_vat():
    det = PiiDetector()
    text = (
        "IBAN: IT60 X054 2811 1010 0000 0123 456.\ncard 4111 1111 1111 1111\nCF: RSSMRA85T10A562S\n"
        "P.IVA 07643520567\nrandom 07643520567\n"
    )
    hits = found(det, text)
    assert ("IT60 X054 2811 1010 0000 0123 456", "IBAN") in hits
    assert ("4111 1111 1111 1111", "CREDIT_CARD") in hits
    assert ("RSSMRA85T10A562S", "IT_FISCAL_CODE") in hits
    assert ("07643520567", "IT_VAT_CODE") in hits
    assert sum(1 for h in hits if h[1] == "IT_VAT_CODE") == 1  # no context, no match


def test_pii_ip_public_only_by_default():
    det = PiiDetector(enabled=["IP_ADDRESS"])
    text = "93.184.216.34 8.8.8.8 10.0.0.1 127.0.0.1 192.0.2.10 version 1.2.3.4 2606:4700:4700::1111 ::1"
    assert found(det, text) == [("93.184.216.34", "IP_ADDRESS"), ("2606:4700:4700::1111", "IP_ADDRESS")]


def test_terms_identity_and_known_values():
    terms = TermsDetector([Term("Acme Corp", "CLIENT"), Term("Project Falcon", "PROJECT")])
    assert found(terms, "for acme corp and PROJECT FALCON") == [("acme corp", "CLIENT"), ("PROJECT FALCON", "PROJECT")]
    ident = IdentityDetector(LocalIdentity(username="marc", full_name="Marco Ferretti", emails=("me@acme.it",)))
    text = r"C:\Users\marc\dev and /home/marc/x by Marco Ferretti <me@acme.it>; marco"
    assert found(ident, text) == [
        ("Marco Ferretti", "PERSON"),
        ("me@acme.it", "EMAIL"),
        ("acme.it", "DOMAIN"),  # your company's, wherever it appears
        ("marc", "USERNAME"),
        ("marc", "USERNAME"),
    ]
    known = KnownValuesDetector([("s3cr3t-value-x", "PASSWORD", EntityClass.SECRET)])
    assert found(known, "again: xs3cr3t-value-x!") == [("s3cr3t-value-x", "PASSWORD")]


# A person's identity on a Latet server holds the host name of each of their computers (R-28): every one is masked.
def test_every_host_name_of_an_identity_is_masked():
    ident = IdentityDetector(LocalIdentity(hostname="emily-laptop", hostnames=("emily-desktop", "emily-laptop", "pc1")))
    text = "ssh emily-laptop, then emily-desktop; pc1 is too short to be told from a word"
    assert found(ident, text) == [("emily-laptop", "HOSTNAME"), ("emily-desktop", "HOSTNAME")]
    assert found(IdentityDetector(LocalIdentity(hostnames=("emily-mini",))), "on emily-mini") == [
        ("emily-mini", "HOSTNAME")
    ]


# RE2's \b knows ASCII letters only: a term that began or ended with an accented letter was never found.
def test_a_term_that_starts_or_ends_with_an_accented_letter_is_found():
    terms = TermsDetector([Term("Nicolò", "PERSON"), Term("Émile", "PERSON"), Term("Zoë", "PERSON")])
    text = "ask Nicolò, then émile and Zoë."
    assert found(terms, text) == [("Nicolò", "PERSON"), ("émile", "PERSON"), ("Zoë", "PERSON")]


def test_an_accented_term_is_still_found_as_a_whole_word_only():
    terms = TermsDetector([Term("Zoë", "PERSON"), Term("Émile", "PERSON")])
    assert found(terms, "Zoëlla, aZoë, Émilette and xÉmile") == []


def test_a_longer_term_cut_by_a_letter_leaves_the_shorter_one_found():
    terms = TermsDetector([Term("Zoë Smith", "PERSON"), Term("Zoë", "PERSON")])
    assert found(terms, "Zoë Smithson called Zoë Smith") == [("Zoë", "PERSON"), ("Zoë Smith", "PERSON")]


# A fake keeps the signs of a value: a term of signs alone was its own fake, and every request with it was stopped.
def test_a_term_of_signs_alone_is_never_looked_for():
    terms = TermsDetector([Term(v) for v in ("--", "###", "~~~~", "-- --", "+", "__", "a--b")])
    assert found(terms, "x -- y ### ~~~~ + __ and a--b") == [("a--b", "CUSTOM")]


def test_a_git_author_name_of_signs_alone_masks_no_sign():
    ident = IdentityDetector(LocalIdentity(full_name="-"))
    assert found(ident, "ls -la x - y") == []


def test_a_known_value_with_an_accented_edge_is_found_again():
    known = KnownValuesDetector([("Nicolò Bianchi", "PERSON", EntityClass.NER), ("Zoë", "PERSON", EntityClass.NER)])
    assert found(known, "again Nicolò Bianchi, and Zoë.") == [("Nicolò Bianchi", "PERSON"), ("Zoë", "PERSON")]


def test_terms_whose_first_or_last_letter_is_accented_are_found():
    wanted = [Term("Zoë", "PERSON"), Term("Émile Durand", "PERSON"), Term("Maison Brûlé", "CLIENT")]
    filler = [Term(f"Filler Company {i}", "CLIENT") for i in range(WORD_INDEX_FROM)]
    text = "Zoë and Émile Durand met Maison Brûlé; Zoëx and Brûlés are other words."
    expected = [("Maison Brûlé", "CLIENT"), ("Zoë", "PERSON"), ("Émile Durand", "PERSON")]
    assert sorted(found(TermsDetector(wanted), text)) == expected
    assert sorted(found(TermsDetector([*filler, *wanted]), text)) == expected


def test_a_long_list_of_terms_finds_what_a_short_one_finds():
    # Past WORD_INDEX_FROM terms a list is looked up word by word: the same matches, the longest at each place.
    wanted = [
        Term("Acme Corp", "CLIENT"),
        Term("Acme", "CLIENT"),
        Term("Project Falcon", "PROJECT"),
        Term("C++ Guild", "TEAM"),
    ]
    filler = [Term(f"Filler Company {i}", "CLIENT") for i in range(WORD_INDEX_FROM)]
    text = "acme corp met ACME, then Acme Corporation; PROJECT FALCON and the C++ Guild. Acme2 is no term."
    expected = [
        ("ACME", "CLIENT"),
        ("Acme", "CLIENT"),
        ("C++ Guild", "TEAM"),
        ("PROJECT FALCON", "PROJECT"),
        ("acme corp", "CLIENT"),
    ]
    assert sorted(found(TermsDetector(wanted), text)) == expected
    assert sorted(found(TermsDetector([*filler, *wanted]), text)) == expected


def test_known_values_past_two_thousand_are_still_matched_exactly():
    people = [(f"Person Number{i}", "PERSON", EntityClass.PII) for i in range(2500)]
    known = KnownValuesDetector([("s3cr3t-value-x", "PASSWORD", EntityClass.SECRET), *people])
    text = "Person Number7, person number8 and again: xs3cr3t-value-x!"
    assert sorted(found(known, text)) == [("Person Number7", "PERSON"), ("s3cr3t-value-x", "PASSWORD")]


def test_a_decomposed_accent_keeps_a_term_inside_its_word_in_a_long_list_too():
    # Decomposed, as macOS writes file names, "ë" is an "e" and a combining mark: the mark belongs to the word.
    zoe, text = unicodedata.normalize("NFD", "Zoë"), unicodedata.normalize("NFD", "Zoëlla called Zoë")
    filler = [Term(f"Filler Company {i}", "CLIENT") for i in range(WORD_INDEX_FROM)]
    for terms in ([], filler):
        assert found(TermsDetector([*terms, Term(zoe, "PERSON")]), text) == [(zoe, "PERSON")]
        assert found(TermsDetector([*terms, Term("Zoe", "PERSON")]), text) == []


def test_a_term_is_found_composed_or_decomposed():
    # A keyboard composes "ë"; macOS writes file names decomposed, an "e" and a combining mark: the same name.
    composed, decomposed = unicodedata.normalize("NFC", "Zoë Dubois"), unicodedata.normalize("NFD", "Zoë Dubois")
    filler = [Term(f"Filler Company {i}", "CLIENT") for i in range(WORD_INDEX_FROM)]
    for terms in ([], filler):
        found_decomposed = found(TermsDetector([*terms, Term(composed, "PERSON")]), f"ls: {decomposed}.pdf")
        assert found_decomposed == [(decomposed, "PERSON")]
        found_composed = found(TermsDetector([*terms, Term(decomposed, "PERSON")]), f"see {composed}.")
        assert found_composed == [(composed, "PERSON")]
    secret = unicodedata.normalize("NFC", "s3cr3t-välue")
    known = KnownValuesDetector([(decomposed, "PERSON", EntityClass.NER), (secret, "PASSWORD", EntityClass.SECRET)])
    secret_written = unicodedata.normalize("NFD", secret)
    text = f"again {composed}, and x{secret_written}!"
    assert found(known, text) == [(composed, "PERSON"), (secret_written, "PASSWORD")]


def test_lists_of_terms_are_not_kept_in_re2s_compile_cache():
    # google-re2 keeps the last 128 patterns given to re2.compile alive, with the memory their automata grew, and the
    # vault's detectors are built again at each new value: each would stay behind.
    def compiled() -> int:
        info = re2._Regexp._make.cache_info()
        return info.hits + info.misses

    before = compiled()
    TermsDetector([Term("Acme Corp", "CLIENT"), Term("Zoë", "PERSON")])
    KnownValuesDetector(
        [("s3cr3t-value-x", "PASSWORD", EntityClass.SECRET), ("Emily Carter", "PERSON", EntityClass.PII)]
    )
    assert compiled() == before


def test_merge_disjoint_and_class_priority():
    spans = [
        Span(0, 10, "EMAIL", EntityClass.PII, "a"),
        Span(5, 15, "TOKEN", EntityClass.SECRET, "b"),
        Span(20, 25, "X", EntityClass.PII, "c"),
        Span(21, 23, "Y", EntityClass.NER, "d"),
    ]
    out = merge_spans(spans)
    assert [(s.start, s.end, s.etype) for s in out] == [(0, 15, "TOKEN"), (20, 25, "X")]
