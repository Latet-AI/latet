"""How fast a Latet server's applications API masks and unmasks log lines and documents, on one API key.

uv run python scripts/bench_apps.py --url http://127.0.0.1:5283 --token-file token.txt
uv run python scripts/bench_apps.py --url URL --token-file FILE --lines 20000 --batch 500 --documents 50 --repeat 0.5

It masks ``--lines`` log lines (``kind`` data), ``--batch`` in a call, then unmasks what came back, the same way; then
``--documents`` documents of about 20 KB of prose (``kind`` prose), one in a call, masked and unmasked (``--no-unmask``:
masked alone). Each line is about 200 characters and holds an email address and an IPv4 address, and the ``--repeat``
share of the lines names a customer named in a line before, a customer named often being named more often, as in a
real log; with ``--session-tokens``, each line ends with a session token of its own (``session_token=`` and 16 hex
digits), a short secret that the key's space holds in memory for good, glued (its newest ``key_held_secrets``, 1,000
unless the server's settings say otherwise), as a log with a token in every line fills that set. A document holds ten
values: four email addresses, three phone numbers, two IBANs, an IPv4 address. It prints
one line for each of the four: the calls, the texts a second, the megabytes of text a second, and how long a call took,
half of them and 95 in 100 less, and the values each text had replaced (mask) or restored (unmask); then the calls
the server refused, and the texts that did not come back as they were sent.

The token is read from a file (``--token-file``): on the command line it would be in the shell's history and in the
list of the computer's processes. Every value in the texts is made up. The key's vault keeps every value it masks:
measure with a key made for it.

The public IPv4 addresses come from a pool of ``ADDRESSES``, as when the numbers of Latet 0.10.0's documentation were
first measured (before 0.9.10, a vault gave fakes to about 770 public IPv4 addresses at most), so that they compare."""

from __future__ import annotations

import argparse
import functools
import ipaddress
import itertools
import random
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from latet.engine.surrogates.data.names import (
    ENGLISH_FIRST_NAMES,
    ENGLISH_LAST_NAMES,
    ITALIAN_FIRST_NAMES,
    ITALIAN_LAST_NAMES,
)

APPS_API = "/_latet/apps/v1"  # latet.bridge.apps.APPS_API, written here so that the script imports no server code
MOST_TEXTS = 1000  # texts one call takes at most (latet.bridge.apps.MOST_TEXTS)
ADDRESSES = 400  # the public IPv4 addresses the lines and the documents name (the module's words)
FIRST_NAMES = 2_000  # given names: the common ones Latet's fakes are drawn from, then made-up rarer ones
LAST_NAMES = 20_000  # family names, the same way
COMPANIES = 3_000  # the companies whose domains the addresses that are not a public mailbox's are at
DOCUMENT_CHARS = 20_000

# How many people have each name, as in a population: the name of rank r has a share that falls as 1 / (r + q), q
# flattening the head. The most common given name is about 4 in 100 people's, the most common family name about 1 in
# 100; the 80 commonest given names are half of all, the 80 commonest family names a quarter. A company of rank r, the
# same way: the largest has about 1.5 in 100 of the addresses at a company.
_FIRST_FLAT, _LAST_FLAT, _COMPANY_FLAT = 3, 10, 10
# The mailboxes, and how many of the addresses at one are at each.
_MAILBOXES = {
    "gmail.com": 45, "outlook.com": 8, "hotmail.com": 8, "yahoo.com": 7, "libero.it": 6, "icloud.com": 5,
    "hotmail.it": 4, "yahoo.it": 3, "live.com": 3, "virgilio.it": 3, "alice.it": 2, "gmx.de": 2, "web.de": 2,
    "protonmail.com": 2,
}  # fmt: skip
_SYLLABLES = "ka ro mi ten bel dor fa li nu sar vek to lan ber mo zi ga pe rin sol da ve qua mar ost elm".split()  # noqa: SIM905
_TLDS = ("com", "it", "de", "fr", "es", "eu", "io", "co.uk", "net", "ch")
_STATUSES = {200: 60, 201: 8, 204: 5, 302: 4, 400: 5, 401: 4, 403: 2, 404: 6, 409: 2, 500: 3, 502: 1}
_SERVICES = ("checkout-api", "billing", "auth-service", "notifications", "orders", "payments", "accounts")
_PATHS = ("orders", "invoices", "carts", "subscriptions", "payments", "addresses", "profile")
# Italy's mobile prefixes, which the phone detector takes as real numbers.
_MOBILE = (320, 324, 327, 328, 329, 330, 333, 334, 335, 338, 339, 340, 342, 345, 346, 347, 348, 349, 350, 366, 380)


def _made_up(rng: random.Random, taken: set[str]) -> str:
    """A name no list holds and none made before: two to four syllables (of about 470,000 such names), capitalized."""
    while True:
        word = "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 4))).capitalize()
        if word.lower() not in taken:
            taken.add(word.lower())
            return word


def _ranked(heads: Sequence[Sequence[str]], n: int, seed: int) -> tuple[str, ...]:
    """``n`` names, the commonest first: those of ``heads`` taken in turn from each list (the first of each, then the
    second...), then made-up ones."""
    names = [name for group in itertools.zip_longest(*heads) for name in group if name is not None]
    taken = {name.lower() for name in names}
    rng = random.Random(seed)
    while len(names) < n:
        names.append(_made_up(rng, taken))
    return tuple(names[:n])


def _weights(n: int, flat: int) -> list[float]:
    """The cumulative shares of ``n`` ranks, the share of rank r as 1 / (r + flat)."""
    return list(itertools.accumulate(1 / (rank + flat) for rank in range(1, n + 1)))


@functools.cache
def _population() -> tuple[tuple[str, ...], list[float], tuple[str, ...], list[float], tuple[str, ...], list[float]]:
    """The given names, the family names and the companies' domains, each with its cumulative shares: made once."""
    first = _ranked((ITALIAN_FIRST_NAMES, ENGLISH_FIRST_NAMES), FIRST_NAMES, 101)
    last = _ranked((ITALIAN_LAST_NAMES, ENGLISH_LAST_NAMES), LAST_NAMES, 102)
    rng = random.Random(103)
    taken: set[str] = set()
    domains = []
    for _ in range(COMPANIES):
        name = _made_up(rng, taken).lower() + rng.choice(("", "", "", "group", "labs", "trade", "logistica"))
        domains.append(f"{name}.{rng.choice(_TLDS)}")
    return (
        first, _weights(FIRST_NAMES, _FIRST_FLAT), last, _weights(LAST_NAMES, _LAST_FLAT),
        tuple(domains), _weights(COMPANIES, _COMPANY_FLAT),
    )  # fmt: skip


_DOCUMENTATION = tuple(ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


@functools.cache
def addresses() -> tuple[str, ...]:
    """The ``ADDRESSES`` public IPv4 addresses, the same at every run: each one Latet masks (public, neither multicast
    nor reserved, outside the networks kept for documentation, and not written like a version, every part one
    digit)."""
    rng = random.Random(104)
    found: list[str] = []
    while len(found) < ADDRESSES:
        address = ipaddress.IPv4Address(rng.getrandbits(32))
        written = str(address)
        if (
            address.is_global
            and not (address.is_multicast or address.is_reserved)
            and not any(address in network for network in _DOCUMENTATION)
            and any(len(part) > 1 for part in written.split("."))
            and written not in found
        ):
            found.append(written)
    return tuple(found)


def customer(k: int, seed: int, attempt: int = 0) -> str:
    """The email address of the ``k``-th customer of a run of ``seed`` (``attempt``: drawn again, when the address
    was another customer's): a person's given and family names, at a public mailbox (two in three of them) with
    digits after their names, or at their company: the forms whose fakes, before 0.9.10, did not run out, so that the
    numbers compare with those first measured."""
    first, first_w, last, last_w, domains, domain_w = _population()
    rng = random.Random(f"customer:{seed}:{k}:{attempt}")
    given = rng.choices(first, cum_weights=first_w)[0].lower()
    family = rng.choices(last, cum_weights=last_w)[0].lower()
    if rng.random() < 2 / 3:
        mailbox = rng.choices(list(_MAILBOXES), weights=list(_MAILBOXES.values()))[0]
        year = f"{rng.randint(50, 109) % 100:02d}"
        pattern = rng.random()
        if pattern < 0.5:
            local = f"{given}.{family}{year}"
        elif pattern < 0.75:
            local = f"{given[0]}{family}{year}"
        else:
            local = f"{given}{rng.randint(1950, 2009)}"
        return f"{local}@{mailbox}"
    domain = rng.choices(domains, cum_weights=domain_w)[0]
    return f"{family}.{given}@{domain}" if rng.random() < 0.2 else f"{given}.{family}@{domain}"


@dataclass
class Customers:
    """A run's customers, in the order it names them for the first time: the same for one seed whatever share of the
    lines repeats one, each with an address no other has."""

    seed: int
    named: list[str] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)

    def next(self) -> str:
        k = len(self.named)
        for attempt in itertools.count():
            email = customer(k, self.seed, attempt)
            if email not in self.taken:
                self.taken.add(email)
                self.named.append(email)
                return email
        raise AssertionError("unreachable")


def _timestamp(rng: random.Random, n: int) -> str:
    seconds = 9 * 3600 + n * 0.37 + rng.random() * 0.3
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    return f"2026-10-24T{int(hours) % 24:02d}:{int(minutes):02d}:{rest:06.3f}Z"


def _line(rng: random.Random, n: int, email: str, ip: str | None = None) -> str:
    """A log line of about 200 characters that names ``email`` and an IPv4 address: ``ip``, else one of
    ``addresses``."""
    when = _timestamp(rng, n)
    chosen = rng.choice(addresses())
    ip = chosen if ip is None else ip
    status = rng.choices(list(_STATUSES), weights=list(_STATUSES.values()))[0]
    took, order, trace = rng.randint(3, 900), rng.randint(10_000, 999_999), f"{rng.getrandbits(64):016x}"
    service, path = rng.choice(_SERVICES), rng.choice(_PATHS)
    kind = rng.random()
    if kind < 0.45:
        return (
            f"{when} INFO  {service} request_id={trace} user={email} ip={ip} method=POST "
            f"path=/v2/{path}/{order} status={status} took={took}ms bytes={rng.randint(200, 90_000)} region=eu-south-1"
        )
    if kind < 0.65:
        return (
            f"{when} WARN  auth-service login failed for {email} from {ip}: invalid password "
            f"(attempt {rng.randint(1, 5)} of 5) session={trace[:12]} agent=web-{rng.randint(1, 40)} region=eu-west-1"
        )
    if kind < 0.85:
        return (
            f"{when} INFO  notifications receipt INV-{order} sent to {email}; client {ip} "
            f"accepted in {took} ms, queue=billing-{rng.randint(1, 8)} template=receipt-v{rng.randint(2, 9)} retries=0"
        )
    return (
        f"{when} ERROR payments card declined for {email} (ip {ip}) order={order} "
        f"amount={rng.randint(5, 2000)}.{rng.randint(0, 99):02d} EUR code=do_not_honor trace={trace} gateway=psp-2"
    )


def lines(n: int, seed: int, repeat: float = 0.5, tokens: bool = False) -> list[str]:
    """``n`` log lines, the same for the same four: each names a customer by their email address, the ``repeat``
    share of them one named before (a past line's, picked at random: a customer named often is named more often), the
    others the next new customer (``Customers``: in the same order whatever ``repeat`` is). ``tokens``: each ends with
    a session token of its own (16 hex digits, drawn apart: the lines are otherwise those without)."""
    rng = random.Random(f"lines:{seed}")
    people = Customers(seed)
    named: list[str] = []
    for _ in range(n):
        email = rng.choice(named) if named and rng.random() < repeat else people.next()
        named.append(email)
    found = [_line(rng, index, email) for index, email in enumerate(named)]
    if tokens:
        drawn = random.Random(f"tokens:{seed}")
        found = [f"{line} session_token={drawn.getrandbits(64):016x}" for line in found]
    return found


# --- documents --------------------------------------------------------------------------------------

_OPENINGS = (
    "The team reviewed the quarterly plan and agreed on the next steps",
    "During the meeting the board discussed the budget for the coming year",
    "The supplier confirmed that the shipment will leave the warehouse next week",
    "Our support desk received several questions about the new pricing",
    "The project schedule was updated after the last review",
    "Customers asked for a clearer explanation of the renewal terms",
)
_MIDDLES = (
    "because the previous estimate did not include the cost of training",
    "while the legal department checks the wording of the contract",
    "so that every region can follow the same procedure",
    "although some details still need to be confirmed by the finance office",
    "and the results will be shared with the whole department",
    "after a long discussion about priorities and available resources",
)
_ENDINGS = (
    "Further updates will follow in the next report.",
    "Everyone is invited to send comments before the end of the month.",
    "The summary below lists the open points and their owners.",
    "A final decision is expected after the next steering meeting.",
    "Please keep this document for your records.",
)


def _iban(rng: random.Random) -> str:
    """An Italian IBAN with valid check digits, in groups of four."""
    bban = rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + "".join(rng.choice("0123456789") for _ in range(22))
    digits = "".join(str(int(c, 36)) for c in bban + "IT00")
    whole = f"IT{98 - int(digits) % 97:02d}{bban}"
    return " ".join(whole[i : i + 4] for i in range(0, len(whole), 4))


def _value_sentence(rng: random.Random, kind: str, people: Customers) -> str:
    if kind == "email":
        return f"Questions about this point can be sent to {people.next()} at any time."
    if kind == "phone":
        number = f"+39 {rng.choice(_MOBILE)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}"
        return f"For the delivery the courier will call {number} in the morning."
    if kind == "iban":
        return f"The monthly fee is paid by direct debit from the account {_iban(rng)} as agreed."
    return f"The last change was made from the address {rng.choice(addresses())} during the night."


def documents(n: int, seed: int) -> list[str]:
    """``n`` documents of about ``DOCUMENT_CHARS`` characters of prose, the same for the same two, each holding ten
    values: four email addresses (of customers of their own), three phone numbers, two IBANs, an IPv4 address."""
    found = []
    for index in range(n):
        rng = random.Random(f"document:{seed}:{index}")
        people = Customers(seed * 1_000_003 + 7_919 * (index + 1))
        sentences: list[str] = []
        size = 0
        while size < DOCUMENT_CHARS:
            sentence = f"{rng.choice(_OPENINGS)}, {rng.choice(_MIDDLES)}. {rng.choice(_ENDINGS)}"
            sentences.append(sentence)
            size += len(sentence) + 1
        values = ["email"] * 4 + ["phone"] * 3 + ["iban"] * 2 + ["ip"]
        places = sorted(rng.sample(range(len(sentences)), len(values)))
        for place, kind in zip(places, values, strict=True):
            sentences[place] += " " + _value_sentence(rng, kind, people)
        paragraphs = [" ".join(sentences[i : i + 6]) for i in range(0, len(sentences), 6)]
        found.append("\n\n".join(paragraphs))
    return found


# --- the measurement ------------------------------------------------------------------------------------


@dataclass
class Measured:
    """What one operation on one kind of text gave: each call's seconds, the texts and their bytes it answered, the
    values those texts had replaced (mask) or restored (unmask), the calls refused (by status, with the first refusal's
    words, which name no value), and the texts each answered."""

    seconds: list[float] = field(default_factory=list)
    texts: int = 0
    sent_bytes: int = 0
    replaced: int = 0
    refused: Counter[int] = field(default_factory=Counter)
    refusal: str = ""
    blocked: int = 0
    answers: list[str | None] = field(default_factory=list)  # the text each text came back as; None: not answered

    def line(self, operation: str, what: str) -> str:
        total = sum(self.seconds)
        rate = self.texts / total if total else 0.0
        megabytes = self.sent_bytes / 1e6 / total if total else 0.0
        p50, p95 = _percentile(self.seconds, 50), _percentile(self.seconds, 95)
        each = self.replaced / self.texts if self.texts else 0.0
        return (
            f"{operation:<7} {what:<10} {len(self.seconds):>6} {self.texts:>7} {rate:>9.0f} {megabytes:>7.2f}"
            f" {p50 * 1000:>8.0f} {p95 * 1000:>8.0f} {each:>11.2f}"
        )


# The last column: the values each text had replaced (mask) or restored (unmask), so that a run that masks nothing
# does not look fast.
HEADER = (
    f"{'':<7} {'':<10} {'calls':>6} {'texts':>7} {'texts/s':>9} {'MB/s':>7} {'p50 ms':>8} {'p95 ms':>8}"
    f" {'values/text':>11}"
)


def _percentile(values: Sequence[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def _batches(texts: Sequence[str | None], size: int) -> Iterator[list[str | None]]:
    for start in range(0, len(texts), size):
        yield list(texts[start : start + size])


def run(client: httpx.Client, operation: str, texts: Sequence[str | None], size: int, kind: str | None) -> Measured:
    """``operation`` on ``texts``, ``size`` in a call, one call after the other; a text that is None (not answered
    before) is left out, and answers None."""
    measured = Measured()
    for batch in _batches(texts, size):
        sent = [text for text in batch if text is not None]
        if not sent:
            measured.answers += [None] * len(batch)
            continue
        body: dict[str, object] = {"texts": sent}
        if kind is not None:
            body["kind"] = kind
        started = time.perf_counter()
        answer = client.post(f"{APPS_API}/{operation}", json=body)
        took = time.perf_counter() - started
        if answer.status_code != 200:
            measured.refused[answer.status_code] += 1
            if not measured.refusal:
                measured.refusal = str(answer.json().get("error", ""))[:300]
            measured.answers += [None] * len(batch)
            continue
        measured.seconds.append(took)
        measured.texts += len(sent)
        measured.sent_bytes += sum(len(text.encode()) for text in sent)
        results = iter(answer.json()["results"])
        for text in batch:
            if text is None:
                measured.answers.append(None)
                continue
            result = next(results)
            if "blocked" in result:
                measured.blocked += 1
            measured.replaced += len(result.get("replacements", ())) + result.get("restored", 0)
            measured.answers.append(result.get("text"))
    return measured


def _token(path: Path) -> str:
    token = path.read_text("utf-8-sig").strip()  # a file written with a byte order mark (Notepad, PowerShell 5)
    if not token:
        raise SystemExit(f"{path} holds no token")
    return token


def _share(value: str) -> float:
    found = float(value)
    if not 0 <= found < 1:
        raise argparse.ArgumentTypeError("a share is from 0 up to (not including) 1")
    return found


def _at_least(least: int, most: int | None = None):
    def check(value: str) -> int:
        found = int(value)
        if found < least or (most is not None and found > most):
            raise argparse.ArgumentTypeError(f"from {least}" + (f" to {most}" if most is not None else " up"))
        return found

    return check


def parser() -> argparse.ArgumentParser:
    """The command's options. No abbreviation is taken: ``--token`` is no shorter way to write ``--token-file``, and
    no option takes the token itself."""
    found = argparse.ArgumentParser(
        description="Measure a Latet server's applications API on log lines and documents.", allow_abbrev=False
    )
    found.add_argument("--url", required=True, help="the server's address, e.g. https://latet.example.com")
    found.add_argument("--token-file", required=True, type=Path, help="a file that holds an API key's token")
    found.add_argument("--lines", type=_at_least(0), default=20_000, help="log lines to mask and unmask")
    found.add_argument("--batch", type=_at_least(1, MOST_TEXTS), default=500, help="log lines in a call")
    found.add_argument("--documents", type=_at_least(0), default=50, help="documents of about 20 KB, one in a call")
    found.add_argument("--repeat", type=_share, default=0.5, help="the share of lines that name a customer seen before")
    found.add_argument("--seed", type=int, default=1, help="another seed gives other texts")
    found.add_argument(
        "--session-tokens", action="store_true", help="a session token of its own in each line (short secrets held)"
    )
    found.add_argument(
        "--unmask", action=argparse.BooleanOptionalAction, default=True, help="unmask what was masked, or not"
    )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    headers = {"authorization": f"Bearer {_token(args.token_file)}"}
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=600) as client:
        warm = client.post(f"{APPS_API}/mask", json={"texts": ["warm up"], "kind": "data"})  # the space built
        if warm.status_code != 200:
            print(f"the server refused the first call ({warm.status_code}): {warm.text[:300]}")
            return 1
        print(HEADER)
        measured: list[tuple[str, str, Measured, Sequence[str]]] = []
        for what, texts, size, kind in (
            ("log lines", lines(args.lines, args.seed, args.repeat, args.session_tokens), args.batch, "data"),
            ("documents", documents(args.documents, args.seed), 1, None),
        ):
            if not texts:
                continue
            masked = run(client, "mask", texts, size, kind)
            print(masked.line("mask", what), flush=True)
            measured.append(("mask", what, masked, texts))
            if args.unmask:
                restored = run(client, "unmask", masked.answers, size, None)
                print(restored.line("unmask", what), flush=True)
                measured.append(("unmask", what, restored, texts))
    for operation, what, found, texts in measured:
        if found.refused:
            said = ", ".join(f"{count} answered {status}" for status, count in sorted(found.refused.items()))
            print(f"{operation} {what}: {said} ({found.refusal})")
        if found.blocked:
            print(f"{operation} {what}: {found.blocked} texts blocked")
        if operation == "unmask":
            back = sum(answer == text for answer, text in zip(found.answers, texts, strict=True))
            print(f"{what}: {back} of {len(texts)} came back as they were sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
