import json
import unicodedata

import pytest
from stdnum import iban as std_iban
from stdnum import luhn as std_luhn
from stdnum.it import codicefiscale, iva

from latet.config import Config, TermConfig
from latet.engine.build import build_masker
from latet.engine.rehydrate import StreamRehydrator, restore_text, restores_for
from latet.engine.types import EntityClass, Policy, SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"


def test_surrogates_keep_format_and_checksums(vault):
    cases = {
        "IBAN": "IT60X0542811101000000123456",
        "CREDIT_CARD": "4111 1111 1111 1111",
        "IT_FISCAL_CODE": "RSSMRA85T10A562S",
        "IT_VAT_CODE": "07643520567",
    }
    out = {etype: vault.intern(etype, EntityClass.PII, value).surrogate for etype, value in cases.items()}
    assert std_iban.is_valid(out["IBAN"]) and out["IBAN"][:2] == "IT" and len(out["IBAN"]) == 27
    assert std_luhn.is_valid(out["CREDIT_CARD"].replace(" ", "")) and out["CREDIT_CARD"][4] == " "
    assert codicefiscale.is_valid(out["IT_FISCAL_CODE"])
    assert iva.is_valid(out["IT_VAT_CODE"])
    for etype, value in cases.items():
        assert out[etype] != value


def test_secret_surrogate_keeps_prefix_length_and_charset(vault):
    e = vault.intern("GITHUB_PAT", EntityClass.SECRET, GH_PAT)
    assert e.surrogate.startswith("ghp_") and len(e.surrogate) == len(GH_PAT) and e.surrogate != GH_PAT
    aws = vault.intern("AWS_ACCESS_TOKEN", EntityClass.SECRET, "AKIA" + "Q7WZ2K4NBX5PL3MD")
    assert (aws.surrogate.startswith("AKIA") and aws.surrogate.isupper()) or aws.surrogate[4:].isalnum()


def test_a_street_of_kept_words_still_gets_a_surrogate(vault):
    # The street type and particles stay as they are: a street made only of them gets another street of its language,
    # never itself with a name added ("Corso DeLuca 3" held "Corso").
    for value in ("Corso", "Piazza della", "VIA"):
        surrogate = vault.intern("STREET_ADDRESS", EntityClass.NER, value).surrogate
        assert value.casefold() not in surrogate.casefold() and len(surrogate.split()) == 3, surrogate
    assert vault.intern("STREET_ADDRESS", EntityClass.NER, "VIA").surrogate.isupper()
    assert vault.intern("STREET_ADDRESS", EntityClass.NER, "via Garibaldi 12").surrogate.startswith("via ")


def test_digits_of_any_script_get_digits_of_the_same_script(vault):
    # They were kept as they are, like signs: a value of such digits alone was its own fake, and every request
    # with it was stopped.
    def zero(c: str) -> int:
        return ord(c) - unicodedata.decimal(c)

    full_width = "".join(chr(0xFF11 + i) for i in range(4))  # 1234 written as full-width digits
    for value in (full_width, "١٢٣٤", "४२४२"):
        surrogate = vault.intern("CUSTOM", EntityClass.CUSTOM, value).surrogate
        assert surrogate != value and [zero(c) for c in surrogate] == [zero(c) for c in value]


def test_a_term_of_signs_alone_in_the_settings_stops_no_request():
    config = Config(ner="off", terms=[TermConfig(value=v) for v in ("--", "###", "+", "Falcon")])
    masker, _ = build_masker(config, Vault(ROOT_KEY))
    masked = masker.mask("x -- y ### + Falcon", SlotKind.TOOL_RESULT)
    assert masked.text.startswith("x -- y ### + ") and [r.original for r in masked.replacements] == ["Falcon"]


def test_a_value_of_signs_alone_found_by_a_detector_stops_no_request():
    # Its fake would have been itself, so the masking failed; and the final check, reading the text with the same
    # detectors, took it for a secret left in clear (in Claude Code, the only check).
    from latet.engine.verifier import Verifier

    masker, _ = build_masker(Config(ner="off"), Vault(ROOT_KEY))
    texts = [
        "Authorization: Bearer ----------------------",
        'curl -H "Authorization: Bearer ...................."',
        '{"password": "!!!!!!!!"}',
    ]
    for text in texts:
        assert masker.mask(text, SlotKind.TOOL_RESULT).text == text
    assert Verifier(masker).verify([(t, SlotKind.TOOL_RESULT, False) for t in texts], "s").leaks == []


def test_surrogates_are_deterministic_across_vaults():
    a = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "mario.rossi@acme.it")
    b = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, "mario.rossi@acme.it")
    c = Vault(b"\x08" * 32).intern("EMAIL", EntityClass.PII, "mario.rossi@acme.it")
    assert a.surrogate == b.surrogate != c.surrogate
    assert a.surrogate.endswith(".example")
    assert "." in a.surrogate.split("@")[0]


def test_email_domain_is_consistent_and_public_domains_kept(vault):
    one = vault.intern("EMAIL", EntityClass.PII, "anna@acme.it").surrogate
    two = vault.intern("EMAIL", EntityClass.PII, "luca@acme.it").surrogate
    assert one.split("@")[1] == two.split("@")[1]
    assert vault.intern("EMAIL", EntityClass.PII, "someone@gmail.com").surrogate.endswith("@gmail.com")


def test_a_written_out_address_shares_its_domain_with_the_address_it_stands_for(vault):
    written = vault.intern("EMAIL", EntityClass.LLM, "daniel dot okafor at stmarys-health dot org")
    plain = vault.intern("EMAIL", EntityClass.PII, "anna@stmarys-health.org")
    domain = plain.surrogate.split("@")[1]
    assert written.surrogate.endswith(" at " + domain.replace(".", " dot "))
    assert vault.by_original("daniel dot okafor at stmarys-health dot org") is written


def test_words_reported_as_an_address_that_write_none_are_not_a_domain(vault):
    entity = vault.intern("EMAIL", EntityClass.LLM, "meet at noon")
    assert "@" not in entity.surrogate and entity.surrogate != "meet at noon"
    assert [e.etype for e in vault.entities()] == ["EMAIL"]  # and no domain is taken out of them


def test_mask_env_file_and_identity(masker):
    text = (
        "     1\tAPI_KEY=" + GH_PAT + "\n     2\tDB_PASSWORD=hunter2!\n     3\tOWNER=mario.rossi@acme.it\n"
        "     4\t# C:\\Users\\marc\\dev by Marco Ferretti for Acme Corp\n"
    )
    masked = masker.mask(text, SlotKind.TOOL_RESULT)
    for secret in (GH_PAT, "hunter2!", "mario.rossi@acme.it", "marc\\", "Marco Ferretti", "Acme Corp"):
        assert secret not in masked.text
    assert "API_KEY=ghp_" in masked.text  # prefix kept, structure intact
    assert masked.text.count("\n") == text.count("\n")


def test_mask_is_stable_and_restore_round_trips(masker, vault):
    text = "key " + GH_PAT + " mail mario.rossi@acme.it"
    first = masker.mask(text).text
    assert masker.mask(text).text == first
    restored, ids = restore_text(first, restores_for(vault.entities()))
    assert restored == text
    assert len(ids) == 2


def test_known_values_masked_anywhere_after_first_sight(masker):
    masker.mask("DB_PASSWORD=Zq8!kLm2pX")
    later = masker.mask("the password is Zq8!kLm2pX, right?", SlotKind.USER_TEXT)
    assert "Zq8!kLm2pX" not in later.text


def test_echo_surrogate_in_client_text_becomes_new_entity(masker, vault):
    s1 = masker.mask("DB_PASSWORD=Zq8!kLm2pX").replacements[0].replacement
    # The client sends back a literal surrogate (e.g. it was written into a file): mask it too.
    echoed = masker.mask(f"file contains {s1}")
    assert s1 not in echoed.text
    # The model's own output keeps its surrogates untouched.
    assert masker.mask(f"I used {s1}", from_model=True).text == f"I used {s1}"


def test_allowlist(vault):
    from latet.engine.detectors.pii import PiiDetector
    from latet.engine.masker import AllowList, Masker

    m = Masker(vault, [PiiDetector()], allow=AllowList(values={"support@acme.it"}))
    assert m.mask("write to support@acme.it or mario@acme.it").text.startswith("write to support@acme.it or ")


def test_latets_own_name_is_never_masked(vault):
    """Latet is the tool the agent talks about, never a value of yours: no detector masks its name, a term or a
    value kept in the vault included."""
    from latet.engine.detectors.terms import Term, TermsDetector
    from latet.engine.masker import AllowList, Masker

    vault.intern("ORG", EntityClass.NER, "Latet", rule="ner:org")
    m = Masker(vault, [TermsDetector([Term("LATET")])], allow=AllowList())
    assert (
        m.mask("Latet masked it; run latet doctor (LATET_PORT).").text
        == "Latet masked it; run latet doctor (LATET_PORT)."
    )
    assert AllowList().allows("Latet") and not AllowList().allows("Latet Ross")


def test_stream_rehydrator_handles_split_and_json(vault):
    e = vault.intern("PASSWORD", EntityClass.SECRET, 'pa"ss\\word')
    text = f"use {e.surrogate} now"
    stream = StreamRehydrator(restores_for([e]))
    out = "".join(stream.feed(c) for c in text) + stream.flush()
    assert out == 'use pa"ss\\word now'
    # In JSON mode the restored value is escaped so the JSON stays valid.
    js = json.dumps({"cmd": f"login {e.surrogate}"})
    restored, _ = restore_text(js, restores_for([e], json_mode=True))
    assert json.loads(restored)["cmd"] == 'login pa"ss\\word'


def test_wordlike_restore_respects_boundaries(vault):
    e = vault.intern("PERSON", EntityClass.IDENTITY, "Marco Ferretti")
    first, last = e.surrogate.split()
    restores = restores_for([e])
    out, _ = restore_text(f"{e.surrogate} and {last}. Not {first}x", restores)
    assert out.startswith("Marco Ferretti and Ferretti.")
    assert out.endswith(f"Not {first}x")


def test_blocked_leak_keeps_blocking_when_resent(masker):
    from latet.engine.verifier import Verifier

    verifier = Verifier(masker)
    leaky = [("token=ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i", SlotKind.TOOL_RESULT, False)]
    assert verifier.verify(leaky, "s").blocking  # never left: blocked
    assert verifier.verify(leaky, "s").blocking  # resent with the next turn: still blocked
    clean = [("nothing to see here", SlotKind.USER_TEXT, False)]
    assert not verifier.verify(clean, "s").blocking


def test_sent_block_is_flagged_not_blocked_later(masker, vault):
    from latet.engine.verifier import Verifier

    verifier = Verifier(masker)
    slot = [("the codename is Zephyrine", SlotKind.USER_TEXT, False)]
    assert not verifier.verify(slot, "s").leaks  # clean: it leaves
    vault.intern("CUSTOM", EntityClass.CUSTOM, "Zephyrine")  # later the value becomes known
    result = verifier.verify(slot, "s")  # the same block again: it already left
    assert result.leaks and not result.blocking


def test_a_leak_says_where_its_value_is(masker, vault):
    from latet.engine.verifier import Verifier

    verifier = Verifier(masker)
    text = f"deploy with {GH_PAT} and then stop"
    slots = [("", SlotKind.USER_TEXT, False), (text, SlotKind.TOOL_RESULT, False)]
    [leak] = verifier.verify(slots, "s").leaks
    assert leak.slot == 1  # among the slots given, an empty one counted
    assert text[leak.start : leak.end] == GH_PAT
    assert GH_PAT not in leak.preview  # where it is, never what it is
    # A block that was clean is read again only for the values learnt since: those have a place too.
    note = "the codename is Zephyrine, again"
    assert not verifier.verify([(note, SlotKind.USER_TEXT, False)], "t").leaks
    vault.intern("CUSTOM", EntityClass.CUSTOM, "Zephyrine")
    [known] = verifier.verify([(note, SlotKind.USER_TEXT, False)], "u").leaks
    assert note[known.start : known.end] == "Zephyrine"


def test_an_email_domain_is_masked_everywhere_in_the_same_text(vault):
    from latet.config import Config
    from latet.engine.build import build

    masker = build(Config(), vault).masker
    text = "Staging: https://api.acme-intra.it/v2 . Mail ops@acme-intra.it. Repo git.acme-intra.it"
    out = masker.mask(text, SlotKind.SYSTEM)
    assert "acme-intra.it" not in out.text
    domain = vault.by_original("acme-intra.it")
    assert domain is not None and out.text.count(domain.surrogate) == 3
    # The same text later gives the same: what the model saw before stays what it sees.
    assert masker.mask(text, SlotKind.SYSTEM).text == out.text
    # A mail provider's domain is no one's: it stays.
    assert masker.mask("write to anna.rossi@gmail.com about gmail.com", SlotKind.USER_TEXT).text.endswith("gmail.com")


def _reported_email(value: str) -> list:
    """``value`` as the local reviewer reports an e-mail address: a value to mask besides what the detectors find."""
    from latet.engine.masker import ExtraValue

    return [ExtraValue(value, "EMAIL", EntityClass.LLM, "llm:email")]


def _domains(vault: Vault) -> list[str]:
    return [e.original for e in vault.entities() if e.etype == "DOMAIN"]


def test_an_address_written_out_brings_its_domain_along(masker, vault):
    """The local reviewer reports an address written out in words, and the same text names its domain bare: the
    domain is masked with the fake domain of the address's own fake, as beside an address written with `@`."""
    from latet.engine.surrogates.generators import written_email

    written = "daniel dot okafor at stmarys-health dot org"
    text = f"Reply to {written}. Portal: https://portal.stmarys-health.org/login"
    out = masker.mask(text, SlotKind.TOOL_RESULT, extra=_reported_email(written)).text
    fake = vault.by_original(written).surrogate
    domain = written_email(fake).address.rpartition("@")[2]
    assert out == f"Reply to {fake}. Portal: https://portal.{domain}/login"
    assert _domains(vault) == ["stmarys-health.org"]


def test_an_address_written_out_at_a_mail_provider_brings_no_domain(masker, vault):
    written = "jane dot doe at gmail dot com"
    out = masker.mask(f"Reply to {written}, not on gmail.com.", SlotKind.TOOL_RESULT, extra=_reported_email(written))
    assert out.text.endswith(", not on gmail.com.") and _domains(vault) == []


@pytest.mark.parametrize(
    ("written", "domains"),
    [("Daniel DOT Okafor AT stmarys-health DOT org", ["stmarys-health.org"]), ("Jane DOT Doe AT gmail DOT com", [])],
)
def test_an_address_written_out_is_no_domain_itself(masker, vault, written, domains):
    """Its domain is the one it names, or none: never the address itself in lower case, here beside it in the text
    (the reviewer reported it as written, in capitals)."""
    text = f"Reply to {written} ({written.lower()} on the old form)."
    masker.mask(text, SlotKind.TOOL_RESULT, extra=_reported_email(written))
    assert _domains(vault) == domains


def test_a_short_name_found_once_is_masked_later(vault):
    from latet.engine.masker import Masker

    masker = Masker(vault, [])
    vault.intern("PERSON", EntityClass.LLM, "Ugo")  # the reviewer found it after a request had left
    vault.intern("ORG", EntityClass.LLM, "Bix")  # three letters, not a name: too often a word too
    out = masker.mask("Ugo ha scritto a Bix; ugo e Ugolino no", SlotKind.USER_TEXT).text
    assert not out.startswith("Ugo ") and "a Bix;" in out
    assert out.endswith("; ugo e Ugolino no")  # a name is matched whole and as written


def test_your_company_domain_is_masked_everywhere(vault):
    from latet.engine.detectors.pii import PiiDetector
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity
    from latet.engine.masker import Masker

    emails = ("anna@studio-rossi.it", "anna@gmail.com", "1+anna@users.noreply.github.com")
    masker = Masker(vault, [PiiDetector(), IdentityDetector(LocalIdentity(full_name="Anna Rossi", emails=emails))])
    text = "Clona git.studio-rossi.it/app, scrivi a luca@studio-rossi.it, non su gmail.com o github.com"
    out = masker.mask(text, SlotKind.USER_TEXT).text
    domain = vault.by_original("studio-rossi.it")
    assert domain is not None and domain.etype == "DOMAIN"
    assert "studio-rossi" not in out and f"git.{domain.surrogate}/app" in out
    assert f"@{domain.surrogate}, non su gmail.com o github.com" in out  # an address stays an address


def test_a_value_right_after_an_escape_in_json_text_is_masked(masker, vault):
    r"""JSON text, as an agent reads it in a file or a tool's output, glues a value to an escape (`\nIT60…`): the
    escape's letter is no part of the value, and the value is masked as it is anywhere else."""
    values = ("luca.neri@acme.it", "IT60X0542811101000000123456", "4539148803436467", "Acme Corp")
    text = json.dumps({"lines": "\n".join(("Anna", *values)), "tab": "\t" + values[1]})
    masked = masker.mask(text, SlotKind.TOOL_RESULT).text
    assert [value for value in values if value in masked] == []
    lines = json.loads(masked)["lines"].split("\n")  # the escapes are as they were
    assert len(lines) == 5 and lines[0] == "Anna"
    restored, _ = restore_text(masked, restores_for(vault.entities()))
    assert restored == text


def test_an_address_after_an_escape_is_masked_without_the_escape(masker, vault):
    text = json.dumps({"to": "Anna\nluca.neri@acme.it", "cc": "luca.neri@acme.it"})
    masked = masker.mask(text, SlotKind.TOOL_RESULT).text
    assert vault.by_original("nluca.neri@acme.it") is None
    entity = vault.by_original("luca.neri@acme.it")
    assert entity is not None
    assert json.loads(masked) == {"to": "Anna\n" + entity.surrogate, "cc": entity.surrogate}  # one address, one fake


def test_a_folder_of_a_windows_path_that_reads_like_an_escape_is_still_masked(vault):
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity
    from latet.engine.masker import Masker

    masker = Masker(vault, [IdentityDetector(LocalIdentity(username="nicola"))])
    assert "nicola" not in masker.mask(r"C:\Users\nicola\dev", SlotKind.TOOL_RESULT).text  # `\n`: a folder


def test_the_final_check_finds_a_value_right_after_an_escape(masker):
    from latet.engine.verifier import Verifier

    text = json.dumps({"note": "IBAN\nIT60X0542811101000000123456"})
    leaks = Verifier(masker).verify([(text, SlotKind.TOOL_RESULT, False)], "s").leaks
    assert [text[leak.start : leak.end] for leak in leaks] == ["IT60X0542811101000000123456"]


def test_a_value_found_once_is_masked_again_right_after_an_escape(vault):
    from latet.engine.masker import Masker
    from latet.engine.types import Span

    class Signature:  # finds a name only where it signs, as the name model finds one by its context
        id, mandatory = "signature", True

        def detect(self, text, ctx):
            at = text.find("Regards, ")
            if at >= 0:
                yield Span(at + 9, at + 20, "PERSON", EntityClass.NER, "signature")

    masker = Masker(vault, [Signature()])
    text = json.dumps({"letter": "Regards, Mario Rossi", "cc": "list:\nMario Rossi"})
    assert "Mario Rossi" not in masker.mask(text, SlotKind.TOOL_RESULT).text


def test_a_fake_right_after_an_escape_is_known_as_one(masker, vault):
    entity = vault.intern("EMAIL", EntityClass.PII, "luca.neri@acme.it")
    text = json.dumps({"to": "Anna\n" + entity.surrogate})
    assert [text[s.start : s.end] for s in masker.surrogates_in(text)] == [entity.surrogate]


def test_a_fake_right_after_an_escape_is_restored(vault):
    r"""The model writes a file whose lines start with a name: its tool input is JSON text, where each line break is
    an escape (`\nGiulia Verdi`). The escape's letter is no part of the name: the name is restored."""
    from latet.engine.rehydrate import Annotator

    person = vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    client = vault.intern("CLIENT", EntityClass.CUSTOM, "Acme Corp")
    tool_input = json.dumps({"content": "Hello\n" + person.surrogate + ",\t" + client.surrogate})
    restores = restores_for(vault.entities(), json_mode=True)
    restored, _ = restore_text(tool_input, restores)
    assert json.loads(restored)["content"] == "Hello\nMario Rossi,\tAcme Corp"
    assert Annotator(restores).annotate(tool_input)[0] == restored
    # A backslash written as two and then an `n` reads, nested, as a line break (Ruling 099-R2): what follows it begins
    # a word, as where the decoded reading finds a value, and is restored.
    glued = json.dumps({"path": "C:\\n" + client.surrogate})
    assert json.loads(restore_text(glued, restores)[0])["path"] == "C:\\nAcme Corp"


def test_a_fake_after_an_escape_is_restored_whatever_the_chunks(vault):
    person = vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    tool_input = json.dumps({"content": "Hi\n" + person.surrogate})
    stream = StreamRehydrator(restores_for(vault.entities(), json_mode=True))
    out = "".join(stream.feed(c) for c in tool_input) + stream.flush()  # the backslash and the `n` apart, too
    assert json.loads(out)["content"] == "Hi\nMario Rossi"


def test_a_backslash_written_as_one_before_a_letter_is_no_escape(masker, vault):
    masker.mask(json.dumps({"path": "C:\\nluca.neri@acme.it"}), SlotKind.TOOL_RESULT)  # a backslash, then nluca…
    assert vault.by_original("nluca.neri@acme.it") is not None


def test_a_value_right_after_an_escaped_character_is_masked(masker):
    text = json.dumps({"x": chr(0xE9) + "IT60X0542811101000000123456"})  # é, written as an escape of six characters
    assert "IT60X0542811101000000123456" not in masker.mask(text, SlotKind.TOOL_RESULT).text


def test_the_final_check_learns_a_value_right_after_an_escape(masker, vault):
    from latet.engine.verifier import Verifier

    verifier = Verifier(masker)
    text = json.dumps({"note": "IBAN\nQuasimodo"})
    slot = [(text, SlotKind.TOOL_RESULT, False)]
    assert not verifier.verify(slot, "s").leaks
    vault.intern("CUSTOM", EntityClass.CUSTOM, "Quasimodo")  # learnt later: the block is read again for it
    assert [text[leak.start : leak.end] for leak in verifier.verify(slot, "t").leaks] == ["Quasimodo"]


def test_a_value_the_reviewer_found_is_masked_right_after_an_escape(masker):
    from latet.engine.masker import ExtraValue

    found = [ExtraValue("Quasimodo", "PERSON", EntityClass.LLM, "reviewer")]
    out = masker.mask(json.dumps({"note": "see\nQuasimodo here"}), SlotKind.TOOL_RESULT, extra=found)
    assert "Quasimodo" not in out.text


# --- a value written with escapes, outside a JSON document read value by value ------------------------------------

NICCOLO = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"
BS = chr(92)  # a backslash
AS_JSON = "Niccol" + BS + "u00f2 Ferri"  # NICCOLO as JSON writes it with ASCII alone


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ('```json\n{"name": "' + AS_JSON + '"}\n```', AS_JSON),
        ('{"id": 1, "name": "' + AS_JSON + '"}\n{"id": 2, "name": "' + AS_JSON + '"}\n', AS_JSON),
        (
            '{"payload": "{' + BS + '"name' + BS + '": ' + BS + '"Niccol' + BS * 2 + "u00f2 Ferri" + BS + '"}"}',
            "Niccol" + BS * 2 + "u00f2 Ferri",
        ),
        ("login user=b'Niccol" + BS + "xc3" + BS + "xb2 Ferri' ok", "Niccol" + BS + "xc3" + BS + "xb2 Ferri"),
        ("name='Niccol" + BS + "xf2 Ferri'", "Niccol" + BS + "xf2 Ferri"),
        ('modified:   "notes/Niccol' + BS + "303" + BS + '262 Ferri.md"', "Niccol" + BS + "303" + BS + "262 Ferri"),
        ("GET /people?q=Niccol%C3%B2%20Ferri HTTP/1.1", "Niccol%C3%B2%20Ferri"),
        ("GET /people?q=Niccol%C3%B2+Ferri&page=2 HTTP/1.1", "Niccol%C3%B2+Ferri"),
        ("<td>Niccol&#242; Ferri</td>", "Niccol&#242; Ferri"),
        ("<td>Niccol&#xF2; Ferri</td>", "Niccol&#xF2; Ferri"),
        ("<td>Niccol&ograve; Ferri</td>", "Niccol&ograve; Ferri"),
    ],
    ids=[
        "code-fence", "json-lines", "under-a-string-field", "utf8-bytes", "latin1-bytes", "git-octal", "url-quoted",
        "form-plus", "html-decimal", "html-hex", "html-named",
    ],
)  # fmt: skip
def test_a_known_value_written_with_escapes_is_masked_as_written_and_comes_back_exactly(vault, text, written):
    """A value the vault knows, written with escapes where no JSON document is read value by value (a code fence, JSON
    lines, a document under a string field, a log, a URL, HTML): found where its escapes read as what they stand for,
    it is kept as it is written, a value of its own with a fake of its own, and the text comes back byte for byte."""
    from latet.engine.masker import Masker

    person = vault.intern("PERSON", EntityClass.NER, NICCOLO)
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    kept = vault.by_original(written)
    assert kept is not None and kept.etype == "PERSON" and kept.id != person.id
    assert {r.original for r in masked.replacements} == {written}
    assert masked.text == text.replace(written, kept.surrogate)
    assert " " not in kept.surrogate or "%" not in text  # in a URL, no space: the link stays one
    restored, _ = restore_text(masked.text, restores_for(vault.entities()))
    assert restored == text


@pytest.mark.parametrize(
    ("value", "etype", "eclass", "text", "written"),
    [
        ("mario.rossi@acme.it", "EMAIL", EntityClass.PII, "GET /find?to=mario.rossi%40acme.it HTTP/1.1", "mario.rossi%40acme.it"),
        ("Mario Rossi", "PERSON", EntityClass.NER, "GET /find?q=Mario%20Rossi HTTP/1.1", "Mario%20Rossi"),
        ("Mario Rossi", "PERSON", EntityClass.NER, "GET /find?q=Mario+Rossi&lang=it HTTP/1.1", "Mario+Rossi"),
        ("Mario Rossi", "PERSON", EntityClass.NER, "name=Mario+Rossi&city=Roma", "Mario+Rossi"),
        ("Maria D'Angelo", "PERSON", EntityClass.NER, "const owner = 'Maria D" + BS + "'Angelo';", "Maria D" + BS + "'Angelo"),
        ("Acme \N{FOX FACE} Labs", "CLIENT", EntityClass.CUSTOM, 'say "Acme ' + BS + "ud83e" + BS + 'udd8a Labs"', BS.join(("Acme ", "ud83e", "udd8a Labs"))),
    ],
    ids=["address", "space", "plus-in-a-query", "plus-in-a-form", "quote", "surrogate-pair"],
)  # fmt: skip
def test_a_known_value_written_url_quoted_quoted_or_in_two_halves_comes_back_exactly(
    vault, value, etype, eclass, text, written
):
    from latet.engine.masker import Masker

    vault.intern(etype, eclass, value)
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    kept = vault.by_original(written)
    assert kept is not None and masked.text == text.replace(written, kept.surrogate)
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == text


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("Maria D'Angelo", "const owner = 'Maria D" + BS + "'Angelo';"),
        (NICCOLO, '<td class="owner">Niccol&ograve; Ferri</td>'),
        (NICCOLO, '  "owner": "' + AS_JSON + '",'),
        ("Mario Rossi", 'SEARCH = "https://crm.example/people?q=Mario%20Rossi"'),
    ],
    ids=["quote", "html", "json", "url"],
)
def test_an_agent_that_edits_a_line_it_read_finds_the_line_again(vault, value, line):
    """The agent reads a file whose line writes a known value in escapes, then edits that line: the Edit call's
    old_string, the line as the model saw it, comes back as the file has it, byte for byte."""
    from latet.engine.build import build
    from latet.engine.pipeline import Session

    vault.intern("PERSON", EntityClass.NER, value)
    engine = build(Config(ner="off"), vault).engine
    session = Session("edit")
    seen = engine.mask_text(f"     1\t# a file\n     2\t{line}\n", SlotKind.TOOL_RESULT, session).text
    seen = seen.split("\n")[1].split("\t", 1)[1]
    assert seen != line  # masked
    call = json.dumps({"file_path": "f", "old_string": seen, "new_string": seen + "  // reviewed"})
    args = json.loads(restore_text(call, engine.restores(session, json_mode=True))[0])
    assert args["old_string"] == line
    assert args["new_string"] == line + "  // reviewed"


@pytest.mark.parametrize(
    ("value", "link", "parameter"),
    [
        (("PERSON", EntityClass.NER, NICCOLO), "https://crm.example/api/people?q=Niccol%C3%B2%20Ferri&cursor=abc", "q"),
        (("PERSON", EntityClass.NER, "Mario Rossi"), "https://crm.example/api/people?q=Mario+Rossi&cursor=abc", "q"),
        (("CUSTOMER", EntityClass.CUSTOM, "R&D Labs Srl"), "https://crm.example/api/c?name=R%26D%20Labs%20Srl&cursor=abc", "name"),
        (("API_KEY", EntityClass.SECRET, "Zq7+Kp2/Wm9=Xr4tLb8v"), "https://api.example/v1/f?sig=Zq7%2BKp2%2FWm9%3DXr4tLb8v&n=2", "sig"),
    ],
    ids=["name", "plus", "ampersand", "token"],
)  # fmt: skip
def test_a_link_that_writes_a_known_value_url_quoted_stays_a_link_and_comes_back_exactly(vault, value, link, parameter):
    """A pagination link with a known value URL-quoted: the model sees a well-formed link (no space, its query's fields
    as they were), and the link comes back exactly, whether the model sends it as it saw it or quotes the value again
    with quote or quote_plus."""
    import urllib.parse

    from latet.engine.build import build
    from latet.engine.pipeline import Session

    vault.intern(*value)
    engine = build(Config(ner="off"), vault).engine
    session = Session("links")
    seen = json.loads(engine.mask_text(json.dumps({"next": link}), SlotKind.TOOL_RESULT, session).text)["next"]
    assert seen != link and " " not in seen
    fields = urllib.parse.parse_qs(urllib.parse.urlsplit(seen).query)
    assert sorted(fields) == sorted(urllib.parse.parse_qs(urllib.parse.urlsplit(link).query))
    fake = seen.split(parameter + "=", 1)[1].split("&", 1)[0]
    plain = urllib.parse.unquote_plus(fake)
    restores = engine.restores(session, json_mode=True)
    for sent in (fake, urllib.parse.quote(plain, safe=""), urllib.parse.quote_plus(plain, safe="")):
        call = json.dumps({"url": seen.replace(fake, sent)})
        assert json.loads(restore_text(call, restores)[0])["url"] == link, sent


def test_a_known_secret_with_a_quote_in_json_lines_stays_json_and_comes_back_exactly(vault):
    from latet.engine.masker import Masker

    secret = 'Xy"7kLm!pQ2wZ'
    vault.intern("API_KEY", EntityClass.SECRET, secret)
    lines = json.dumps({"note": f"use {secret} now"}) + "\n" + json.dumps({"n": 1}) + "\n"
    masked = Masker(vault, []).mask(lines, SlotKind.TOOL_RESULT)
    assert secret not in json.loads(masked.text.split("\n")[0])["note"]  # masked, and still JSON
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == lines


def test_a_plus_outside_a_form_is_a_plus(vault):
    from latet.engine.masker import Masker

    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    text = "c = Mario+Rossi(1)"  # not a query nor a form: `+` is a sign
    assert Masker(vault, []).mask(text, SlotKind.TOOL_RESULT).text == text


def test_a_term_and_your_identity_written_with_escapes_are_masked_as_written(vault):
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
    from latet.engine.masker import Masker

    client = "Societ\N{LATIN SMALL LETTER A WITH GRAVE} Rossi"
    masker = Masker(
        vault, [TermsDetector([Term(client, "CLIENT")]), IdentityDetector(LocalIdentity(full_name=NICCOLO))]
    )
    in_git, in_json = "Niccol" + BS + "303" + BS + "262 Ferri", "Societ" + BS + "u00e0 Rossi"
    text = f'git log\n    "notes/{in_git}.md"\n{{"client": "{in_json}"}}'
    masked = masker.mask(text, SlotKind.TOOL_RESULT)
    found = {r.original: r.replacement for r in masked.replacements}
    assert set(found) == {in_git, in_json}
    assert masked.text == text.replace(in_git, found[in_git]).replace(in_json, found[in_json])
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == text


def test_an_address_written_url_quoted_is_found_and_kept_as_written(masker, vault):
    text = "open https://crm.example/mail?to=luca.neri%40acme.it&subject=hello"
    masked = masker.mask(text, SlotKind.TOOL_RESULT)
    entity = vault.by_original("luca.neri%40acme.it")
    assert entity is not None and entity.etype == "EMAIL"
    assert masked.text == text.replace("luca.neri%40acme.it", entity.surrogate)
    assert "%40" in entity.surrogate and "@" not in entity.surrogate  # written as the address was
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == text


def test_the_final_check_finds_a_known_value_written_with_escapes(masker, vault):
    from latet.engine.verifier import Verifier

    vault.intern("CLIENT", EntityClass.CUSTOM, "Quasimodo Srl")
    text = "curl 'https://crm.example/find?name=Quasimod%6F%20Srl'"
    assert [text[s.start : s.end] for s in masker.known_in(text)] == ["Quasimod%6F%20Srl"]
    leaks = Verifier(masker).verify([(text, SlotKind.TOOL_RESULT, False)], "s").leaks
    assert [text[leak.start : leak.end] for leak in leaks] == ["Quasimod%6F%20Srl"]


def test_a_fake_written_with_escapes_is_known_as_one(masker, vault):
    entity = vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    text = "q=" + entity.surrogate.replace(" ", "%20")
    assert [text[s.start : s.end] for s in masker.surrogates_in(text)] == [text[2:]]


@pytest.mark.parametrize(
    ("policies", "values", "text", "masked_value"),
    [
        ({"EMAIL": Policy.ALLOW}, [], "pec: RSSMRA85T10A562S%40pec.it", "RSSMRA85T10A562S"),
        ({"EMAIL": Policy.ALLOW}, [("PERSON", EntityClass.NER, "mario.rossi")], "to mario.rossi%40acme.it ok", "mario.rossi"),
        ({"PHONE": Policy.ALLOW}, [], "iban IT60X0542811101000000123456%2B393331234567 end", "IT60X0542811101000000123456"),
    ],
    ids=["fiscal-code-in-an-address", "known-name-in-an-address", "iban-before-a-phone"],
)  # fmt: skip
def test_a_value_of_a_type_let_through_found_in_escapes_never_frees_another(
    vault, policies, values, text, masked_value
):
    """Found where escapes read as what they stand for, a value of a type your policies let through never names what
    merges with it: what was masked as written still is (an address let through holding a fiscal code)."""
    from latet.engine.build import build_masker

    for value in values:
        vault.intern(*value)
    masker, _ = build_masker(Config(ner="off", policies=policies), vault)
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    assert masked_value not in out.text
    assert masked_value in {r.original for r in out.replacements}


@pytest.mark.parametrize(
    "text",
    [
        "path = r'D:" + BS + "mail" + BS + "100acme.it" + BS + "inbox'  # " + BS + "u00e8",
        "echo 'Q2FuIGZvbyBiYXI%3D' | base64 -d  # token=Q2FuIGZvbyBiYXI%3D",
        "https://registry.npmjs.org/%40babel%2Fcore/-/core-7.24.0.tgz",
        "SELECT * FROM t WHERE name LIKE '%40%' OR code LIKE '%2F%';",
        "https://latet.acme.lan/u/Em1ly_Cart3r-Rout3K3y_7q9%41",
    ],
    ids=["octal-in-a-path", "base64-padding", "npm-scope", "sql-like", "route-and-escape"],
)
def test_escapes_that_stand_for_signs_find_no_secret_nor_a_value_where_0_9_8_found_none(text):
    """What the escapes stand for gives no new secret (`%3D` padding a base64 run), no value of a structure, and an
    octal escape is read only where git writes the bytes of a character past ASCII (`\\100` in a path is no `@`)."""
    from latet.engine.build import build_masker

    masker, _ = build_masker(Config(ner="off"), Vault(ROOT_KEY))
    assert masker.mask(text, SlotKind.TOOL_RESULT).text == text


def test_a_secret_that_0_9_8_masked_as_written_comes_back_exactly(vault):
    from latet.engine.build import build_masker

    masker, _ = build_masker(Config(ner="off"), vault)
    text = "Authorization: Basic dXNlcjpwYXNz%3D"
    masked = masker.mask(text, SlotKind.TOOL_RESULT)
    assert [r.original for r in masked.replacements] == ["dXNlcjpwYXNz"] and masked.text.endswith("%3D")
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == text


@pytest.mark.parametrize(
    "path",
    ["C:" + BS + "\N{LATIN CAPITAL LETTER E WITH ACUTE}milie Durand" + BS + "notes.txt",
     BS * 2 + "srv" + BS + "share" + BS + "\N{LATIN CAPITAL LETTER E WITH ACUTE}milie Durand",
     "x " + BS + "\N{LATIN CAPITAL LETTER E WITH ACUTE}milie Durand"],
    ids=["drive", "share", "after-a-backslash"],
)  # fmt: skip
def test_a_backslash_written_before_an_escape_stays_where_it_is(vault, path):
    """JSON writes a backslash as two, then `É` as an escape: three backslashes, the first two one backslash as written.
    A value found after them leaves them where they are: the path stays a path."""
    from latet.engine.masker import Masker

    name = "\N{LATIN CAPITAL LETTER E WITH ACUTE}milie Durand"
    vault.intern("PERSON", EntityClass.NER, name)
    text = "```json\n" + json.dumps({"path": path}) + "\n```"
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    assert "u00c9milie" not in masked.text
    assert json.loads(masked.text.split("\n")[1])["path"].count(BS) == path.count(BS)
    assert restore_text(masked.text, restores_for(vault.entities()))[0] == text


@pytest.mark.parametrize("policies", [{}, {"EMAIL": Policy.ALLOW}, {"PERSON": Policy.ALLOW, "PHONE": Policy.ALLOW}])
def test_reading_what_escapes_stand_for_never_masks_less(monkeypatch, policies):
    """Whatever the text, and whatever types the policies let through, what is masked when the escapes are read for
    what they stand for holds what was masked before (the text as written and with its escapes' letters blanked out)."""
    import random

    from latet.engine import escapes
    from latet.engine.detectors.pii import PiiDetector
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
    from latet.engine.masker import Masker

    values = [("PERSON", EntityClass.NER, NICCOLO), ("PERSON", EntityClass.NER, "Mario Rossi"),
              ("PERSON", EntityClass.NER, "mario.rossi"), ("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4"),
              ("EMAIL", EntityClass.PII, "mario.rossi@acme.it")]  # fmt: skip
    pieces = [
        "Niccol", BS + "u00f2", "\N{LATIN SMALL LETTER O WITH GRAVE}", " Ferri", "Ferri", "%20", "%40", "%C3%B2", "+",
        "=", "&", "?q=", "&#242;", "&ograve;", BS, BS * 2, BS * 3, "n", "u", "x", "c3", BS + "xb2", BS + "303",
        "mario.rossi", "@acme.it", "acme.it", "Mario", "Rossi", " ", "\n", '"', "tok_9fQ2xL7pZ4",
        "IT60X0542811101000000123456", "4539148803436467", "RSSMRA85T10A562S", "+393331234567", "Acme Corp", "marc",
        "luca.neri%40acme.it", BS + "ud83e", BS + "udd8a", "%3D", "%2B",
    ]  # fmt: skip

    def masked(text: str) -> set[int]:
        known = Vault(ROOT_KEY)
        for etype, eclass, value in values:
            known.intern(etype, eclass, value)
        detectors = [PiiDetector(), TermsDetector([Term("Acme Corp", "CLIENT")]),
                     IdentityDetector(LocalIdentity(username="marc", full_name="Marco Ferretti"))]  # fmt: skip
        done = Masker(known, detectors, policies=policies).mask(text, SlotKind.TOOL_RESULT)
        return {i for r in done.replacements if r.policy != Policy.ALLOW for i in range(r.start, r.end)}

    rng = random.Random(29)
    texts = ["".join(rng.choice(pieces) for _ in range(rng.randrange(1, 14))) for _ in range(400)]
    now = [masked(text) for text in texts]
    monkeypatch.setattr(escapes, "decoded", lambda text: None)  # as before: no reading of what escapes stand for
    before = [masked(text) for text in texts]
    for text, was, is_ in zip(texts, before, now, strict=True):
        assert was <= is_, text
    assert sum(len(n) for n in now) > sum(len(b) for b in before)  # and more is masked


def test_a_span_and_a_reading_never_show_a_value_in_their_repr():
    from latet.engine.escapes import decoded
    from latet.engine.types import Span

    assert "Niccol" not in repr(Span(0, 5, "PERSON", EntityClass.NER, "known-values", value=NICCOLO))
    reading = decoded("q=" + "Niccol%C3%B2")
    assert reading is not None and "Niccol" not in repr(reading)


# --- round 2: what comes back after an escape, repeats let through, fakes that read as values ----------------------

_GLUED = [
    "GET /people?q=Dr.%20Mario%20Rossi&x=1",
    "GET /people?q=%22Mario%20Rossi%22&x=1",
    "GET /people?q=Rossi%2CMario%20Rossi&x=1",
    "GET /people?q=Dr.%20Niccol%C3%B2%20Ferri&x=1",
    "GET /people?q=Dr.+Mario+Rossi&x=1",
    "user=b'Dr." + BS + "x20Mario" + BS + "x20Rossi' logged in",
    "```json\n" + '{"payload": "{' + BS + '"note' + BS + '": ' + BS + '"Gentile' + BS * 2 + "nMario Rossi" + BS + '"}"}' + "\n```",
    "{'body': '{\"note\": \"caff" + BS * 2 + "u00e8" + BS * 2 + "nMario Rossi\"}'}",
    "<td>Dott.&#xA0Mario Rossi</td>",
    "<td>Dott.&#160;Mario Rossi</td>",
    "x " + BS * 2 + "u0020Mario Rossi end",
    "```json\n" + '{"name": "Dr.' + BS + 'u00a0Mario Rossi"}' + "\n```",
]  # fmt: skip


@pytest.mark.parametrize("text", _GLUED, ids=[f"glued-{n}" for n in range(len(_GLUED))])
def test_a_value_right_after_an_escape_that_ends_in_a_letter_comes_back_exactly(vault, text):
    """A value found where escapes read as what they stand for, right after one that ends in a letter or a digit as
    written (`%20`, `%2C`, `\\x20`, a doubled line break, `&#xA0` with no `;`): what precedes the fake begins a word
    for the restore too, so the text comes back exactly, as it reads and in a tool call's JSON."""
    from latet.engine.build import build
    from latet.engine.pipeline import Session

    for value in ("Mario Rossi", NICCOLO):
        vault.intern("PERSON", EntityClass.NER, value)
    engine = build(Config(ner="off"), vault).engine
    session = Session("glued")
    masked = engine.mask_text(text, SlotKind.TOOL_RESULT, session)
    assert masked.replacements and masked.text != text
    assert restore_text(masked.text, engine.restores(session, json_mode=False))[0] == text
    call = json.dumps({"command": masked.text}, ensure_ascii=False)
    assert json.loads(restore_text(call, engine.restores(session, json_mode=True))[0])["command"] == text


_FUZZ_VALUES = [
    ("PERSON", EntityClass.NER, NICCOLO), ("PERSON", EntityClass.NER, "Mario Rossi"),
    ("PERSON", EntityClass.NER, "Anna Maria"), ("PERSON", EntityClass.NER, "Maria Rossi"),
    ("PERSON", EntityClass.NER, "Rossi Bianchi"), ("PERSON", EntityClass.NER, "Maria D'Angelo"),
    ("PERSON", EntityClass.NER, "mario.rossi"), ("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4"),
    ("EMAIL", EntityClass.PII, "mario.rossi@acme.it"),
]  # fmt: skip
_FUZZ_PIECES = [
    "Niccol", BS + "u00f2", "\N{LATIN SMALL LETTER O WITH GRAVE}", " Ferri", "Ferri", "%20", "%40", "%C3%B2", "+", "=",
    "&", "?q=", "&#242;", "&ograve;", BS, BS * 2, BS * 3, BS * 4, "n", "u", "x", "c3", BS + "xb2", BS + "303",
    BS + "262", "mario.rossi", "@acme.it", "acme.it", "Mario", "Rossi", "Anna", "Maria", "Bianchi", " ", " ", "\n", '"',
    "'", BS + "'", BS + '"', "D", "D'Angelo", "D%27Angelo", "&#39;", "tok_9fQ2xL7pZ4", "IT60X0542811101000000123456",
    "4539148803436467", "RSSMRA85T10A562S", "+393331234567", "Acme Corp", "Acme", "marc", "luca.neri%40acme.it",
    "ops@acme.it", BS + "ud83e", BS + "udd8a", "%3D", "%2B", "%2C", ".", ",", "-", "_", "&#64;", "&amp;", BS + "n",
    BS + "u0020", "&#xA0", "%22",
]  # fmt: skip
_FUZZ_FOUND = [
    "-RSSMRA85T10A562SRSSMRA85T10A562Sops@acme.it" + BS + "n%40mario.rossi+393331234567 Ferrimarcops@acme.it.",
    "%20mario.rossi %20" + BS + "ud83e%40RSSMRA85T10A562S&ograve; Ferritok_9fQ2xL7pZ4 Acme",
    "D" + BS + "u0020Acme" + BS + "ud83e" + BS + "262&#39;&#64;-?q=x%3DAcme Corp\"" + BS * 4,
    BS + "nIT60X0542811101000000123456uAnna'&#39;.%2Bmario.rossi _%C3%B2" + BS + "ud83e,+",
    "x" + BS + "udd8a,%40mario.rossi=mario.rossi-RSSMRA85T10A562Sops@acme.it&#39;marc+",
    "%2BAcme Corp%20@acme.itops@acme.it",
    "NiccolD%27Angelo%20Acme Corp",
    "Acme Corp=&ops@acme.it%2Bmario.rossi",
    "RSSMRA85T10A562Sluca.neri%40acme.it" + BS + "Niccol%2BAcme Corp" + BS + "u0020D%27AngeloAcme Corpacme.it"
    "IT60X0542811101000000123456mario.rossi",
    "Mariomarc%40&%2B" + BS + "'" + BS + "'_RSSMRA85T10A562S%2BAcme Corp%2C@acme.it",
]  # fmt: skip


@pytest.mark.parametrize(
    "policies",
    [{}, {"EMAIL": Policy.ALLOW}, {"PERSON": Policy.ALLOW, "PHONE": Policy.ALLOW}, {"DOMAIN": Policy.ALLOW}],
    ids=["none", "email-allow", "person-phone-allow", "domain-allow"],
)
def test_masked_texts_of_escapes_come_back_exactly(policies):
    """Texts of escapes, values and signs (the reviewers' fuzz, its cases that came back wrong first): whatever is
    masked comes back exactly, as it is and in a tool call's JSON."""
    import random

    from latet.engine.detectors.pii import PiiDetector
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
    from latet.engine.masker import Masker

    rng = random.Random(9917)
    texts = _FUZZ_FOUND + ["".join(rng.choice(_FUZZ_PIECES) for _ in range(rng.randrange(1, 16))) for _ in range(300)]
    for text in texts:
        known = Vault(ROOT_KEY)
        for value in _FUZZ_VALUES:
            known.intern(*value)
        detectors = [
            PiiDetector(),
            TermsDetector([Term("Acme Corp", "CLIENT"), Term("Rossi Bianchi", "CLIENT")]),
            IdentityDetector(LocalIdentity(username="marc", full_name="Marco Ferretti")),
        ]
        done = Masker(known, detectors, policies=policies).mask(text, SlotKind.TOOL_RESULT)
        if any(r.policy == Policy.REDACT for r in done.replacements):
            continue
        assert restore_text(done.text, restores_for(known.entities()))[0] == text, (text, done.text)
        as_json = json.dumps(done.text, ensure_ascii=False)
        back = restore_text(as_json, restores_for(known.entities(), json_mode=True))[0]
        assert json.loads(back) == text, (text, done.text)


@pytest.mark.parametrize(
    ("policies", "text", "masked_value"),
    [
        ({"EMAIL": Policy.ALLOW}, "pec: RSSMRA85T10A562S@pec.it, again: RSSMRA85T10A562S%40pec.it", "RSSMRA85T10A562S"),
        ({"EMAIL": Policy.ALLOW}, "pec RSSMRA85T10A562S@pec.it; link https://x.example/?to=RSSMRA85T10A562S%40pec.it&y=1",
         "RSSMRA85T10A562S"),
        ({"PHONE": Policy.ALLOW}, "tel +393331234567; iban IT60X0542811101000000123456%2B393331234567 end",
         "IT60X0542811101000000123456"),
    ],
    ids=["again-in-escapes", "in-a-link", "phone-let-through"],
)  # fmt: skip
def test_a_repeat_of_a_type_let_through_found_in_escapes_never_frees_another(vault, policies, text, masked_value):
    """A value let through, repeated where escapes read as what they stand for (``Masker._repeats``), never names what
    it merges with: the fiscal code 0.9.8 masked in the address written `%40` is masked still."""
    from latet.engine.build import build_masker

    masker, _ = build_masker(Config(ner="off", policies=policies), vault)
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    assert masked_value in {r.original for r in out.replacements}
    assert text.count(masked_value) > out.text.count(masked_value)


# --- a span a policy lets through leaves in clear only itself ------------------------------------------------------

_IBAN = "IT60X0542811101000000123456"


def _final_check_leaks(masker, text: str) -> list[str]:
    from latet.engine.verifier import Verifier

    result = Verifier(masker).verify([(text, SlotKind.TOOL_RESULT, False)], "s", record=False)
    return [leak.etype for leak in result.leaks]


@pytest.mark.parametrize(
    "text",
    [f"pay {_IBAN}.ops@acme-intra.it", f"pay {_IBAN}@acme-intra.it", f"pay ops.{_IBAN}@acme-intra.it"],
    ids=["before-a-dot", "the-local-part", "after-a-dot"],
)
def test_an_iban_inside_an_address_let_through_is_masked_where_it_sits(vault, text):
    """A value of a type your policies let through stays as written, and only it: an IBAN written inside an address let
    through is masked with its own fake where it sits, the rest of the address as written. 0.9.9 merged the IBAN into
    the address (``merge_spans`` names a group by its strongest finding) and let the whole of it through."""
    masker, _ = build_masker(Config(ner="off", policies={"EMAIL": Policy.ALLOW}), vault)
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    assert _IBAN not in out.text and "@acme-intra.it" in out.text
    [replacement] = out.replacements
    assert (replacement.etype, replacement.original) == ("IBAN", _IBAN)
    assert out.text == text.replace(_IBAN, replacement.replacement)
    assert restore_text(out.text, restores_for(vault.entities()))[0] == text
    assert _final_check_leaks(masker, out.text) == []
    assert set(_final_check_leaks(masker, text)) == {"IBAN"}  # what the gateway's final check stops, as in 0.9.9


@pytest.mark.parametrize(
    ("policies", "known", "terms", "text", "value", "masked"),
    [
        ({"EMAIL": Policy.ALLOW}, [], [], "billing 4539148803436467@acme-intra.it", "4539148803436467",
         "4539148803436467"),
        ({"EMAIL": Policy.ALLOW}, [("PERSON", EntityClass.NER, "mario.rossi")], [], "to mario.rossi@acme.it ok",
         "mario.rossi", "mario.rossi"),
        ({"PHONE": Policy.ALLOW}, [], [], "call +393331234567 or IT60X0542811101000000123456+393331234567",
         _IBAN, _IBAN),
        ({"EMAIL": Policy.ALLOW}, [], [TermConfig(value="Quasimodo", category="CLIENT")],
         "to quasimodo.ops@acme-intra.it ok", "quasimodo", "quasimodo.ops@acme-intra.it"),
        ({"PERSON": Policy.ALLOW}, [("PERSON", EntityClass.NER, "Mario Quasimodo")],
         [TermConfig(value="Quasimodo", category="CLIENT")], "ask Mario Quasimodo today", "Quasimodo",
         "Mario Quasimodo"),
        ({"EMAIL": Policy.ALLOW}, [("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4")], [],
         "key tok_9fQ2xL7pZ4ops@acme.it", "tok_9fQ2xL7pZ4", "tok_9fQ2xL7pZ4ops@acme.it"),
        ({"EMAIL": Policy.ALLOW}, [], [TermConfig(value="acme-intra.it", type="DOMAIN")],
         "write ops@acme-intra.it today", "acme-intra.it", "ops@acme-intra.it"),
    ],
    ids=["card-in-an-address", "known-value-in-an-address", "iban-before-a-phone", "term-in-an-address",
         "term-in-a-name", "secret-in-an-address", "domain-term-in-an-address"],
)  # fmt: skip
def test_a_masked_value_inside_any_span_let_through_never_leaves_in_clear(
    vault, policies, known, terms, text, value, masked
):
    """Whatever type a policy lets through (an address, a phone number, a name), a value of a type that is masked inside
    it never leaves in clear. Where the value let through names the two (``merge_spans``: the stronger of them), the
    masked one is masked where it sits and the rest stays as written: a card number, a value the vault knows, an IBAN
    glued to a phone number. Where the masked one names them (a term of yours, a secret), the whole is masked as it
    was in 0.9.9."""
    for entry in known:
        vault.intern(*entry)
    masker, _ = build_masker(Config(ner="off", policies=policies, terms=terms), vault)
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    replacement = next(r for r in out.replacements if value.lower() in r.original.lower())
    assert replacement.original == masked and value not in out.text
    assert out.text.endswith(text[replacement.end :]) and out.text.startswith(text[: replacement.start])
    assert restore_text(out.text, restores_for(vault.entities()))[0] == text
    assert _final_check_leaks(masker, out.text) == []


def test_a_term_of_yours_typed_domain_is_never_an_address_s_own_domain(vault):
    """An address let through keeps its own domain only where the address names what holds it (a repeat of the domain,
    a domain the vault knows): a term of yours typed DOMAIN outranks the address, which is masked whole with it, as
    0.9.9 masked it, wherever it is written."""
    terms = [TermConfig(value="acme-intra.it", type="DOMAIN")]
    masker, _ = build_masker(Config(ner="off", policies={"EMAIL": Policy.ALLOW}, terms=terms), vault)
    for text in ("write ops@acme-intra.it today", "see acme-intra.it or write ops@acme-intra.it"):
        out = masker.mask(text, SlotKind.TOOL_RESULT)
        assert "acme-intra.it" not in out.text, out.text
        assert restore_text(out.text, restores_for(vault.entities()))[0] == text
        assert _final_check_leaks(masker, out.text) == []


@pytest.mark.parametrize(
    "text",
    [f"pay {_IBAN}.ops@acme-intra.it", f"pay {_IBAN}@acme-intra.it", f"pay ops.{_IBAN}@acme-intra.it"],
    ids=["before-a-dot", "the-local-part", "after-a-dot"],
)
def test_an_address_masked_is_masked_whole_as_before(vault, text):
    """Nothing changes where the address is masked: it is one value, and its fake takes its place whole."""
    masker, _ = build_masker(Config(ner="off"), vault)
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    [replacement] = out.replacements
    assert replacement.etype == "EMAIL" and replacement.original == text.removeprefix("pay ")
    assert _IBAN not in out.text


def test_an_address_let_through_keeps_its_own_domain_which_it_still_brings_along(vault):
    """An address let through brings its domain along as before: masked elsewhere in the text, and kept in the vault.
    Inside the address it stays the address's, in this text and in the next, where the vault knows it."""
    masker, _ = build_masker(Config(ner="off", policies={"EMAIL": Policy.ALLOW}), vault)
    text = "write ops@acme-intra.it, or see api.acme-intra.it"
    out = masker.mask(text, SlotKind.TOOL_RESULT)
    assert out.text.startswith("write ops@acme-intra.it, or see api.") and "api.acme-intra.it" not in out.text
    assert [e.original for e in vault.entities()] == ["acme-intra.it"]
    again = f"pay {_IBAN}@acme-intra.it"
    masked = masker.mask(again, SlotKind.TOOL_RESULT)
    assert masked.text.endswith("@acme-intra.it") and [r.etype for r in masked.replacements] == ["IBAN"]


def _fake_of_a_link(vault: Vault) -> tuple[str, str]:
    """The fake `Mario%20Rossi` gets in a link, with ``vault``'s values, and how it reads."""
    from urllib.parse import unquote

    from latet.engine.masker import Masker

    masked = Masker(vault, []).mask("GET /people?q=Mario%20Rossi&x=1", SlotKind.TOOL_RESULT)
    [replacement] = masked.replacements
    return replacement.replacement, unquote(replacement.replacement)


def test_a_fake_written_in_escapes_never_reads_as_a_value_the_vault_knows():
    """The fake drawn for a value kept as written (`Mario%20Rossi`) is written in its escapes (`Nicola%20Greco`): it
    never reads as a value the vault knows (a real Nicola Greco) nor as another value's fake, which the model's
    `Nicola Greco` would bring back as another person."""
    from latet.engine.vault import Entity

    first = Vault(ROOT_KEY)
    first.intern("PERSON", EntityClass.NER, "Mario Rossi")
    _, reading = _fake_of_a_link(first)  # what it would read as, with nothing else in the vault
    knows = Vault(ROOT_KEY)
    knows.intern("PERSON", EntityClass.NER, "Mario Rossi")
    knows.intern("PERSON", EntityClass.NER, reading)  # a real person of that name
    assert _fake_of_a_link(knows)[1] != reading
    taken = Vault(ROOT_KEY, [Entity("e1", "PERSON", EntityClass.NER, "Luca Bianchi", reading, 0, 0.0, "ner")])
    taken.intern("PERSON", EntityClass.NER, "Mario Rossi")
    fake, other = _fake_of_a_link(taken)
    assert other != reading and taken.by_surrogate(reading).original == "Luca Bianchi"
    assert taken.by_surrogate(other) is taken.by_original("Mario%20Rossi")  # its reading is a fake of its own
    assert taken.is_surrogate(fake) and taken.is_surrogate(other)


def test_a_value_kept_as_written_comes_back_as_it_reads_where_the_model_writes_its_fake_so(vault):
    """The model sees `Nicola%20Greco` for `Mario%20Rossi` and writes `Nicola Greco` in prose or a tool call: it gets
    `Mario Rossi` back, as 0.9.8 gave it, each name of it alone too; the fake as seen gives the value as written."""
    from urllib.parse import unquote

    from latet.engine.build import build
    from latet.engine.pipeline import Session

    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    engine = build(Config(ner="off"), vault).engine
    session = Session("forms")
    masked = engine.mask_text("GET /people?q=Mario%20Rossi&x=1", SlotKind.TOOL_RESULT, session)
    fake = masked.replacements[0].replacement
    plain = unquote(fake)
    first, last = plain.split()
    cases = {fake: "Mario%20Rossi", plain: "Mario Rossi", plain.upper(): "MARIO ROSSI", first: "Mario", last: "Rossi"}
    for written, expected in cases.items():
        prose = f"I looked up {written} in the CRM."
        assert (
            restore_text(prose, engine.restores(session, json_mode=False))[0] == f"I looked up {expected} in the CRM."
        )
        call = json.dumps({"name": written}, ensure_ascii=False)
        assert json.loads(restore_text(call, engine.restores(session, json_mode=True))[0])["name"] == expected


@pytest.mark.parametrize(
    "text", ['```json\n{"owner": "' + AS_JSON + '"}\n```', "<td>Niccol&ograve; Ferri</td>"], ids=["json", "html"]
)
def test_a_value_kept_as_written_in_capitals_keeps_its_escapes(vault, text):
    """The fake in capitals comes back as the value in capitals outside its escapes: `NICCOL\\u00f2 FERRI`, never an
    escape no reader takes (`\\U00F2`, `&OGRAVE;`)."""
    from latet.engine.masker import Masker

    vault.intern("PERSON", EntityClass.NER, NICCOLO)
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    [replacement] = masked.replacements
    written = replacement.original
    capitals = restore_text(replacement.replacement.upper(), restores_for(vault.entities()))[0]
    assert capitals.replace("FERRI", "Ferri").replace("NICCOL", "Niccol") == written


O_GRAVE = "\N{LATIN SMALL LETTER O WITH GRAVE}"
UNREADABLE = "\N{REPLACEMENT CHARACTER}"


@pytest.mark.parametrize(
    ("written", "reads"),
    [
        ("Niccol" + BS + "u00f2", "Niccol" + O_GRAVE), ("Niccol" + BS * 2 + "u00f2", "Niccol" + O_GRAVE),
        (BS + "ud83e" + BS + "udd8a", "\N{FOX FACE}"), (BS * 2 + "ud83e" + BS * 2 + "udd8a", "\N{FOX FACE}"),
        (BS + "ud83e x", UNREADABLE + " x"), (BS + "udd8a", UNREADABLE), (BS + "U0001F98A", "\N{FOX FACE}"),
        (BS + "xc3" + BS + "xb2", O_GRAVE), (BS + "xf2", O_GRAVE), (BS + "xc3!", "\N{LATIN CAPITAL LETTER A WITH TILDE}!"),
        (BS + "303" + BS + "262", O_GRAVE), ("%C3%B2", O_GRAVE), ("%E2%82%AC", "\N{EURO SIGN}"), ("%40", "@"),
        ("%C3%41", "\N{LATIN CAPITAL LETTER A WITH TILDE}A"), ("%C3" + BS + "xb2", "\N{LATIN CAPITAL LETTER A WITH TILDE}\N{SUPERSCRIPT TWO}"),
        ("&#242;", O_GRAVE), ("&#xF2;", O_GRAVE), ("&#242 x", O_GRAVE + " x"), ("&ograve;", O_GRAVE), ("&amp;", "&"),
        ("&#0;", UNREADABLE), ("&#55357;", UNREADABLE), ("&nosuchname;", None), ("q=Mario+Rossi&x=1", "q=Mario Rossi&x=1"),
        ("q=a%2Bb+c", "q=a+b c"), ("C++ and a + b", None), ("say " + BS + '"hi' + BS + '"', 'say "hi"'),
        ("line" + BS + "nbreak", None), (BS * 4 + "q", None), ("https:" + BS + "/" + BS + "/acme.it", "https://acme.it"),
        ("D:" + BS + "mail" + BS + "100acme.it", None), (BS + "351", None), (BS + "303" + BS + "41", None),
        ("C:" + BS * 3 + "u00c9milie", "C:" + BS * 2 + "\N{LATIN CAPITAL LETTER E WITH ACUTE}milie"),
        (BS * 4 + "u00c9", "\N{LATIN CAPITAL LETTER E WITH ACUTE}"), (BS * 5 + "'x", BS * 4 + "'x"),
    ],
)  # fmt: skip
def test_escapes_read_as_what_they_stand_for(written, reads):
    """Each escape reads as the character it stands for: halves of a character as one, bytes as UTF-8 where they are
    and one character each elsewhere. A text whose escapes are of white space alone is not read so: blanked out
    (``escapes``), it is read already."""
    from latet.engine.escapes import decoded

    found = decoded(written)
    assert (found.text if found is not None else None) == reads


def test_what_escapes_read_is_placed_where_they_are_written():
    import random

    from latet.engine.escapes import decoded

    pieces = [
        "a", "b", " ", BS, BS * 2, "u", "x", "U", "00f2", "d83e", "dd8a", "c3", "b2", "303", "262", "%", "C3", "B2",
        "41", "&", "#", "242", ";", "ograve", "amp", "=", "+", "n", '"', "/", O_GRAVE, "\N{FOX FACE}",
    ]  # fmt: skip
    rng = random.Random(31)
    seen = 0
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 16)))
        found = decoded(text)
        if found is None:
            continue
        seen += 1
        assert found.read(0, len(text)) == found.text
        for start in range(len(found.text)):
            for end in range(start + 1, min(len(found.text), start + 4) + 1):
                written = found.written(start, end)
                assert found.read(*written) == found.text[start:end], (text, start, end)
    assert seen > 300


# 2 MB of one unit repeated: escapes of each kind, dense; runs of backslashes, of signs and of escapes left unfinished.
_DENSE = {
    "json": BS + "u00e9", "doubled": BS * 2 + "u00e9", "halves": BS + "ud83e" + BS + "udd8a",
    "bytes": BS + "xc3" + BS + "xa9", "octal": BS + "303" + BS + "251", "url": "%C3%A9", "url-ascii": "%41",
    "html": "&#233;", "html-hex": "&#xE9;", "html-named": "&eacute;", "form": "q=a+b&", "breaks": BS + "n",
    "quotes": BS + '"', "backslashes": BS, "backslashes-u": BS * 999 + "u00", "backslashes-x": BS * 999 + "x",
    "backslashes-octal": BS * 998 + "30", "percents": "%", "ampersands": "&#", "names": "&" + "a" * 40,
    "pluses": "=" + "+" * 999, "equals": "=" + "a" * 999,
}  # fmt: skip


@pytest.mark.timing
@pytest.mark.parametrize("unit", list(_DENSE))
def test_the_reading_of_what_escapes_stand_for_takes_time_in_proportion_to_the_text(vault, unit):
    """2 MB dense with escapes of each kind, and runs of backslashes, signs and escapes left unfinished: each is read
    once, its known values found in a time in proportion to its length."""
    import time

    from latet.engine.escapes import decoded
    from latet.engine.masker import Masker

    text = _DENSE[unit] * (2_000_000 // len(_DENSE[unit]))
    vault.intern("PERSON", EntityClass.NER, NICCOLO)
    masker = Masker(vault, [])
    began = time.perf_counter()
    decoded(text)
    masker.known_in(text)
    took = time.perf_counter() - began
    assert took < 3.0, took  # generous for CI; well under a second locally


# --- round 3: a fake's reading kept apart from the text, sent back, or a real value; a plus that is a sign ----------

_LINK = "GET /people?q=Mario%20Rossi&x=1"


def _reading_of_the_link_fake() -> str:
    """How the fake `Mario%20Rossi` gets in a link reads, the vault knowing Mario Rossi alone."""
    vault = Vault(ROOT_KEY)
    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    return _fake_of_a_link(vault)[1]


def _engine_knowing_mario():
    from latet.engine.build import build

    vault = Vault(ROOT_KEY)
    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    return vault, build(Config(ner="off"), vault)


def test_a_fake_written_in_escapes_never_reads_as_a_name_the_same_text_holds():
    """`Mario%20Rossi` in a link, and a reviewer named in prose as its fake would read (`Nicola Greco`, the name model
    off): the fake is drawn again, as a plain fake is where the text holds it, so the model's `Nicola Greco` stays the
    reviewer, never Mario Rossi."""
    from urllib.parse import unquote

    from latet.engine.pipeline import Session

    reading = _reading_of_the_link_fake()
    _, built = _engine_knowing_mario()
    session = Session("same text")
    masked = built.engine.mask_text(f"{_LINK}\nReviewed by {reading}.", SlotKind.TOOL_RESULT, session)
    [replacement] = masked.replacements
    assert replacement.original == "Mario%20Rossi" and unquote(replacement.replacement) != reading
    answer = f"{reading} reviewed the record."
    assert restore_text(answer, built.engine.restores(session, json_mode=False))[0] == answer


def test_a_name_that_reads_as_a_fake_written_in_escapes_is_masked_in_a_later_text():
    """After `Mario%20Rossi` went out as `Nicola%20Greco`, a prompt that names a Nicola Greco (the name model off) is
    masked as a fake sent back is (0.9.8's echo): the model's answer about that person comes back as Nicola Greco,
    never as Mario Rossi."""
    from urllib.parse import unquote

    from latet.engine.pipeline import Session

    _, built = _engine_knowing_mario()
    session = Session("later")
    link = built.engine.mask_text(_LINK, SlotKind.TOOL_RESULT, session)
    reading = unquote(link.replacements[0].replacement)
    prompt = built.engine.mask_text(f"Please write to {reading} about the order.", SlotKind.USER_TEXT, session)
    assert reading not in prompt.text
    [echo] = prompt.replacements
    answer = f"Dear {echo.replacement}, about your order"
    assert (
        restore_text(answer, built.engine.restores(session, json_mode=False))[0] == f"Dear {reading}, about your order"
    )


def test_a_real_value_that_reads_as_a_fake_written_in_escapes_wins_over_the_reading():
    """A real Nicola Greco learnt after `Mario%20Rossi` went out as `Nicola%20Greco` (a client of yours, which the final
    check blocks on, as it does not on a name the name model found): the real value wins over the fake's reading. The
    plugin's masking, which leaves fakes as they are, masks it; the final check finds it in clear; a text sent before
    with it in clear is masked when it is sent again (the conversation's history)."""
    from urllib.parse import unquote

    from latet.engine.pipeline import Session

    reading = _reading_of_the_link_fake()
    vault, built = _engine_knowing_mario()
    session = Session("real")
    notes = f"Notes: {reading} called about the invoice."
    assert built.engine.mask_text(notes, SlotKind.TOOL_RESULT, session).text == notes  # nobody knows it yet
    link = built.engine.mask_text(_LINK, SlotKind.TOOL_RESULT, session)
    assert unquote(link.replacements[0].replacement) == reading
    vault.intern("CLIENT", EntityClass.CUSTOM, reading, rule="term:client")  # a real one, from another text
    assert reading not in built.engine.mask_text(notes, SlotKind.TOOL_RESULT, session).text
    plugin = built.engine.mask_text(notes, SlotKind.TOOL_RESULT, Session("plugin"), keep_surrogates=True)
    assert reading not in plugin.text
    assert [s.etype for s in built.engine.masker.known_in(notes)] == ["CLIENT"]
    leaks = built.verifier.verify([(notes, SlotKind.TOOL_RESULT, False)], "s").leaks
    assert [leak.etype for leak in leaks] == ["CLIENT"]


@pytest.mark.parametrize(
    ("etype", "eclass", "value"),
    [
        ("PHONE", EntityClass.PII, "+393331234567"),
        ("EMAIL", EntityClass.PII, "mario.rossi+crm@acme.it"),
        ("API_KEY", EntityClass.SECRET, "sk_live_ab+cd/ef9QxLm2Zt"),
    ],
    ids=["phone", "sub-address", "token"],
)
def test_a_plus_in_a_value_found_as_it_is_written_is_a_sign(vault, etype, eclass, value):
    """A phone's `+39`, a sub-address's `+crm`, a token's `+`: a value found as it is written reads as it is written,
    never as a form's value with a space; its fake has no other reading, and restores as 0.9.8's did."""
    from urllib.parse import quote

    from latet.engine.vault import fake_reading

    entity = vault.intern(etype, eclass, value, rule="test")
    fake = entity.surrogate
    assert "+" in fake and fake_reading(entity) is None
    assert not vault.is_surrogate(fake.replace("+", " ")) and vault.by_surrogate(fake.replace("+", " ")) is None
    expected = {fake, fake.upper(), quote(fake, safe="")} if etype == "EMAIL" else {fake}
    assert {r.pattern for r in restores_for([entity])} == expected


def test_a_name_written_in_a_form_with_a_plus_keeps_its_reading(vault):
    """`Mario+Rossi` in a query is a value written in a form's escapes: its fake (`Nicola+Greco`) reads with a space,
    kept apart and given back as the value reads."""
    from latet.engine.masker import Masker
    from latet.engine.vault import fake_reading

    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    masked = Masker(vault, []).mask("GET /people?q=Mario+Rossi&x=1", SlotKind.TOOL_RESULT)
    [replacement] = masked.replacements
    entity = vault.by_original("Mario+Rossi")
    assert replacement.replacement == entity.surrogate and "+" in entity.surrogate
    reading = entity.surrogate.replace("+", " ")
    assert fake_reading(entity) == reading and vault.is_surrogate(reading)
    assert restore_text(f"call {reading} now", restores_for([entity]))[0] == "call Mario Rossi now"


def test_a_phone_found_where_a_query_writes_its_spaces_as_plus_has_a_reading(vault):
    """`333 1234567` known, `333+1234567` in a query string: found where the query's `+` read as spaces, masked, its
    entry says it was found so (``Entity.decoded``), and its fake as it reads comes back as the value. With no record of
    how a value was found, 0.9.9 read a `+` as a form's space in a value written in words alone, and gave the phone's
    fake no reading: the model's `345 7654321` came back as it was."""
    from latet.engine.masker import Masker
    from latet.engine.vault import fake_reading

    vault.intern("PHONE", EntityClass.PII, "333 1234567")
    text = "GET /api/customers?tel=333+1234567&page=2 200"
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    assert "1234567" not in masked.text
    entity = vault.by_original("333+1234567")
    assert entity is not None and entity.decoded is True and "+" in entity.surrogate
    assert masked.text == text.replace("333+1234567", entity.surrogate)
    reading = fake_reading(entity)
    assert reading == entity.surrogate.replace("+", " ") and vault.is_surrogate(reading)
    restores = restores_for(vault.entities())
    assert restore_text(f"call {reading} today", restores)[0] == "call 333 1234567 today"
    assert restore_text(masked.text, restores)[0] == text


def test_a_name_whose_query_writes_a_space_as_plus_beside_a_json_escape_keeps_its_reading(vault):
    """`René Dupont` known, `Ren\\u00e9+Dupont` in a link held in a JSON string: found where the escapes read, its
    `+` a form's space as in any value written in words, so its fake as it reads comes back as the value, as 0.9.9
    gave it back (round 1, M2)."""
    from latet.engine.masker import Masker
    from latet.engine.vault import fake_reading

    vault.intern("PERSON", EntityClass.NER, "Ren\N{LATIN SMALL LETTER E WITH ACUTE} Dupont")
    text = '{"url": "https://x.example/?q=Ren\\u00e9+Dupont"}'
    masked = Masker(vault, []).mask(text, SlotKind.TOOL_RESULT)
    assert "Dupont" not in masked.text
    entity = vault.by_original("Ren\\u00e9+Dupont")
    assert entity is not None and entity.decoded is True and "+" in entity.surrogate
    reading = fake_reading(entity)
    assert reading == entity.surrogate.replace("+", " ")
    restores = restores_for(vault.entities())
    assert restore_text(f"Call {reading} today.", restores)[0] == "Call Ren\\u00e9 Dupont today."
    assert restore_text(masked.text, restores)[0] == text


def test_how_a_value_was_found_decides_its_reading_and_an_entry_kept_before_keeps_the_old_rule(vault):
    """A reading is computed for an entry found where escapes read as what they stand for (``decoded``), and only for
    it: a value found as it is written that holds `%41` has none. An entry kept before Latet recorded how its value was
    found (None) keeps 0.9.9's rule: a `+` counts as a form's space in a value written in words alone."""
    from dataclasses import replace
    from urllib.parse import quote

    from latet.engine.vault import Entity, fake_reading

    password = vault.intern("PASSWORD", EntityClass.SECRET, "pa%41ss-Lavender-77", rule="test")
    assert password.decoded is False and fake_reading(password) is None
    assert {r.pattern for r in restores_for([password])} == {password.surrogate, quote(password.surrogate, safe="")}
    name = Entity("n" * 20, "PERSON", EntityClass.NER, "Mario+Rossi", "Nicola+Greco", 0, 0.0, "known")
    phone = Entity("p" * 20, "PHONE", EntityClass.PII, "333+1234567", "345+7654321", 0, 0.0, "known")
    assert (name.decoded, phone.decoded) == (None, None)
    assert fake_reading(name) == "Nicola Greco" and fake_reading(phone) is None  # 0.9.9's rule, kept
    assert fake_reading(replace(name, decoded=False)) is None
    assert fake_reading(replace(phone, decoded=True)) == "345 7654321"
    found = replace(phone, decoded=True)
    assert restore_text("call 345 7654321", restores_for([found]))[0] == "call 333 1234567"
    signed = Entity("s" * 20, "PHONE", EntityClass.PII, "+39 333 \\u0031234567", "+39 345 7654321", 0, 0.0, "k", True)
    assert fake_reading(signed) is None  # its `+` is a sign: the reading is of its `\u` escape, and reads as the fake
