import json
import unicodedata

import pytest
from stdnum import iban as std_iban
from stdnum import luhn as std_luhn
from stdnum.it import codicefiscale, iva

from latet.config import Config, TermConfig
from latet.engine.build import build_masker
from latet.engine.rehydrate import StreamRehydrator, restore_text, restores_for
from latet.engine.types import EntityClass, SlotKind
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
    # A backslash written as one (`\\`) and then an `n` is a letter: the fake is part of a longer word, and stays.
    glued = json.dumps({"path": "C:\\n" + client.surrogate})
    assert restore_text(glued, restores)[0] == glued


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
