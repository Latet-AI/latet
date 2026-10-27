"""The applications API, in an identity's space on a Latet server: ``POST /_latet/apps/v1/scan``, ``mask`` and
``unmask``, each taking a list of texts (``{"texts": [...], "kind": "prose" | "data"}``) and answering a list in the
same order; and ``forget`` (Task 11b), taking a list of values (``{"values": [...]}``) and answering how many the vault
forgot and how many it did not hold. Mounted beside the plugin's bridge in an API key's space on a server alone
(``create_app``): a computer has none of it, nor a person's space.

The server's gate checks the call's token (``Authorization: Bearer``, an API key's: a person's is refused) and hands the
call on with what the token may do (``scope["latet.grant"]``, which no client can set): a call without it, or for
another identity, is refused here too. Each operation needs its own permission: ``scan``, ``mask``, ``unmask`` or
``forget``, which no other brings and a token without permissions of its own does not hold.

Every call runs on the space's bridge thread, as the plugin's do (an unmask's entry in the administration log apart,
written in the server's log threads), with a session of its own that keeps nothing (``Session.kept``): an
application's call is no conversation. ``scan`` says where the space's rules find values, and keeps nothing: the vault
learns nothing, and the local reviewer, which would teach it, does not read it (R1). ``mask`` masks each text as one
text with the space's fakes, the space's own fakes taken as values like any other (``keep_surrogates=False``), then lets
the local reviewer read it within its time (R8), every text whatever the gateway's requests wait for; a text it stops
answers ``blocked`` and no text, and the Live Reviewer never holds one: an application cannot wait for a person.
``unmask`` restores every fake of the space's vault, whatever call made it, and is written in the administration log
before its answer is made: a log that cannot be written refuses it. ``forget`` takes each value, found exactly as it is
given, out of the space's vault with everything that names it (``PgVaultStore.forget_values``), its entry in the
administration log written in the same transaction (``apps.forgotten``: a log that cannot be written forgets nothing),
then out of the space's memory, its engine's masked texts dropped; the other instances drop it as they hear of it. A
value forgotten is masked with a new fake the next time it is met, and its old fake restores nothing. Positions are
Unicode code points: in the text sent for ``scan``, in the text answered for ``mask``.

An error is ``{"error": words}``, and its words never quote a text: 400 a body that is not one, 403 a permission the
token lacks, 404 another path under the prefix, 405 another method, 422 texts whose words name more of the vault's
values or fakes than one call may weigh (nothing of it masked or restored), 507 a vault that is full, 500 another
masking failure (by its words, which name no value), 503 a store that does not answer, or an unmask or a forget the log
did not take."""

from __future__ import annotations

import functools
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from latet import observe
from latet.bridge.api import STORE_UNAVAILABLE, a_store_not_answering
from latet.bridge.review import MASKED, inline_review
from latet.engine import wellformed
from latet.engine.masker import MaskReport
from latet.engine.pipeline import WITHHELD_DUMP, Session
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import MaskingError
from latet.gateway.mediate import Masked, Mediator, ReviewItem
from latet.gateway.recorder import replacement_dicts
from latet.logline import Paced, described
from latet.server.settings import APPS_API
from latet.server.vault import NotRecorded, TooManyCandidates, VaultFull

if TYPE_CHECKING:
    from latet.gateway.app import OnServer

__all__ = ["APPS_API", "APPS_REVIEW_SECONDS", "MOST_TEXTS", "Apps", "values_asked"]

log = logging.getLogger(__name__)

MOST_TEXTS = 1000  # texts one call takes at most, and values one forget takes at most
APPS_REVIEW_SECONDS = 30.0  # what the local reviewer is given on a call, when llm_review.budget_ms says nothing (R8)
APPS_SESSION = "apps"  # the key of every call's session, which keeps nothing
# What a text is: written by people (messages, documents), masked as the gateway masks your messages; or data (logs,
# records, a program's output), masked as a tool's output (R2).
KINDS = {"prose": SlotKind.USER_TEXT, "data": SlotKind.TOOL_RESULT}
# What the local reviewer reads of a call before it answers: every text, of either kind, whatever ``llm_review.inline``
# narrows for the gateway's requests, whose other texts it reads after they leave. An application has no later moment.
_INLINE = tuple(kind.value for kind in KINDS.values())
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]  # each answered here: 405 but POST
NOT_A_KEY = "the applications API takes an API key's token"
NO_LOG = "the unmask could not be written in the administration log, so nothing was restored: try again"
NO_FORGET_LOG = "the forget could not be written in the administration log, so nothing was forgotten: try again"
FAILED = "the applications API could not answer this call: try again"
_WITHHELD = "[withheld by Latet: "


def _error(status: int, words: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": words}, status_code=status, headers=headers)


def _texts(raw: bytes, kinds: bool) -> tuple[list[str], SlotKind] | str:
    """In the bridge's thread: the texts of a call's body, and their kind (``kinds``: the call takes one, ``prose`` when
    it says none); or the words of its refusal (400), which quote nothing of it."""
    try:
        data = wellformed.loads(raw)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return 'the body is a JSON object: {"texts": [text, ...], "kind": "prose" or "data"}'
    texts = data.get("texts")
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        return "texts is a list of texts"
    if not texts:
        return "texts holds one text at least"
    if len(texts) > MOST_TEXTS:
        return f"texts holds {MOST_TEXTS} texts at most: send the others in another call"
    kind = data.get("kind", "prose") if kinds else "prose"
    if not isinstance(kind, str) or kind not in KINDS:
        return "kind is prose (text people wrote) or data (logs, records, a program's output)"
    return texts, KINDS[kind]


def values_asked(raw: bytes) -> tuple[list[str]] | str:
    """In the bridge's thread (in the administration's, for its own forget): the values a forget's body names
    (``{"values": [value, ...]}``, ``MOST_TEXTS`` at most, each a text); or the words of its refusal (400), which quote
    nothing of it."""
    try:
        data = wellformed.loads(raw)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return 'the body is a JSON object: {"values": [value, ...]}'
    values = data.get("values")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return "values is a list of the values to forget, each a text"
    if not values:
        return "values holds one value at least"
    if len(values) > MOST_TEXTS:
        return f"values holds {MOST_TEXTS} values at most: send the others in another call"
    return (values,)


def _counts(types: Iterable[str]) -> dict[str, int]:
    return dict(Counter(types))


def _place(etype: str, eclass: EntityClass | str, start: int, end: int) -> dict[str, Any]:
    """A value found or replaced, as an application reads it: its type, its class, where it is."""
    name = eclass.name if isinstance(eclass, EntityClass) else eclass
    return {"type": etype, "class": name.lower(), "start": start, "end": end}


def _rendered(answer: dict[str, Any]) -> JSONResponse:
    """In the bridge's thread: an answer of texts, rendered there (the event loop every identity shares would wait)."""
    return JSONResponse(answer)


def _withheld_reason(done: Masked) -> str:
    """Why a text Latet withheld in its masking (a tool output past the dump guard) is not answered: its notice's own
    words, which count values and name none."""
    if done.text.startswith(WITHHELD_DUMP):
        return done.text.removeprefix(_WITHHELD).removesuffix("]")
    return "Latet withheld this text"


def _masked_results(out: list[dict[str, Any]], degraded: list[str]) -> tuple[JSONResponse, int]:
    """In the bridge's thread: ``mask``'s answer, each text with its replacements' places in the text it answers, or
    why it is not answered (``blocked``: the local reviewer stopped it, or its masking withheld it, the first time or
    once masked again with what the reviewer found), and what ran weaker on the call (``degraded``: a detector's name,
    ``llm-review``), said only when something did; and how many values were replaced in the texts answered."""
    results: list[dict[str, Any]] = []
    replaced = 0
    for entry in out:
        done: Masked = entry[MASKED]
        if entry.get("blocked") or done.withheld:
            results.append({"blocked": entry.get("blocked") or _withheld_reason(done)})
            continue
        placed = replacement_dicts(done.replacements)
        places = [_place(r["type"], r["class"], r["masked_start"], r["masked_end"]) for r in placed]
        replaced += len(places)
        results.append({"text": done.text, "replacements": places, "counts": _counts(p["type"] for p in places)})
    answer: dict[str, Any] = {"results": results}
    if degraded:
        answer["degraded"] = sorted(set(degraded))
    return _rendered(answer), replaced


class Apps:
    """The applications API's routes in an identity's space on a Latet server (the module's words)."""

    def __init__(self, mediator: Mediator, on_server: OnServer) -> None:
        self.mediator = mediator
        self.engine = mediator.engine
        self.on_server = on_server
        self._unanswered = Paced()  # calls answered 503 because a store did not answer, said once a minute
        self._failed = Paced()  # calls that failed otherwise, said once a minute by the error's type
        self._unlogged = Paced()  # unmasks and forgets refused because the administration log did not take them

    def routes(self) -> list[Route]:
        return [
            Route(f"{APPS_API}/scan", self._call("scan", self.scan), methods=_METHODS),
            Route(f"{APPS_API}/mask", self._call("mask", self.mask), methods=_METHODS),
            Route(f"{APPS_API}/unmask", self._call("unmask", self.unmask), methods=_METHODS),
            Route(f"{APPS_API}/forget", self._call("forget", self.forget, values_asked), methods=_METHODS),
            Route(APPS_API, self.nowhere, methods=_METHODS),
            Route(f"{APPS_API}/{{rest:path}}", self.nowhere, methods=_METHODS),
        ]

    def _through_the_gate(self, request: Request) -> dict[str, Any] | None:
        """What the token of a call the server's gate handed on may do (``latet.grant``), for this space's identity
        alone: None for a call that did not come so."""
        grant = request.scope.get("latet.grant")
        if request.scope.get("latet.identity") != self.on_server.identity or not isinstance(grant, dict):
            return None
        return grant

    def _call(
        self,
        operation: str,
        work: Callable[..., Awaitable[Response]],
        read: Callable[[bytes], tuple[Any, ...] | str] | None = None,
    ) -> Callable[[Request], Awaitable[Response]]:
        """``work`` behind the checks of every call: the gate's grant (403), the method (405), the permission of
        ``operation`` (403), the body (400, read and checked in the bridge's thread by ``read``, ``_texts`` unless
        another is given, whose parts ``work`` is given after the request and the grant); and its failures answered: a
        full vault 507, another masking failure 500, a store that does not answer 503, anything else 500, each said in
        the log by its words when they name no value (a masking failure's), else by the error's type."""
        reader = read if read is not None else functools.partial(_texts, kinds=operation != "unmask")

        async def endpoint(request: Request) -> Response:
            grant = self._through_the_gate(request)
            if grant is None:
                return _error(403, NOT_A_KEY)
            if request.method != "POST":
                return _error(405, "the applications API takes POST", {"allow": "POST"})
            permissions = grant.get("permissions")
            if not isinstance(permissions, list) or operation not in permissions:
                return _error(403, f"this token lacks the {operation} permission")
            try:
                raw = await request.body()
            except ClientDisconnect:  # gone, or its body past the server's limit (answered 413 there): nothing to say
                return Response(status_code=400)
            try:
                given = await self.mediator.run_bridge(reader, raw)
                if isinstance(given, str):
                    return _error(400, given)
                return await work(request, grant, *given)
            except VaultFull as exc:  # nothing of the call is answered, nothing leaves in clear (R9)
                full = f"its limit is {exc.limit} values" if exc.limit is not None else "it holds the most it may"
                return _error(507, f"this API key's vault is full: {full}; latet server keys limit raises it")
            except TooManyCandidates as exc:  # nothing of it masked, nor restored: its words say so, and name no value
                return _error(422, str(exc))
            except MaskingError as exc:  # its words name no value, as the bridge's
                # The vault's store, asked for the values a text may hold, did not answer (``StoredVault``): as below.
                if exc.__cause__ is not None and a_store_not_answering(exc.__cause__):
                    return self._not_answering(exc.__cause__)
                log.error("an applications API call failed: %s", exc)
                return _error(500, f"masking failed: {exc}")
            except Exception as exc:  # by its type alone: a driver's words, or a traceback, can quote a text
                if a_store_not_answering(exc):
                    return self._not_answering(exc)
                if count := self._failed.due():
                    many = f" ({count} since the line before)" if count > 1 else ""
                    log.error("an applications API call failed (%s)%s", described(exc), many)
                return _error(500, FAILED)

        return endpoint

    def _not_answering(self, exc: BaseException) -> Response:
        """A call a store did not answer: 503, said in the log once a minute by the error's type."""
        if count := self._unanswered.due():
            many = f" ({count} since the line before)" if count > 1 else ""
            said = "an applications API call was answered 503: a store did not answer (%s)%s"
            log.warning(said, described(exc), many)
        return _error(503, STORE_UNAVAILABLE)

    async def nowhere(self, request: Request) -> Response:
        """Another path under the prefix: 404, once the call is known to have come through the gate."""
        if self._through_the_gate(request) is None:
            return _error(403, NOT_A_KEY)
        return _error(404, "there is no such route in the applications API")

    async def scan(self, request: Request, grant: dict[str, Any], texts: list[str], kind: SlotKind) -> Response:
        """``{texts, kind?}`` → ``{results: [{findings: [{type, class, start, end}], counts}]}``."""
        answer, found = await self.mediator.run_bridge(self._scan, texts, kind)
        observe.apps("scan", len(texts), found)
        return answer

    def _scan(self, texts: list[str], kind: SlotKind) -> tuple[JSONResponse, int]:
        """In the bridge's thread: where the space's rules find values in each text, by the rules ``mask`` masks it by,
        with nothing kept (``Engine.find``). The values the texts may hold are brought into memory first, in one call
        to the vault's store for all of them (``Vault.prefetch``), and held while they are read."""
        session = Session(APPS_SESSION, kept=False)
        results: list[dict[str, Any]] = []
        found = 0
        vault = self.engine.vault
        with vault.holding(scoped=True):
            vault.prefetch(texts)
            for text in texts:
                spans = self.engine.find(text, kind, session)
                found += len(spans)
                findings = [_place(s.etype, s.eclass, s.start, s.end) for s in spans]
                results.append({"findings": findings, "counts": _counts(s.etype for s in spans)})
        return _rendered({"results": results}), found

    async def mask(self, request: Request, grant: dict[str, Any], texts: list[str], kind: SlotKind) -> Response:
        """``{texts, kind?}`` → ``{results: [{text, replacements: [{type, class, start, end}], counts}], degraded?}``,
        a result ``{blocked}`` for a text the local reviewer or the dump guard stopped, and ``degraded`` the names of
        what ran weaker on the call (a detector that did not read a text, a review that did not end), when something
        did. The local reviewer reads every text, of either kind, within ``llm_review.budget_ms``, else
        ``APPS_REVIEW_SECONDS`` (R8), and nothing of them later; the Live Reviewer never holds them."""
        session, out, reviews, degraded = await self.mediator.run_bridge(self._mask, texts, kind)
        if reviews:
            degraded = await inline_review(
                self.mediator, session, reviews, out, degraded, budget_s=APPS_REVIEW_SECONDS, background=False
            )
        answer, replaced = await self.mediator.run_bridge(_masked_results, out, degraded)
        observe.apps("mask", len(texts), replaced)
        return answer

    def _mask(
        self, texts: list[str], kind: SlotKind
    ) -> tuple[Session, list[dict[str, Any]], list[ReviewItem], list[str]]:
        """In the bridge's thread: each text masked as one text, a value found in it taken as real whatever it is (the
        space's own fakes too), then those a later text made stale masked again (``settle``); what the local reviewer is
        to read of them, and what ran weaker (``MaskReport.degraded``). The values the texts may hold are brought into
        memory first, in one call to the vault's store for all of them (``Vault.prefetch``), and held while they are
        masked: a value found once is masked for ever."""
        session = Session(APPS_SESSION, kept=False)  # a new one each call: it holds what it was shown in its memory
        report = MaskReport()
        config = self.mediator.review_config(session)
        if config is not None:  # what the gateway's requests do not wait for is reviewed after; an application's, now
            config = config.model_copy(update={"inline": list(_INLINE)})  # a list of its own, each call
        masker = self.mediator._masker(session) if config is not None else None
        vault = self.engine.vault
        with vault.holding(scoped=True):
            vault.prefetch(texts)
            call = self.mediator.call()
            done = [
                self.mediator.mask_value(text, kind, session, report, keep_surrogates=False, call=call, as_text=True)
                for text in texts
            ]
            self.mediator.settle(call)  # a text masked before a later one of the call taught the vault a value in it
        out: list[dict[str, Any]] = []
        reviews: list[ReviewItem] = []
        for index, masked in enumerate(done):
            out.append({"text": masked.text, "count": len(masked.replacements), MASKED: masked})
            if config is not None and masker is not None:
                review = self.mediator.review_item(config, masker, index, kind, masked)
                if review is not None:
                    reviews.append(review)
        return session, out, reviews, sorted(set(report.degraded))

    async def unmask(self, request: Request, grant: dict[str, Any], texts: list[str], kind: SlotKind) -> Response:
        """``{texts}`` → ``{results: [{text, restored}]}``: every fake of the space's vault in each text restored, and
        how many values; written in the administration log before the answer is made (``apps.unmasked``: the token, how
        many texts and values, the client's address; never a text or a value). A log that does not take it refuses the
        call (503), nothing restored in it.

        The texts are restored in the bridge's thread, the entry written in the server's log threads (``audit``),
        where it waits on the log's chain, one entry at a time for the whole server, with the bridge's thread free for
        the space's other calls meanwhile; then the answer is rendered in the bridge's thread (Ruling T5a)."""
        client = request.scope.get("client")
        address = str(client[0]) if client else None
        results, restored = await self.mediator.run_bridge(self._unmask, texts)
        details = {"token": str(grant.get("token")), "texts": len(texts), "restored": restored}
        try:
            if self.on_server.audit is None:
                raise RuntimeError("this space has no administration log")
            await self.on_server.audit("apps.unmasked", details, address)
        except Exception as exc:  # by its type alone: a driver's words can name an address
            if count := self._unlogged.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.error(
                    "an unmask was refused: the administration log did not take its entry (%s)%s", described(exc), many
                )
            return _error(503, NO_LOG)
        answer = await self.mediator.run_bridge(_rendered, {"results": results})
        observe.apps("unmask", len(texts), restored)
        return answer

    def _unmask(self, texts: list[str]) -> tuple[list[dict[str, Any]], int]:
        """In the bridge's thread: the texts restored (``Engine.unmask``), and how many values. The fakes the texts may
        hold are asked for first, in one call to the vault's store for all of them (``Vault.prefetch_fakes``)."""
        results: list[dict[str, Any]] = []
        restored = 0
        vault = self.engine.vault
        with vault.holding():
            vault.prefetch_fakes(texts)
            for text in texts:
                found, count = self.engine.unmask(text)
                restored += count
                results.append({"text": found, "restored": count})
        return results, restored

    async def forget(self, request: Request, grant: dict[str, Any], values: list[str]) -> Response:
        """``{values}`` → ``{forgotten, unknown}``: each value, found exactly as it is given (another case, another
        written form is another value, as the vault keeps it), taken out of the space's vault with everything that names
        it and the local reviewer's remembered answers that found it, and how many of the values (each counted once)
        the vault did not hold (Task 11b). Written in the administration log in the same transaction, before the answer
        is made (``apps.forgotten``: the token, how many values, how many forgotten and not held, the client's address;
        never a value), in the server's log threads (``OnServer.forget``): a log that does not take it forgets nothing
        (503). Then out of the space's memory, in the bridge's thread: its working set (``StoredVault.forgotten``) and
        the texts its engine masked before, which may hold their fakes; the other instances drop them as they hear of
        it (``spaces._forget``)."""
        client = request.scope.get("client")
        address = str(client[0]) if client else None
        if self.on_server.forget is None:
            raise RuntimeError("this space cannot forget values")  # by its type: 500
        details = {"token": str(grant.get("token")), "values": len(values)}
        try:
            ids, unknown, generation = await self.on_server.forget(values, details, address)
        except NotRecorded as exc:
            if count := self._unlogged.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.error("a forget was refused: the administration log did not take its entry (%s)%s", exc, many)
            return _error(503, NO_FORGET_LOG)
        await self.mediator.run_bridge(self._forgotten, ids, generation, values)
        observe.apps("forget", len(values), len(ids))
        return JSONResponse({"forgotten": len(ids), "unknown": unknown})

    def _forgotten(self, ids: list[str], generation: int | None, values: list[str]) -> None:
        """In the bridge's thread: the values its store forgot out of the space's memory (an API key's working set at
        once, with the generation its new fakes are drawn in from then on, ``StoredVault.forgotten``; another vault one
        by one), the local reviewer's answers remembered in memory that found one of ``values`` (review 11b's N3), and
        the texts its engine masked before dropped, held values or not: one may hold a fake of theirs."""
        vault = self.engine.vault
        if isinstance(vault, StoredVault):
            vault.forgotten(ids, generation)
            if self.mediator.reviewer is not None:
                self.mediator.reviewer.forget_answers(values)
        else:
            for entity_id in ids:
                vault.forget(entity_id)
        self.engine.rules_changed()
