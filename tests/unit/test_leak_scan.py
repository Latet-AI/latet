"""``latet leak``'s scan: the secrets of the agents' sessions, grouped by value, never shown."""

from __future__ import annotations

import base64
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from latet.config import Config
from latet.engine.detectors.terms import VALUE_LONGEST
from latet.engine.types import Policy
from latet.leak import scan as scan_module
from latet.leak.scan import hint, scan
from latet.leak.sources import Skipped, Text

GITHUB = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
ANTHROPIC = (
    "sk-ant-api03-"
    + "Qm7Zx2Lp9Vn4Bk8Rt3Wc6Yd1Hs5Fj0GaPe8Ux2Kc7Nb4Vr9Tz3Mq6Wy1Jd5Sh0Lf8Xg2Ra7Eo4Ci9Bu3Yt6Kp1Dn5Hv-Zq8AA"
)
PASSWORD = "Zq7#Kx9!" + "Lm2vBn4Jt"
EMAIL = "jane.doe" + "@example-corp.it"
BADGE_REF = {
    "name": "card-ref",
    "type": "CARD_REF",
    "pattern": r"\bCR(?P<value>\d{8})\b",
    "checksum": "luhn",
    "class": "secret",
    "examples": {"match": ["ref CR12345674"], "no_match": ["ref CR12345678"]},
}
CONFIG = Config(ner="off")


def day(month: int, d: int) -> datetime:
    return datetime(2026, month, d, 12, 0, tzinfo=UTC)


def text(session: str, body: str, when: datetime | None = None, agent: str = "claude", folder: str | None = None):
    return Text(agent=agent, session=session, folder=folder, time=when, text=body)


def test_a_secret_in_two_sessions_is_one_finding_with_two_sessions():
    report = scan(
        [
            text("s1", f"export GITHUB_TOKEN={GITHUB}", day(10, 2), folder="C:/a"),
            text("s2", f"the token {GITHUB} again", day(10, 9), agent="codex", folder="C:/b"),
        ],
        CONFIG,
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.in_clear.sessions == 2
    assert finding.replaced.sessions == 0
    assert finding.agents == frozenset({"claude", "codex"})
    assert finding.folders == ("C:/a", "C:/b")
    assert finding.in_clear.first == day(10, 2) and finding.in_clear.last == day(10, 9)
    assert report.sessions == {"claude": 1, "codex": 1}


def test_a_string_that_arrives_twice_in_one_session_counts_the_session_once():
    report = scan([text("s1", f"token {GITHUB}"), text("s1", f"another line with {GITHUB} in it")], CONFIG)
    assert report.findings[0].in_clear.sessions == 1


def test_the_same_text_twice_in_a_session_is_scanned_once(monkeypatch):
    calls: list[str] = []
    real = scan_module.build_masker

    def counting(*args, **kwargs):
        masker, statuses = real(*args, **kwargs)
        inner = masker.planned

        def planned(body, *a, **k):
            calls.append(body)
            return inner(body, *a, **k)

        masker.planned = planned  # pyright: ignore[reportAttributeAccessIssue]
        return masker, statuses

    monkeypatch.setattr(scan_module, "build_masker", counting)
    body = f"export GITHUB_TOKEN={GITHUB}"
    report = scan([text("s1", body), text("s1", body), text("s2", body)], CONFIG)
    assert len(calls) == 1
    assert report.findings[0].in_clear.sessions == 2


def test_the_scan_teaches_its_scratch_vault_nothing(monkeypatch):
    """Each text is read for what it holds alone: a scratch vault that learnt every value found would have every later
    text read for all of them and for their fakes, the whole cost of a long scan."""
    from latet.engine.vault import Vault

    learnt: list[str] = []
    real = Vault.intern

    def intern(self, etype, *a, **k):
        learnt.append(etype)
        return real(self, etype, *a, **k)

    monkeypatch.setattr(Vault, "intern", intern)
    body = f"password: {PASSWORD}\nexport GITHUB_TOKEN={GITHUB} mail {EMAIL}"
    report = scan([text("s1", body), text("s2", f"again {GITHUB}")], CONFIG, personal=True)
    assert learnt == []
    assert {f.etype for f in report.findings} >= {"PASSWORD"} and report.personal.get("EMAIL") == 1
    assert max(f.in_clear.sessions for f in report.findings) == 2


def test_the_findings_are_the_same_whatever_order_the_texts_come_in():
    """A value found in one text by what is written around it (``password:``) is not looked for in the others: what a
    session holds is found in its own texts, whichever session is read first."""
    named, bare = text("s1", f"password: {PASSWORD}", day(10, 2)), text("s2", f"it was {PASSWORD}", day(10, 3))
    first = scan([named, bare], CONFIG).findings
    later = scan([bare, named], CONFIG).findings
    assert first == later and [f.in_clear.sessions for f in first] == [1]


def test_the_name_model_is_not_loaded_for_the_scan(monkeypatch):
    """The local name model reads none of a tool's results, the kind each saved text is read as: it would find nothing
    and only take the time and the memory to load."""
    from latet.engine import build

    loaded: list[str] = []
    monkeypatch.setattr(build.ner, "detector_for", lambda *a, **k: loaded.append("ner"))
    scan([text("s1", f"a {GITHUB}")], Config(ner="auto"), personal=True)
    assert loaded == []


def test_a_texts_findings_do_not_depend_on_the_texts_next_to_it():
    """Read joined (``Masker.joined``), a call's texts find each other's values: a value written bare beside the text
    that names it would count, and one a few texts further would not. Each is read alone."""
    named, bare = f"password: {PASSWORD}", f"it was {PASSWORD}"
    near = [text("s1", named, day(10, 2)), text("s2", bare, day(10, 3))]
    apart = [near[0], *(text("s3", f"line {n}", day(10, 3)) for n in range(600)), near[1]]
    assert scan(near, CONFIG).findings == scan(apart, CONFIG).findings
    assert [f.in_clear.sessions for f in scan(near, CONFIG).findings] == [1]


def test_the_scan_tells_its_progress_by_counts_alone():
    told: list[tuple[str, int]] = []
    texts = [text("s1", f"a {GITHUB}"), text("s1", "b"), text("s2", "c"), text("s3", "d", agent="codex")]
    scan([*texts, Skipped("x.jsonl", "unreadable")], CONFIG, progress=lambda agent, n: told.append((agent, n)))
    assert told[:3] == [("claude", 1), ("claude", 2), ("codex", 1)]
    assert all(isinstance(agent, str) and isinstance(n, int) for agent, n in told)


def test_findings_are_secrets_alone_unless_personal_is_asked():
    body = f"mail {EMAIL} and export GITHUB_TOKEN={GITHUB}"
    plain = scan([text("s1", body)], CONFIG)
    assert [f.etype for f in plain.findings] and all(f.eclass == "secret" for f in plain.findings)
    assert plain.personal == {}
    asked = scan([text("s1", body)], CONFIG, personal=True)
    assert [f.etype for f in asked.findings] == [f.etype for f in plain.findings]
    assert asked.personal.get("EMAIL") == 1


def test_the_hint_shows_a_typed_prefix_alone():
    assert hint("GITHUB_PAT", GITHUB) == f"ghp_… ({len(GITHUB)} characters)"
    assert hint("ANTHROPIC_API_KEY", ANTHROPIC) == f"sk-ant-api03-… ({len(ANTHROPIC)} characters)"


def test_an_untyped_secret_shows_no_character_of_it():
    shown = hint("PASSWORD", PASSWORD)
    assert str(len(PASSWORD)) in shown
    assert not any(PASSWORD[i : i + 2] in shown for i in range(len(PASSWORD) - 1))
    assert "Password" in shown


def test_no_value_is_in_the_report():
    report = scan(
        [
            text(
                "s1",
                f"password: {PASSWORD}\nexport GITHUB_TOKEN={GITHUB}\nexport ANTHROPIC_API_KEY={ANTHROPIC} {EMAIL}",
                day(10, 2),
            )
        ],
        CONFIG,
        personal=True,
    )
    assert report.findings
    dumped = repr(report) + "".join(repr(dataclasses.asdict(f)) for f in report.findings)
    for value in (GITHUB, ANTHROPIC, PASSWORD, EMAIL):
        assert value not in dumped
    assert GITHUB[4:] not in dumped and ANTHROPIC[13:] not in dumped


def test_the_users_terms_and_recognizers_are_used():
    config = Config(ner="off", recognizers=[BADGE_REF])
    report = scan([text("s1", "ref CR12345674 here")], config)
    assert [f.etype for f in report.findings] == ["CARD_REF"]
    assert "12345674" not in repr(report)


class FakeKnown:
    def __init__(self, since=None, masked=None, began=None, horizon=None):
        self._since, self._masked, self._began, self._horizon = since, dict(masked or {}), began, horizon
        self.since_calls: list[tuple[str, str]] = []
        self.masked_calls: list[tuple[str, str]] = []

    def since(self, etype, value):
        self.since_calls.append((etype, value))
        return self._since

    def masked_in(self, etype, value):
        self.masked_calls.append((etype, value))
        return self._masked

    def latet_since(self):
        return self._began

    def history_horizon(self):
        return self._horizon


SLACK = scan_module.SINCE_SLACK
REDACTING = Config(ner="off", policies={"GITHUB_PAT": Policy.REDACT})


def where(finding) -> tuple[int, int, int, int]:
    """A finding's sessions: in clear, replaced, of which confirmed, and redacted."""
    return (finding.in_clear.sessions, finding.replaced.sessions, finding.replaced.confirmed, finding.redacted.sessions)


def test_known_is_asked_once_per_secret():
    known = FakeKnown(since=day(10, 12))
    scan(
        [
            text("s1", f"a {GITHUB}", day(10, 2)),
            text("s2", f"b {GITHUB}", day(10, 20)),
            text("s3", f"export ANTHROPIC_API_KEY={ANTHROPIC}"),
        ],
        CONFIG,
        known=known,
    )
    assert len(known.since_calls) == len(set(known.since_calls)) == 2
    assert len(known.masked_calls) == len(set(known.masked_calls)) == 2


def test_sessions_before_latet_knew_a_secret_are_in_clear_and_after_are_replaced():
    texts = [text("old", f"a {GITHUB}", day(10, 2)), text("new", f"b {GITHUB}", day(10, 20))]
    report = scan(texts, CONFIG, known=FakeKnown(since=day(10, 12)))
    finding = report.findings[0]
    assert finding.in_clear.sessions == 1 and finding.replaced.sessions == 1
    assert finding.in_clear.last == day(10, 2) and finding.replaced.first == day(10, 20)
    assert finding.replaced.confirmed == 0
    known = FakeKnown(since=day(10, 12), masked={"new": day(10, 20), "other": day(10, 21)})
    assert where(scan(texts, CONFIG, known=known).findings[0]) == (1, 1, 1, 0)


def test_a_secret_the_vault_does_not_know_is_in_clear_and_without_known_everything_is():
    texts = [text("s1", f"a {GITHUB}", day(10, 20))]
    assert scan(texts, CONFIG, known=FakeKnown(since=None)).findings[0].replaced.sessions == 0
    assert scan(texts, CONFIG).findings[0].replaced.sessions == 0


def test_a_session_without_a_time_is_in_clear_when_the_vault_knows_the_secret():
    report = scan([text("s1", f"a {GITHUB}", None)], CONFIG, known=FakeKnown(since=day(10, 12)))
    assert report.findings[0].in_clear.sessions == 1


def test_the_session_latet_first_saw_a_secret_in_is_replaced():
    """A transcript writes a prompt or a tool's result before the request that carries it reaches Latet, which keeps
    the value as it masks that request: the session Latet first saw it in has its text a little before the vault's
    date. Within ``SINCE_SLACK`` of it a session counts as replaced; one the history names, as confirmed too."""
    first_seen = day(10, 12)
    texts = [
        text("first", f"a {GITHUB}", first_seen - timedelta(seconds=40)),  # the history keeps its request
        text("pruned", f"b {GITHUB}", first_seen - SLACK + timedelta(seconds=1)),
        text("older", f"c {GITHUB}", first_seen - SLACK - timedelta(seconds=1)),
    ]
    finding = scan(texts, CONFIG, known=FakeKnown(since=first_seen, masked={"first": first_seen})).findings[0]
    assert where(finding) == (1, 2, 1, 0)
    assert finding.in_clear.last == first_seen - SLACK - timedelta(seconds=1)


def test_a_session_the_history_names_is_confirmed_from_its_first_masking_on():
    """The history names a session with the first time it shows the value masked there. A session that held the value
    since well before (Latet put in front of its agent halfway, or a transcript sent again) sent it in clear first: it
    is in clear, the vault's date notwithstanding. One whose first sight is within the slack of that masking is replaced
    and confirmed, also where the vault's entry is newer (a value forgotten and met again), and one with no time to
    place against it is in clear."""
    texts = [
        text("before-forget", f"a {GITHUB}", day(10, 2)),
        text("halfway", f"b {GITHUB}", day(10, 1)),
        text("timeless", f"c {GITHUB}", None),
        text("never-through-latet", f"d {GITHUB}", day(10, 3)),
    ]
    masked = {
        "before-forget": day(10, 2) + timedelta(seconds=30),
        "halfway": day(10, 9),
        "timeless": day(10, 9),
    }
    finding = scan(texts, CONFIG, known=FakeKnown(since=day(10, 20), masked=masked)).findings[0]
    assert where(finding) == (3, 1, 1, 0)
    assert finding.replaced.first == day(10, 2)
    # Named, the vault knew the value before the session began, and the history reaches back past the session's start:
    # it shows no masking then, and its masking came later. Latet was put in front of the agent halfway.
    known = FakeKnown(since=day(9, 20), masked={"halfway": day(10, 9)}, horizon=day(9, 25))
    assert where(scan([text("halfway", f"b {GITHUB}", day(10, 1))], CONFIG, known=known).findings[0]) == (1, 0, 0, 0)


def test_a_named_session_older_than_the_history_is_split_by_the_vault_date():
    """The history keeps 14 days: a session whose first requests it pruned has a first masking later than Latet's real
    one, and the history cannot tell what Latet did before its oldest request (``history_horizon``). Such a session is
    split by the vault's date, as an unnamed one: replaced, not confirmed, where the vault knew the value from its start;
    in clear where it did not, or has no date."""
    texts = [text("long", f"a {GITHUB}", day(10, 2))]
    masked, horizon = {"long": day(10, 20)}, day(10, 18)
    pruned = FakeKnown(since=day(10, 1), masked=masked, horizon=horizon)
    assert where(scan(texts, CONFIG, known=pruned).findings[0]) == (0, 1, 0, 0)
    later = FakeKnown(since=day(10, 10), masked=masked, horizon=horizon)
    assert where(scan(texts, CONFIG, known=later).findings[0]) == (1, 0, 0, 0)
    undated = FakeKnown(since=None, masked=masked, horizon=horizon)
    assert where(scan(texts, CONFIG, known=undated).findings[0]) == (1, 0, 0, 0)
    # The history's horizon just within the slack of the session's start: it sees that far back, and decides.
    edge = FakeKnown(since=day(10, 1), masked=masked, horizon=day(10, 2) - SLACK)
    assert where(scan(texts, CONFIG, known=edge).findings[0]) == (1, 0, 0, 0)
    # No horizon to tell how far back the history sees: the vault's date decides.
    blind = FakeKnown(since=day(10, 1), masked=masked, horizon=None)
    assert where(scan(texts, CONFIG, known=blind).findings[0]) == (0, 1, 0, 0)


def test_a_secret_your_settings_redact_is_redacted_where_latet_was_there():
    """Latet sent a marker in its place, in any session that went through it, and kept nothing: no date in the vault. A
    session from Latet's first date on this computer on is one it may have redacted it in (``Finding.redacted``, never
    ``replaced``); one before, or with no time to place, is in clear: Latet was not there."""
    began = day(10, 10)
    texts = [
        text("before-latet", f"a {GITHUB}", began - SLACK - timedelta(seconds=1)),
        text("from-latet", f"b {GITHUB}", began - SLACK + timedelta(seconds=1)),
        text("later", f"c {GITHUB}", day(10, 15)),
        text("timeless", f"d {GITHUB}", None),
    ]
    finding = scan(texts, REDACTING, known=FakeKnown(since=None, began=began)).findings[0]
    assert where(finding) == (2, 0, 0, 2)
    assert finding.redacted.first == began - SLACK + timedelta(seconds=1) and finding.redacted.last == day(10, 15)
    [masked] = scan(texts, CONFIG, known=FakeKnown(since=None, began=began)).findings
    assert where(masked) == (4, 0, 0, 0)


def test_a_redacted_secret_with_no_date_of_latet_is_redacted_in_every_session():
    """Without Latet's answers, or where it has no date on this computer, a redacted secret's sessions are all ones it
    may have redacted it in: neither in clear nor replaced (the command says "may")."""
    texts = [text("s1", f"a {GITHUB}", day(10, 2)), text("s2", f"b {GITHUB}", None)]
    for known in (None, FakeKnown(since=None, began=None)):
        assert where(scan(texts, REDACTING, known=known).findings[0]) == (0, 0, 0, 2)


def test_a_secret_moved_to_redact_keeps_the_sessions_latet_masked_it_in():
    """A value the vault keeps was masked once, before the settings moved its type to redact: the sessions before the
    vault knew it are in clear, those the history shows it masked in replaced and confirmed, and the others from the
    vault's date on, a fake or a marker left, which the scan cannot tell, redacted."""
    texts = [
        text("before-vault", f"a {GITHUB}", day(10, 5)),
        text("masked-then", f"b {GITHUB}", day(10, 13)),
        text("after", f"c {GITHUB}", day(10, 25)),
    ]
    known = FakeKnown(since=day(10, 12), masked={"masked-then": day(10, 13)}, began=day(10, 1))
    assert where(scan(texts, REDACTING, known=known).findings[0]) == (1, 1, 1, 1)


def test_a_secret_too_long_for_the_vault_is_redacted():
    """A value longer than a vault keeps (``VALUE_LONGEST``) is redacted whatever the settings say."""
    encoded = base64.b64encode(bytes(range(256)) * 50).decode()
    body = "".join(encoded[at : at + 64] + "\n" for at in range(0, len(encoded), 64))
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----"
    assert len(pem) > VALUE_LONGEST
    [finding] = scan([text("s1", f"key:\n{pem}\n", day(10, 2))], CONFIG).findings
    assert where(finding) == (0, 0, 0, 1)


def test_a_secret_in_claude_codes_tool_result_is_found_and_one_only_in_its_screen_copy_is_not(tmp_path, monkeypatch):
    """Claude Code keeps a tool's result twice: the ``tool_result`` block the request carries, and ``toolUseResult``
    for its own screen (an edit's whole ``originalFile``), which never reached the provider."""
    import json

    from latet.leak import sources

    screen = f"password: {PASSWORD}\nGITHUB_TOKEN={GITHUB}"
    assert "PASSWORD" in {f.etype for f in scan([text("s1", screen)], CONFIG).findings}  # read, it would be found
    record = {
        "sessionId": "s1",
        "type": "user",
        "timestamp": "2026-10-02T10:00:00.000Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": f"GITHUB_TOKEN={GITHUB}"}]},
        "toolUseResult": {"originalFile": screen},
    }
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    path = tmp_path / ".claude" / "projects" / "p" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    report = scan(sources.read(["claude"], home=tmp_path), CONFIG)
    assert [f.etype for f in report.findings] == ["GITHUB_PAT"]


def test_skipped_are_kept_and_counted_unread():
    skipped = Skipped("a.jsonl", "unreadable")
    report = scan([skipped, text("s1", "nothing here")], CONFIG)
    assert report.skipped == [skipped] and report.unread == 1 and report.findings == []


def test_a_failure_of_the_detectors_is_skipped_and_never_names_the_text(monkeypatch):
    real = scan_module.build_masker

    def broken(*args, **kwargs):
        masker, statuses = real(*args, **kwargs)

        def planned(body, *a, **k):
            raise RuntimeError(body)

        masker.planned = planned  # pyright: ignore[reportAttributeAccessIssue]
        return masker, statuses

    monkeypatch.setattr(scan_module, "build_masker", broken)
    report = scan([text("s1", f"a {GITHUB}")], CONFIG)
    assert report.unread == 1 and GITHUB not in repr(report)


def test_a_hint_keeps_a_vendors_prefix_and_none_of_the_random_part():
    """What a fake kept before 0.15.0 (``secret_prefix_length``) could run into the random part (`sk-proj-AbCdEf_`) or
    be the words of a password (`Summer2024-Company-`): a hint keeps what the fake keeps now (``public_prefix``), the
    prefix a vendor writes before every key, Latet's own prefix of its tokens, and nothing of a type any value can
    have."""
    openai = "sk-proj-" + "AbCdEf_GhIjKlMnOpQrStUvWx12"
    assert hint("OPENAI_API_KEY", openai) == f"sk-proj-… ({len(openai)} characters)"
    admin = "latet_adm_" + "Xk3j9_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T"
    assert hint("LATET_ADMIN_TOKEN", admin) == f"latet_adm_… ({len(admin)} characters)"
    for etype, value in [
        ("PASSWORD", "Summer2024-Company-Xy7q9w2kLmNo"),
        ("ASSIGNED_SECRET", "deploy-key-prod-a8f3k2m9x7"),
        ("GENERIC_API_KEY", "acme-int-Zt8q2Lw9Rk3m"),
        ("HASHICORP_TF_PASSWORD", "terraform-admin-pass-9xk2m7q4w"),
        ("CARD_REF", "ref-team-a-4k2m9x7q3w"),
    ]:
        assert hint(etype, value) == f"{scan_module.label(etype)} ({len(value)} characters)", etype
    assert hint("AWS_ACCESS_TOKEN", "AKIA" + "Q7W2K9M3X8L4P6R1") == "AKIA… (20 characters)"


@pytest.mark.parametrize("etype", ["GITHUB_TOKEN", "PASSWORD"])
def test_the_label_is_never_empty(etype):
    assert scan_module.label(etype)


def _jwt() -> str:
    def part(claims: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'sub': '1234567890', 'iat': 1716239022})}." + (
        "Qm7Zx2Lp9Vn4Bk8Rt3Wc6Yd1Hs5Fj0GaPe8Ux2Kc7N"
    )


def _curl(value: str) -> str:
    return f'curl -H "Authorization: Bearer {value}" https://api.example.com/v1/items'


BEARER = "Zt8q2Lw9" + "Rk3mXv7Bn4Jc1Hs6"  # a made-up token of no vendor's


@pytest.mark.parametrize(
    ("bodies", "shown", "types"),
    [
        # A JWT in a curl option's header and bare: as often, so the type it was first found as.
        pytest.param(
            [_curl(_jwt()), f"the token is {_jwt()}"], "CURL_AUTH_HEADER", ("CURL_AUTH_HEADER", "JWT"), id="jwt"
        ),
        # A token in a curl option's header once and assigned to a name twice: the type found most often.
        pytest.param(
            [_curl(BEARER), f"export API_TOKEN={BEARER}", f"API_TOKEN={BEARER} make deploy"],
            "GENERIC_API_KEY",
            ("GENERIC_API_KEY", "CURL_AUTH_HEADER"),
            id="token",
        ),
        # A GitHub token in a curl option's header twice and bare once: a vendor's type first, whatever the counts.
        pytest.param(
            [_curl(GITHUB), _curl(GITHUB) + " -s", f"export GITHUB_TOKEN={GITHUB}"],
            "GITHUB_PAT",
            ("GITHUB_PAT", "CURL_AUTH_HEADER"),
            id="vendor",
        ),
    ],
)
def test_one_value_found_as_two_types_is_one_secret(bodies, shown, types):
    """The same secret once: a value is one finding whatever types it was found as, shown as a vendor's type first,
    then the type it was found as most often, then the first; its hint is the type shown's; ``types`` has them all."""
    report = scan([text(f"s{n}", body, day(10, 2 + n)) for n, body in enumerate(bodies)], CONFIG)
    [finding] = report.findings
    assert finding.etype == shown and finding.types == types
    assert finding.in_clear.sessions == len(bodies)
    value = next(v for v in (_jwt(), BEARER, GITHUB) if v in bodies[0])
    assert finding.hint == hint(shown, value)
    assert value not in repr(report)


def test_a_value_is_redacted_only_where_every_type_it_was_found_as_is():
    bodies = [_curl(BEARER), f"export API_TOKEN={BEARER}"]
    texts = [text(f"s{n}", body, day(10, 2 + n)) for n, body in enumerate(bodies)]
    one = Config(ner="off", policies={"CURL_AUTH_HEADER": Policy.REDACT})
    assert where(scan(texts, one).findings[0]) == (2, 0, 0, 0)
    both = Config(ner="off", policies={"CURL_AUTH_HEADER": Policy.REDACT, "GENERIC_API_KEY": Policy.REDACT})
    assert where(scan(texts, both).findings[0]) == (0, 0, 0, 2)


def test_each_list_has_the_agents_and_folders_of_its_own_sessions():
    texts = [
        text("old", f"a {GITHUB}", day(10, 2), folder="C:/shop"),
        text("new", f"b {GITHUB}", day(10, 20), agent="codex", folder="C:/billing"),
    ]
    [finding] = scan(texts, CONFIG, known=FakeKnown(since=day(10, 12))).findings
    assert (finding.in_clear.agents, finding.in_clear.folders) == (frozenset({"claude"}), ("C:/shop",))
    assert (finding.replaced.agents, finding.replaced.folders) == (frozenset({"codex"}), ("C:/billing",))
    assert finding.agents == frozenset({"claude", "codex"}) and finding.folders == ("C:/billing", "C:/shop")


def test_a_lone_surrogate_loses_no_text():
    """A JSON string may hold half of a pair (`\\ud83d` alone), which the detectors cannot read: it is read as the
    replacement character, one for one, so that the text's secrets are found where they are."""
    body = f"an emoji cut in half {chr(0xD83D)} and the token {GITHUB}"
    report = scan([text("s1", body, day(10, 2))], CONFIG)
    assert report.skipped == [] and [f.etype for f in report.findings] == ["GITHUB_PAT"]
    assert report.findings[0].hint == hint("GITHUB_PAT", GITHUB)


def test_a_text_the_detectors_fail_on_is_said_once(monkeypatch):
    real = scan_module.build_masker

    def broken(*args, **kwargs):
        masker, statuses = real(*args, **kwargs)

        def planned(body, *a, **k):
            raise RuntimeError("no")

        masker.planned = planned  # pyright: ignore[reportAttributeAccessIssue]
        return masker, statuses

    monkeypatch.setattr(scan_module, "build_masker", broken)
    body = f"a {GITHUB}"
    report = scan([text("s1", body), text("s1", body), text("s2", body)], CONFIG)
    assert len(report.skipped) == 1 and report.unread == 1


def test_the_cache_keeps_no_text():
    """Memory stays bounded: what the scan remembers of a text it read is a digest, never the text, however short."""
    import gc

    class Kept(str):  # its __dict__ has the garbage collector see each holder, as it does not a plain string's
        pass

    bodies = [Kept(f"note {n} with some words in it") for n in range(3)] + [Kept("x" * 300 + f" {n}") for n in range(2)]
    holders: list[list[str]] = []

    def texts():
        for n, body in enumerate(bodies):
            yield text(f"s{n}", body)
        holders.extend([type(r).__name__ for r in gc.get_referrers(b) if isinstance(r, dict)] for b in bodies)

    scan(texts(), CONFIG)
    assert holders == [[] for _ in bodies]
