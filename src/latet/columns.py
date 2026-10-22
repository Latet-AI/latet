"""Strict data mode, guided: the columns of a tool's output, and for each a proposal (keep it in clear,
keep it masked, or your call) with its reason. Computed when you open the output in the dashboard, from
the original the exchange keeps: nothing new is stored, and nothing applies by itself."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from latet.config import FieldRule
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.fields import FieldDetector, cells
from latet.engine.detectors.structural import _key_kind as secret_kind
from latet.engine.masker import Masker
from latet.engine.types import EntityClass, SlotKind
from latet.protocols.base import map_json_values

Proposal = Literal["keep", "mask", "ask"]

# What agents wrap a tool's output in (Codex's `output`, an MCP result's `content[].text`): not columns,
# but texts that may hold a table, or JSON again.
_WRAPPERS = ("output", "stdout", "stderr", "content[].text")
_NOT_COLUMNS = (*_WRAPPERS, "content[].type", "isError")
_NOTHING = re.compile(r"(?i)^(?:null|none|nil|n/a|-+|\(null\))$")
_FLAGS = {"yes", "no", "si", "sì", "y", "n", "true", "false", "t", "f", "on", "off", "0", "1", "vero", "falso"}
_TOKEN = re.compile(r"^[A-Za-z0-9][\w.:\-]{0,63}$")  # one word: an id, a code, a state
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_INTEGER = re.compile(r"^-?\d+$")
_AMOUNT = re.compile(r"^[€$£]?\s?-?\d{1,3}(?:[.,' ]?\d{3})*(?:[.,]\d{1,4})\s?(?:€|EUR|USD|\$|£)?$")
_DATES = (
    re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"),
    re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}(?: \d{1,2}:\d{2}(?::\d{2})?)?$"),
    re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$"),
)
# Names that say a column is about people, their contacts or their words.
_PERSON = {
    *("name", "nome", "cognome", "surname", "firstname", "lastname", "fullname", "first", "last", "user"),
    *("username", "utente", "cliente", "customer", "client", "referente", "contact", "contatto", "owner"),
    *("email", "mail", "pec", "phone", "telefono", "tel", "cellulare", "mobile", "fax", "address"),
    *("indirizzo", "via", "street", "city", "citta", "città", "zip", "cap", "patient", "paziente"),
    *("employee", "dipendente", "destinatario", "mittente", "recipient", "sender", "author", "autore"),
    *("assigned", "assignee", "reporter", "creator", "editor", "approver", "requester", "richiedente", "login"),
    *("medico", "dottore", "doctor", "physician", "nominativo", "persona", "person", "people", "manager"),
    *("responsabile", "operatore", "operator", "intestatario", "titolare", "beneficiario", "beneficiary"),
    *("studente", "student", "candidato", "candidate", "member", "socio", "tecnico", "venditore", "seller"),
    *("buyer", "acquirente", "firmatario", "signer", "nickname", "sesso", "gender", "sex", "genere"),
}
# Words that say a column is about someone's health, beliefs or private life: never in clear, whatever
# else its name says (`hiv_status` holds states, about a person).
_SENSITIVE = {
    *("health", "salute", "hiv", "aids", "malattia", "malattie", "disease", "illness", "pregnant", "pregnancy"),
    *("gravidanza", "incinta", "disability", "disabilita", "invalidita", "handicap", "medical", "clinical"),
    *("clinico", "clinica", "therapy", "terapia", "allergy", "allergia", "blood", "sangue", "vaccine", "vaccino"),
    *("marital", "married", "civile", "coniuge", "spouse", "religion", "religione", "faith", "political"),
    *("politics", "politico", "partito", "union", "sindacato", "ethnicity", "ethnic", "etnia", "race", "razza"),
    *("sexual", "sessuale", "orientation", "orientamento", "criminal", "penale", "conviction", "condanna"),
    *("biometric", "biometrico", "genetic", "genetico"),
}
_NOTES = {
    *("note", "nota", "notes", "description", "descrizione", "comment", "commento", "comments", "message"),
    *("messaggio", "text", "testo", "body", "diagnosis", "diagnosi", "remarks", "reason", "motivo"),
}
# Dates about a person (of birth, of death), not of events.
_BIRTH = {"birth", "birthday", "birthdate", "dob", "born", "nascita", "nato", "nata", "compleanno", "nacimiento"}
_BIRTH |= {"naissance", "geburtsdatum", "geburtstag", "death", "died", "deceased", "morte", "decesso"}
# What a column's name says its values are (the dashboard's technicalKind, in lib/fields.ts).
_ID_WORDS = {"id", "uuid", "guid", "pk", "seq", "version", "rev"}
_DATE_WORDS = {
    *("date", "time", "timestamp", "ts", "day", "month", "year", "created", "updated", "modified", "deleted"),
    *("creato", "creata", "aggiornato", "modificato", "scadenza"),
}
_DATE_LAST = {"at", "on", "il"}
_STATE_WORDS = {"status", "state", "stato", "type", "kind", "category", "level", "priority", "enabled", "active"}
_STATE_WORDS |= {"flag", "attivo", "attiva", "abilitato", "abilitata", "tipo", "categoria", "livello", "priorita"}
_COUNT_WORDS = {"count", "qty", "quantity"}
_SAMPLE = 20  # values of a column the detectors read: enough to tell, quick to run


@dataclass
class Column:
    """One column of a tool's output (a table's, a JSON path's, a key's), and what to do with it."""

    name: str
    values: list[str]  # its values, as they were (the original output)
    masked: int = 0  # how many of them strict data mode masked in this output
    proposal: Proposal = "ask"
    reason: str = "unclear"  # keep: dates, ids... | mask: people, person, sensitive... | ask: repeats, amounts...
    detail: str | None = None  # what was found (EMAIL), or the values that repeat
    kept_now: bool = False  # a rule of yours keeps it in clear already
    everywhere: bool = True  # a rule for everywhere would change it here (no rule of the project's comes first)
    examples: list[str] = field(default_factory=list)  # up to three of its values


def _words(name: str) -> list[str]:
    last = name.rsplit(".", 1)[-1].replace("[]", "")
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", last).lower()
    return [w for w in re.split(r"[^a-z0-9àèéìòù]+", spaced) if w]


def technical_kind(name: str) -> str | None:
    """`id`, `date`, `state` or `count` when a column's name says its values are one of those (the dates
    of events, not of birth); None when it says nothing of the kind."""
    words = _words(name)
    if not words:
        return None
    last = words[-1]
    if last in _ID_WORDS:
        return "id"
    if any(w in _BIRTH or w in _SENSITIVE for w in words):
        return None
    if any(w in _DATE_WORDS for w in words) or (len(words) > 1 and (words[0] == "data" or last in _DATE_LAST)):
        return "date"
    if last in _STATE_WORDS:
        return "state"
    if last in _COUNT_WORDS:
        return "count"
    return None


def _collect(text: str, out: dict[str, list[str]], depth: int = 0) -> None:
    """The values of each column in ``text``: a JSON document by path, else a table or `key=value`."""
    stripped = text.strip()
    if depth < 3 and stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:

            def take(value: str, path: str, is_number: bool) -> str:
                name = path[3:] if path.startswith("[].") else path
                if name in _WRAPPERS and not is_number:
                    _collect(value, out, depth + 1)  # a table, or JSON, inside an agent's wrapper
                elif name and name not in _NOT_COLUMNS and not name.startswith("metadata."):
                    out.setdefault(name, []).append(value)
                return value

            map_json_values(stripped, take, numbers=True)
            return
    for cell in cells(text):
        out.setdefault(cell.field, []).append(text[cell.start : cell.end])


def strict_values(original: str, spans: Iterable[dict[str, Any]]) -> set[str]:
    """The values strict data mode masked in a tool's output: the originals of its replacements (as
    `recorder.reconstruct` gives them) that the `data` module made."""
    return {original[s["start"] : s["end"]] for s in spans if s.get("module") == "data"}


def columns_of(original: str, strict_values: Iterable[str] = ()) -> list[Column]:
    """The columns of a tool's output, each with its values; ``strict_values``: the values strict data
    mode masked in it (the ones the dashboard shows as masked)."""
    found: dict[str, list[str]] = {}
    _collect(original, found)
    masked = set(strict_values)
    out = []
    for name, raw in found.items():
        values = [v.strip() for v in raw if v.strip() and not _NOTHING.match(v.strip())]
        if not values:
            continue
        examples = list(dict.fromkeys(values))[:3]
        out.append(Column(name, values, sum(v in masked for v in values), examples=examples))
    return out


def _found(masker: Masker, values: Sequence[str]) -> tuple[str, str] | None:
    """What the detectors find in a column's values: ("secret" | "people", the type), or None."""
    types: Counter[str] = Counter()
    secret = False
    for value in list(dict.fromkeys(values))[:_SAMPLE]:
        for span in masker.detect(value, DetectContext(SlotKind.USER_TEXT), skip=("fields",)):
            if span.eclass >= EntityClass.NER:
                types[span.etype] += 1
                secret = secret or span.eclass == EntityClass.SECRET
    if not types:
        return None
    return ("secret" if secret else "people"), types.most_common(1)[0][0]


def _proposal(column: Column, masker: Masker) -> tuple[Proposal, str, str | None]:
    """Masked when its values or its name point to a person or a secret; in clear only when its name
    says it holds ids, dates of events, states or counts (the shape of its values alone never does:
    usernames repeat as states do); your call otherwise."""
    words, values = _words(column.name), column.values
    distinct = list(dict.fromkeys(values))
    if secret_kind(column.name.rsplit(".", 1)[-1].replace("[]", "")):
        return "mask", "secret-name", None
    if found := _found(masker, values):
        return "mask", found[0], found[1]
    if any(w in _PERSON for w in words) or (len(words) > 1 and words[-1] == "by"):  # created_by
        return "mask", "person", None
    if any(w in _SENSITIVE for w in words):
        return "mask", "sensitive", None
    if any(w in _NOTES for w in words) or any(len(v.split()) >= 5 for v in values):
        return "mask", "free-text", None
    kind = technical_kind(column.name)
    if all(any(d.match(v) for d in _DATES) for v in values):
        if any(w in _BIRTH for w in words):
            return "mask", "birth-dates", None
        return ("keep", "dates", None) if kind == "date" else ("ask", "dates-unclear", None)
    if all(v.lower() in _FLAGS for v in values):
        return ("keep", "flags", None) if kind == "state" else ("ask", "flags-unclear", None)
    if all(_UUID.match(v) for v in values) or (kind == "id" and all(_TOKEN.match(v) for v in values)):
        return "keep", "ids", None  # an id says which record, not who
    one_words = all(_TOKEN.match(v) and len(v) <= 24 and not _INTEGER.match(v) for v in values)
    if one_words and kind == "state" and len(distinct) <= 12:
        return "keep", "states", ", ".join(distinct[:5])
    if kind == "count" and all(_INTEGER.match(v) for v in values):
        return "keep", "counts", None
    if all(_AMOUNT.match(v) for v in values):
        return "ask", "amounts", None
    if one_words and len(values) >= 4 and len(distinct) <= min(8, len(values) // 2):
        return "ask", "repeats", ", ".join(distinct[:5])  # states, or the names of a few people
    return "ask", "unclear", None


def propose(
    columns: Sequence[Column], masker: Masker, fields: Sequence[FieldRule] = (), nearer: Sequence[FieldRule] = ()
) -> list[Column]:
    """Each column with its proposal and reason, worked out from its name and its values; whether a rule
    of yours (``fields``, the project's in effect) keeps it in clear already; and whether a rule for
    everywhere would change it there (``nearer``: the rules of the project's own layers, which come first)."""
    rules, near = FieldDetector(fields), FieldDetector(nearer)
    for column in columns:
        column.proposal, column.reason, column.detail = _proposal(column, masker)
        rule = rules._rule(column.name)
        column.kept_now = rule is not None and not rule.mask
        column.everywhere = near._rule(column.name) is None
    return list(columns)
