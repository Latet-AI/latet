"""Optional local detection of names and street addresses in free text (``latet[ner]``).

OpenAI's Privacy Filter (Apache-2.0), a multilingual token classifier, runs with ONNX Runtime on this
machine: nothing leaves it. Only people and addresses are taken from it; structured data (emails,
IBANs, fiscal codes, phones, keys) is found better by the validated detectors. How the model was
chosen: ``notes/ner-eval.md``.

The model is downloaded on request (``latet ner install``), never implicitly.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from latet.engine import modelspec
from latet.engine.detectors.base import DetectContext, DetectorSkipped
from latet.engine.modelspec import ModelSpec
from latet.engine.surrogates.data.names import APOSTROPHES, NAME_EDGES, STREET_PARTICLES, STREET_TYPES
from latet.engine.types import EntityClass, SlotKind, Span
from latet.logline import described

log = logging.getLogger(__name__)


MODEL = ModelSpec(
    name="privacy-filter",
    repo="openai/privacy-filter",
    revision="7ffa9a043d54d1be65afb281eddf0ffbe629385b",
    files={
        "onnx/model_quantized.onnx": ("a325fb5341567a73c94e91ec5e49060d38d9b16111f518ad34773039a0c9c098", 162_239),
        "onnx/model_quantized.onnx_data": (
            "50f4c8c7f3c27fbc1fe16d4f74f6f7c3b74ba8f18a262e8b6911854c64c33a6d",
            1_618_042_064,
        ),
        "tokenizer.json": ("0614fe83cadab421296e664e1f48f4261fa8fef6e03e63bb75c20f38e37d07d3", 27_868_174),
        "config.json": ("b2b26a4a4a000639ad30b0c264adbefe365bdb567fbd7bb27303b8c438375bd1", 3_039),
    },
    license="Apache-2.0",
    homepage="https://huggingface.co/openai/privacy-filter",
)


def model_dir(data: Path, spec: ModelSpec = MODEL) -> Path:
    return modelspec.model_dir(data, spec)


def is_installed(data: Path, spec: ModelSpec = MODEL) -> bool:
    return modelspec.is_installed(data, spec)


def runtime_available() -> bool:
    """The ``ner`` extra is installed (ONNX Runtime, tokenizers, NumPy)."""
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "tokenizers", "numpy"))


# --- decoding -------------------------------------------------------------------------------------------


def _allowed(labels: list[str]) -> list[list[bool]]:
    """allowed[a][b]: label b may follow label a. Outside, E and S end a span; B and I continue one."""

    def split(label: str) -> tuple[str, str]:
        return ("O", "") if label == "O" else (label[0], label[2:])

    out = []
    for a in labels:
        pa, ta = split(a)
        row = []
        for b in labels:
            pb, tb = split(b)
            row.append(pb in ("O", "B", "S") if pa in ("O", "E", "S") else (pb in ("I", "E") and ta == tb))
        out.append(row)
    return out


def decode_bioes(logits: Any, labels: list[str]) -> list[int]:
    """The best label path under BIOES rules (constrained Viterbi over log-probabilities).

    Privacy Filter is decoded this way by its authors: an independent argmax per token can start a span
    in the middle or close it with another type.
    """
    import numpy as np

    n = len(labels)
    logp = logits - logits.max(axis=-1, keepdims=True)
    logp = logp - np.log(np.exp(logp).sum(axis=-1, keepdims=True))
    trans = np.where(np.array(_allowed(labels)), 0.0, -np.inf)
    start = np.array([0.0 if lab == "O" or lab[0] in "BS" else -np.inf for lab in labels])
    end = np.array([0.0 if lab == "O" or lab[0] in "ES" else -np.inf for lab in labels])
    score = start + logp[0]
    back = np.zeros((len(logp), n), dtype=np.int64)
    for t in range(1, len(logp)):
        cand = score[:, None] + trans
        back[t] = cand.argmax(axis=0)
        score = cand.max(axis=0) + logp[t]
    path = [int((score + end).argmax())]
    for t in range(len(logp) - 1, 0, -1):
        path.append(int(back[t, path[-1]]))
    return path[::-1]


def windows(n: int, size: int, stride: int) -> list[int]:
    """Start offsets of windows of ``size`` tokens, ``stride`` apart, the last one ending at ``n``."""
    starts = list(range(0, max(n - size, 0) + 1, stride))
    if starts[-1] + size < n:
        starts.append(n - size)
    return starts


# --- the model ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Token:
    start: int
    end: int
    label: str  # "O", or BIOES: "B-private_person", "E-private_address"...
    score: float


class TokenModel(Protocol):
    def predict(self, text: str, max_tokens: int | None = None) -> list[Token]: ...


class PastDeadline(Exception):
    """The time given to read a set of texts ran out before the model had read them all."""


class OnnxTokenModel:
    """Privacy Filter (int8 ONNX) over windows of 1,024 tokens that overlap by 256.

    This export computes attention over the whole window and masks it down to 128 tokens each side, so
    its cost and memory grow with the square of the window: 1,024 tokens was the fastest and lightest
    size measured (``notes/ner-eval.md``). A token near a seam keeps at least 128 tokens of context on
    each side. Loading takes a few seconds and happens on a thread of its own, started with the daemon;
    thread-safe afterwards.
    """

    WINDOW = 1024
    STRIDE = 768
    # Short texts read at once, as rows of one run (``predict_many``), each padded to the longest and masked from the
    # others: what one row gives is what the text gives read alone. At most this many tokens in a run, padding counted:
    # a second or so on a desktop CPU, at about twice the rate of one text a run of a window.
    BATCH = 2048

    def __init__(self, directory: Path, threads: int = 0) -> None:
        self.directory = directory
        self.threads = threads or min(4, os.cpu_count() or 1)
        self._lock = threading.Lock()  # held while loading
        self._starting = threading.Lock()  # never held long: callers must not wait for a load
        self._session: Any = None
        self._loader: threading.Thread | None = None
        self.error: BaseException | None = None

    @property
    def ready(self) -> bool:
        return self._session is not None

    def start_loading(self) -> None:
        """Load in the background (once); requests meanwhile go on without names, flagged."""
        if self._session is not None or self.error is not None:
            return
        with self._starting:
            if self._loader is None:
                self._loader = threading.Thread(target=self._load_guarded, name="latet-ner-load", daemon=True)
                self._loader.start()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        """Load and wait (tests, diagnostics); requests never call this."""
        self.start_loading()
        loader = self._loader
        if loader is not None:
            loader.join(timeout)
        return self.ready

    def _load_guarded(self) -> None:
        started = time.monotonic()
        try:
            with self._lock:
                if self._session is None:
                    self._load()
            self.predict("Warm up.")  # the first run allocates the runtime's buffers
            log.info("local name model ready in %.1f s", time.monotonic() - started)
        except Exception as exc:
            self.error = exc
            log.error("the local name model could not be loaded: %s", described(exc))

    def _load(self) -> None:
        import numpy
        import onnxruntime
        import tokenizers

        config = json.loads((self.directory / "config.json").read_text("utf-8"))
        id2label = config["id2label"]
        labels = [id2label[str(i)] for i in range(len(id2label))]
        if config.get("model_type") != "openai_privacy_filter" or "B-private_person" not in labels:
            raise RuntimeError("config.json is not Privacy Filter's: not the expected model")
        self._labels = labels
        tokenizer = tokenizers.Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        self._tokenizer = tokenizer
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.log_severity_level = 3
        self._np = numpy
        self._session = onnxruntime.InferenceSession(
            str(self.directory / "onnx" / "model_quantized.onnx"), options, providers=["CPUExecutionProvider"]
        )

    def _loaded(self) -> None:
        with self._lock:
            if self._session is None:
                self._load()

    def predict(self, text: str, max_tokens: int | None = None) -> list[Token]:
        self._loaded()
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        ids, offsets = encoding.ids, encoding.offsets
        n = len(ids)
        if n == 0:
            return []
        if max_tokens is not None and n > max_tokens:
            raise DetectorSkipped(f"{n} tokens: over the {max_tokens} the local model reads")
        labels, scores = self._label(ids)
        return [Token(offsets[i][0], offsets[i][1], labels[i], scores[i]) for i in range(n)]

    def predict_many(
        self, texts: Sequence[str], max_tokens: int, deadline: float | None = None
    ) -> list[list[Token] | None]:
        """Each text's tokens, as ``predict`` gives them, the texts read in as few runs of the model as hold them: the
        texts of a window or less as rows of one run (``BATCH``), those of a like length together, each masked from the
        others, so that each is read exactly as alone; a longer one alone, in windows, as ``predict`` reads it. One over
        ``max_tokens`` is not read: None in its place. ``deadline`` (``time.monotonic``): raises ``PastDeadline`` when
        it passes before the model's next run, what was read then given to nobody."""
        self._loaded()
        encoded = [self._tokenizer.encode(text, add_special_tokens=False) for text in texts]
        out: list[list[Token] | None] = [[] for _ in texts]
        short: list[int] = []  # texts of a window or less, read as rows
        for i, encoding in enumerate(encoded):
            n = len(encoding.ids)
            if n == 0:
                continue
            if n > max_tokens:
                out[i] = None
            elif n > self.WINDOW:  # read alone, in windows that overlap
                labels, scores = self._label(encoding.ids, deadline)
                out[i] = [Token(s, e, labels[k], scores[k]) for k, (s, e) in enumerate(encoding.offsets)]
            else:
                short.append(i)
        short.sort(key=lambda i: len(encoded[i].ids))
        rows: list[int] = []
        for i in short:  # the longest last: each run padded to its own longest
            if rows and (len(rows) + 1) * len(encoded[i].ids) > self.BATCH:
                self._rows(rows, encoded, out, deadline)
                rows = []
            rows.append(i)
        if rows:
            self._rows(rows, encoded, out, deadline)
        return out

    def _rows(self, rows: list[int], encoded: list[Any], out: list[list[Token] | None], deadline: float | None) -> None:
        """The texts ``rows`` (each a window or less) read in one run, as rows padded to the longest, each row's
        padding and the other rows masked from it: what a row gives is what its text gives read alone."""
        if deadline is not None and time.monotonic() > deadline:
            raise PastDeadline()
        np = self._np
        ids = np.zeros((len(rows), max(len(encoded[i].ids) for i in rows)), dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, i in enumerate(rows):
            n = len(encoded[i].ids)
            ids[row, :n], mask[row, :n] = encoded[i].ids, 1
        logits = self._session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        for row, i in enumerate(rows):
            n = len(encoded[i].ids)
            labels, scores = ["O"] * n, [1.0] * n
            for start, end, found, given in self._spans(logits[row, :n]):
                labels[start:end], scores[start:end] = found, given
            out[i] = [Token(s, e, labels[k], scores[k]) for k, (s, e) in enumerate(encoded[i].offsets)]

    def _spans(self, logits: Any) -> list[tuple[int, int, list[str], list[float]]]:
        """The spans one window's ``logits`` decode to (``decode_bioes``): where each starts and ends among its tokens,
        its tokens' labels and their scores."""
        np = self._np
        path = decode_bioes(logits, self._labels)
        exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        length = len(path)
        out = []
        i = 0
        while i < length:
            label = self._labels[path[i]]
            if label == "O":
                i += 1
                continue
            j = i + 1
            while j < length and label[0] in "BI" and self._labels[path[j]][0] in "IE":
                label = self._labels[path[j]]
                j += 1
            labels = [self._labels[path[k]] for k in range(i, j)]
            scores = [float(probs[k, path[k]]) for k in range(i, j)]
            out.append((i, j, labels, scores))
            i = j
        return out

    def _label(self, ids: list[int], deadline: float | None = None) -> tuple[list[str], list[float]]:
        """The label of each token of ``ids``, and its score, read in windows (``WINDOW``, ``STRIDE``). ``deadline``:
        raises ``PastDeadline`` when it has passed before a window."""
        session: Any = self._session
        np = self._np
        n = len(ids)
        found: list[tuple[int, int, int, list[str], list[float]]] = []  # centrality, start, end, labels, scores
        for start in windows(n, self.WINDOW, self.STRIDE):
            if deadline is not None and time.monotonic() > deadline:
                raise PastDeadline()
            window = np.array([ids[start : start + self.WINDOW]], dtype=np.int64)
            logits = session.run(None, {"input_ids": window, "attention_mask": np.ones_like(window)})[0][0]
            length = len(logits)
            for i, j, labels, scores in self._spans(logits):
                found.append((min(i, length - j), start + i, start + j, labels, scores))
        # Each window decodes the whole span it sees. Where two windows overlap, a span either one found is
        # kept (the more central reading first): a name one window sees whole is never cut at the seam.
        out_label = ["O"] * n
        out_score = [1.0] * n
        for _, lo, hi, labels, scores in sorted(found, key=lambda f: -f[0]):
            if all(out_label[k] == "O" for k in range(lo, hi)):
                out_label[lo:hi], out_score[lo:hi] = labels, scores
        return out_label, out_score


# --- from tokens to values -----------------------------------------------------------------------------

_APOSTROPHES = APOSTROPHES
_EDGES = NAME_EDGES
# Words of product and agent names ("Claude Code", "Hermes Agent", "Gemini CLI"): a name holding one
# is not a person. Found live in Claude Code's own context (skills, reminders, CLAUDE.md).
_PRODUCT_WORDS = frozenset(
    """claude sonnet opus haiku fable anthropic openai chatgpt gpt codex gemini copilot cursor ollama llama
    mistral qwen deepseek grok hermes agent agents code cli sdk api desktop studio server client cloud bot
    assistant model plugin skill mcp latet""".split()  # noqa: SIM905
)
# Capitalised words the model takes for names in technical prose; ignored as single-word names.
_TECH_WORDS = frozenset(
    w.lower()
    for w in """Redis Postgres Postgresql Mysql Mariadb Sqlite Mongo Mongodb Kafka Docker Kubernetes Jenkins Django
    Flask Fastapi Rails Laravel Spring Express React Vue Angular Svelte Next Nuxt Node Deno Bun Rust Go
    Swift Kotlin Java Python Ruby Perl Elixir Erlang Haskell Scala Julia Dart Lua Hadoop Spark Hive
    Cassandra Elastic Kibana Grafana Prometheus Nginx Apache Tomcat Ubuntu Debian Fedora Alpine Linux
    Windows Azure Vercel Netlify Heroku Terraform Ansible Vault Consul Nomad Git Github Gitlab Jira
    Confluence Slack Notion Figma Stripe Twilio Celery Pandas Numpy Torch Keras Vite Webpack Babel Jest
    Vitest Pytest Claude Codex Gemini Copilot Cursor Ollama""".split()  # noqa: SIM905
)


def _word_char(c: str) -> bool:
    return c.isalnum() or c == "-" or c in _APOSTROPHES


def _street_type_only(value: str) -> bool:
    """ "Corso", "Piazza della": a street type with no name, which names no place."""
    words = (w.lower().strip(".,") for w in value.split())
    return all(not w or w in STREET_TYPES or w in STREET_PARTICLES for w in words)


_PERSON, _ADDRESS = "private_person", "private_address"

# Words the model takes in with a name ("Ciao Paolo", "Herrn Thomas Müller", "Oggi Rosa"): never part of it.
# A greeting of several words ("Sehr geehrte", "Buenos días") is dropped a word at a time. Not listed, as names
# start with them too: "don" (Don, a first name), "dias" (Dias, a surname), "m" (the initial in "M. Rossi").
_NOT_NAME_LEAD = frozenset(
    """ciao salve buongiorno buonasera grazie oggi ieri domani hello hi hey dear thanks caro cara cari care gentile
    egregio egregia herr herrn frau mr mrs ms miss dr dott dottor dottore dottoressa prof professor professoressa
    ing avv sig sigra signor signora signore monsieur madame mme mlle señor señora sr sra
    hallo moin servus guten tag morgen abend danke liebe lieber sehr geehrte geehrter hr fr
    bonjour bonsoir salut coucou merci cher chère chere chers chères cheres
    mademoiselle maître maitre docteur professeur
    hola buenos buenas días tardes noches gracias
    querido querida queridos queridas estimado estimada estimados estimadas
    senor senora señorita senorita srta dra doña doctor doctora profesor profesora lic""".split()  # noqa: SIM905
)
_POSTCODE = re.compile(r"(?:^|(?<=[\s,]))(\d{4,5})(?=\s+[^\W\d_])")
# The place after "a"/"in" at the end ("... 5 a Firenze"): no digits, so "via in Selci 84 a Roma" is not
# cut at its first "in".
_IN_CITY = re.compile(r"\s+(?:a|in|à|en|ad)\s+([^\W\d_](?:[^\W\d_]|[" + _APOSTROPHES + r" -])*?)$")
_PROVINCE = re.compile(r"\s+\(?[A-Z]{2}\)?$")


def _entities(text: str, tokens: Iterable[Token], min_score: float) -> list[list[Any]]:
    """BIOES tokens → [start, end, type, score] spans of people and addresses, widened to whole words."""
    out: list[list[Any]] = []
    current: list[Any] | None = None
    for t in tokens:
        kind = t.label[2:] if t.label != "O" else ""
        if kind not in (_PERSON, _ADDRESS) or t.score < min_score or t.end <= t.start:
            current = None
            continue
        tag = t.label[0]
        if current is not None and current[2] == kind and tag in ("I", "E"):
            current[1] = t.end
            current[3] = min(current[3], t.score)
        else:
            current = [t.start, t.end, kind, t.score]
            out.append(current)
        if tag in ("E", "S"):
            current = None
    for span in out:
        # The tokenizer keeps the space before a word, and joins some signs to it: "=M", "(M", ".M".
        while span[0] < span[1] and not text[span[0]].isalnum():
            span[0] += 1
        while span[0] > 0 and _word_char(text[span[0] - 1]):
            span[0] -= 1
        while span[1] < len(text) and _word_char(text[span[1]]):
            span[1] += 1
        while span[1] > span[0] and (text[span[1] - 1] in " \t.,;:" or text[span[1] - 1] in _EDGES):
            span[1] -= 1
        # No name begins or ends with a quote or an apostrophe: console.table's 'Ann Ross', “Ann Ross”, «Ann Ross»
        while span[0] < span[1] and text[span[0]] in _EDGES:
            span[0] += 1
    return [s for s in out if s[1] > s[0]]


def _trim_person(text: str, start: int, end: int) -> tuple[int, int]:
    """Greetings, titles and time words in front of a name are dropped: "Ciao Paolo" → "Paolo"."""
    while True:
        m = re.match(r"(\S+)\s+", text[start:end])
        if m is None or m.group(1).lower().rstrip(".") not in _NOT_NAME_LEAD:
            return start, end
        start += m.end()


def _starts_with_street_type(value: str) -> bool:
    first = value.split(maxsplit=1)[0].lower().strip(".,") if value.strip() else ""
    return first in STREET_TYPES


def _address_start(text: str, start: int, end: int) -> int:
    """The address begins at its street type: "abita in viale Monza" → "viale Monza"; "Garibaldi 12" after
    "via " takes it in."""
    words = list(re.finditer(r"\S+", text[start:end]))
    for i, w in enumerate(words):
        if w.group().lower().strip(".,") in STREET_TYPES:
            if i and all(x.group().islower() for x in words[:i]):
                return start + w.start()
            break
    before = re.search(r"(\S+) $", text[max(0, start - 20) : start])
    if before is not None and before.group(1).lower().strip(".,") in STREET_TYPES:
        return start - len(before.group(1)) - 1
    return start


def _split_address(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """Street, postcode and city out of one address: the fakes for each keep their own shape."""
    value = text[start:end]
    post = _POSTCODE.search(value, 1)
    if post is None and (lead := _POSTCODE.match(value)) is not None and _place_only(value[lead.end(1) :]):
        post = lead  # "20121 Milano"; "1600 Amphitheatre Parkway, ..." starts with a house number
    if post is not None:
        parts = []
        street_end = start + post.start(1)
        while street_end > start and text[street_end - 1] in " ,":
            street_end -= 1
        if street_end > start:
            parts.append((start, street_end, "STREET_ADDRESS"))
        parts.append((start + post.start(1), start + post.end(1), "ZIPCODE"))
        city_start = start + post.end(1)
        while city_start < end and text[city_start].isspace():
            city_start += 1
        if city_start < end:
            parts.append((city_start, end, "CITY"))
        return parts
    m = _IN_CITY.search(value)
    if m is not None:
        return [(start, start + m.start(), "STREET_ADDRESS"), (start + m.start(1), start + m.end(1), "CITY")]
    if _place_only(value):
        return [(start, end, "CITY")]  # "Milano": a place with no street in it
    return [(start, end, "STREET_ADDRESS")]


def _place_only(value: str) -> bool:
    """A place name and nothing else: no digit, no comma, no street word ("Milano", "Mountain View")."""
    words = [w.lower().strip(".,") for w in value.split()]
    return bool(words) and not any(c.isdigit() or c == "," for c in value) and not any(w in STREET_TYPES for w in words)


def _people_and_addresses(text: str, spans: list[list[Any]]) -> list[tuple[int, int, str, int]]:
    """[start, end, type, score] spans → (start, end, PERSON | STREET_ADDRESS | ZIPCODE | CITY, group)."""

    def number_between(a: int, b: int) -> bool:  # "II| 154, |00186": only a house number between two parts
        gap = text[a:b]
        return len(gap) <= 12 and re.fullmatch(r"[\s,\d/]*", gap) is not None

    # A "person" that starts with a street type ("Corso Vittorio Emanuele II") is the street of an address,
    # when the address shows: a number in it, or the rest of the address right after it. A title ("Dr.") or
    # a name that is also a street word ("Lane Johnson") with nothing of an address stays a person.
    items: list[list[Any]] = []
    for i, (s, e, k, _) in enumerate(spans):
        value = text[s:e]
        if (
            k == _PERSON
            and _starts_with_street_type(value)
            and value.split()[0].lower().rstrip(".") not in _NOT_NAME_LEAD
        ):
            following = spans[i + 1] if i + 1 < len(spans) else None
            if any(c.isdigit() for c in value) or (
                following is not None and following[2] == _ADDRESS and number_between(e, following[0])
            ):
                k = _ADDRESS
        items.append([s, e, k])
    # An address part and the next one, with only a house number between, are one address.
    joined: list[list[Any]] = []
    for item in items:
        prev = joined[-1] if joined else None
        if prev is not None and prev[2] == item[2] == _ADDRESS and number_between(prev[1], item[0]):
            prev[1] = item[1]
        else:
            joined.append(item)
    out: list[tuple[int, int, str, int]] = []
    for group, (start, end, kind) in enumerate(joined):
        if kind == _PERSON:
            s, e = _trim_person(text, start, end)
            out.append((s, e, "PERSON", group))
            continue
        start = _address_start(text, start, end)
        for s, e, etype in _split_address(text, start, end):
            if etype == "CITY" and (p := _PROVINCE.search(text[s:e])) is not None and p.start() == 0:
                continue
            out.append((s, e, etype, group))
    return out


def _plausible_name(value: str, single_names: bool) -> bool:
    words = value.split()
    if not words or (len(words) < 2 and not single_names):
        return False
    for w in words:
        core = w.strip(_APOSTROPHES + "-.")
        if not core or not core[0].isupper() or any(c.isdigit() or c in "_/@:" for c in core):
            return False
    plain = [w.lower().strip(_APOSTROPHES + ".").removesuffix("'s").removesuffix(chr(0x2019) + "s") for w in words]
    if any(w in _PRODUCT_WORDS for w in plain):
        return False
    return not all(w in _TECH_WORDS for w in plain)


def _plausible_street(text: str, start: int, end: int) -> bool:
    """A street needs a house number, or a word such as "via" or "Street" in it or just before it.

    A street type alone ("Corso", "Piazza della") is not one: it names no place.
    """
    value = text[start:end]
    if len(value) < 4 or not any(c.isalpha() for c in value) or _street_type_only(value):
        return False
    if any(c.isdigit() for c in value):
        return True
    before = text[max(0, start - 20) : start].split()
    words = [w.lower().strip(".,") for w in value.split()] + ([before[-1].lower().strip(".,")] if before else [])
    return any(w in STREET_TYPES for w in words)


class NerDetector:
    """Names and street addresses in natural-language text, with a local multilingual model."""

    id = "ner"
    mandatory = False  # if the model fails, the request goes on (flagged): the other detectors still ran

    def __init__(
        self,
        model: TokenModel,
        kinds: Iterable[SlotKind] = (SlotKind.USER_TEXT, SlotKind.ATTACHMENT, SlotKind.IMAGE_TEXT),
        single_names: bool = False,
        cities: bool = False,
        min_score: float = 0.6,
        max_chars: int = 16_000,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.kinds = frozenset(kinds)
        self.single_names = single_names  # one-word names ("Giulia") count, not only full names
        self.cities = cities  # cities and postcodes with no street next to them
        self.min_score = min_score
        self.max_chars = max_chars
        self.max_tokens = max_tokens  # about 5 s on a desktop CPU; more is skipped, flagged, never waited for
        # Each thread's texts read ahead (``read_ahead``), while it masks a document (``reading``): a text's tokens, or
        # None for one over ``max_tokens``.
        self._ahead = threading.local()

    @contextlib.contextmanager
    def reading(self) -> Iterator[None]:
        """While it is open, what ``read_ahead`` reads in this thread is kept, and ``detect`` takes each text's reading
        from there; forgotten when it closes."""
        before = getattr(self._ahead, "tokens", None)
        self._ahead.tokens = {}
        try:
            yield
        finally:
            self._ahead.tokens = before

    def _reads(self, text: str, kind: SlotKind) -> bool:
        return kind in self.kinds and len(text) <= self.max_chars and any(c.isalpha() for c in text)

    def read_ahead(self, texts: Iterable[str], kind: SlotKind, deadline: float) -> bool:
        """``texts`` of ``kind``, read before they are masked one by one (a document's strings and keys), in as few runs
        of the model as hold them, each up to ``max_tokens`` (``predict_many``): run once for each text, the model costs
        a document of a thousand short values minutes. Kept while ``reading`` is open, for ``detect``. False when
        ``deadline`` (``time.monotonic``) passes before all are read: the caller does not send what it would have masked
        without them. True when the model is not ready, or failed: ``detect`` then says so for each text, as it always
        did."""
        ahead = getattr(self._ahead, "tokens", None)
        if ahead is None or not getattr(self.model, "ready", True):
            return True
        todo = list(dict.fromkeys(t for t in texts if t not in ahead and self._reads(t, kind)))
        try:
            many = getattr(self.model, "predict_many", None)
            if many is not None:
                found = many(todo, self.max_tokens, deadline) if todo else []
                ahead.update(zip(todo, found, strict=True))
                return True
            for text in todo:  # a model that reads one text at a time: the time is checked between two
                if time.monotonic() > deadline:
                    return False
                try:
                    ahead[text] = self.model.predict(text, max_tokens=self.max_tokens)
                except DetectorSkipped:
                    ahead[text] = None
        except PastDeadline:
            return False
        except Exception as exc:  # by its type: ``detect`` reads each text alone, and says what it met
            log.warning("the local name model could not read a document's texts at once: %s", type(exc).__name__)
        return True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if ctx.kind not in self.kinds or not any(c.isalpha() for c in text):
            return
        if len(text) > self.max_chars:
            raise DetectorSkipped(f"{len(text)} characters: over the {self.max_chars} the local model reads")
        if not getattr(self.model, "ready", True):
            # Never make a request (or a Claude Code hook, 10 s budget) wait for the model to load.
            error = getattr(self.model, "error", None)
            if error is not None:  # by its type: its words name the model's file, a library's own words
                raise DetectorSkipped(f"the local model could not be loaded: {described(error)}")
            self.model.start_loading()  # type: ignore[attr-defined]
            raise DetectorSkipped("the local model is still loading")
        ahead = getattr(self._ahead, "tokens", None)
        if ahead is not None and text in ahead:
            tokens = ahead[text]
            if tokens is None:
                raise DetectorSkipped(f"over the {self.max_tokens} tokens the local model reads")
        else:
            tokens = self.model.predict(text, max_tokens=self.max_tokens)
        found = _people_and_addresses(text, _entities(text, tokens, self.min_score))
        streets = {g for s, e, t, g in found if t == "STREET_ADDRESS" and _plausible_street(text, s, e)}
        for start, end, etype, group in found:
            value = text[start:end]
            if etype == "PERSON" and not _plausible_name(value, self.single_names):
                continue
            if etype == "STREET_ADDRESS" and group not in streets:
                continue
            if etype in ("CITY", "ZIPCODE") and (len(value) < 2 or not (group in streets or self.cities)):
                continue
            yield Span(start, end, etype, EntityClass.NER, f"ner:{etype.lower()}")


def rejected_now(etype: str, original: str) -> bool:
    """A name the model found earlier that today's filters reject (a product name, say).

    Found once, a value is masked everywhere as a known value: a false positive would stay forever.
    """
    return etype == "PERSON" and not _plausible_name(original, single_names=True)


_SHARED: dict[Path, OnnxTokenModel] = {}
_SHARED_LOCK = threading.Lock()


def shared_model(directory: Path) -> OnnxTokenModel:
    """One model per folder for every masker, the global one and each project's: up to about 2 GB."""
    with _SHARED_LOCK:
        model = _SHARED.get(directory)
        if model is None or model.error is not None:  # a load that failed is tried again with new rules
            model = _SHARED[directory] = OnnxTokenModel(directory)
        return model


def detector_for(data: Path, profile: str, model: TokenModel | None = None) -> NerDetector | None:
    """The NER detector a profile uses, when the model and its runtime are installed."""
    if model is None:
        if not (runtime_available() and is_installed(data)):
            return None
        model = shared_model(model_dir(data))
    if profile == "max":
        everywhere = (
            SlotKind.USER_TEXT,
            SlotKind.ATTACHMENT,
            SlotKind.SYSTEM,
            SlotKind.TOOL_RESULT,
            SlotKind.IMAGE_TEXT,
            SlotKind.OTHER,
        )
        return NerDetector(model, everywhere, single_names=True, cities=True, max_chars=64_000, max_tokens=8192)
    return NerDetector(model)
