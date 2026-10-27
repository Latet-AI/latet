---
title: Applications API reference
description: The HTTP contract of a Latet server's applications API, its answers, its errors and its limits.
sidebar:
  label: Applications API
---

The [applications API](../../applications/) of a Latet server answers four operations under
`https://<your server>/_latet/apps/v1/`. This is version 1: a change that would break a program
written for it comes as `v2`, beside it. The [OpenAPI description](../../apps-api.openapi.json)
says the same for tools that generate a client; Python programs can use `latet-client`
([logs](../../applications/logs/), [RAG](../../applications/rag/)).

## Authentication

Each call carries an API key's token in one header:

```http
Authorization: Bearer latet_key_...
```

A token is `latet_key_` followed by 43 characters, and it is made by the administrator
(`latet server keys add`, `keys add-token`). Each operation needs its own permission: `scan`, `mask`
(which brings `scan`), `unmask` or `forget` (which no other brings, and which a key's first token does
not have). The server refuses:

- with **401** and `WWW-Authenticate: Bearer`, a call with no `Authorization` header, with two, with
  another scheme than `Bearer`, or with a token it does not know (revoked, rotated, of a key disabled
  or removed);
- with **403**, a person's computer token, a token without the operation's permission, and any request
  that carries an `Origin` or a `Sec-Fetch-Site` header, as a browser's does: the API is for programs,
  never for a web page.

Tokens the server refuses count against the address they come from, as on every route of the server:
past the limit, 429. Call the server over HTTPS; the token and the texts would otherwise travel in
clear.

## Requests

| Operation | Path | Permission | Body |
| --- | --- | --- | --- |
| Scan | `POST /_latet/apps/v1/scan` | `scan` | `{"texts": [...], "kind": "prose"}` |
| Mask | `POST /_latet/apps/v1/mask` | `mask` | `{"texts": [...], "kind": "prose"}` |
| Unmask | `POST /_latet/apps/v1/unmask` | `unmask` | `{"texts": [...]}` |
| Forget | `POST /_latet/apps/v1/forget` | `forget` | `{"values": [...]}` |

- `texts` is a list of 1 to 1,000 strings, answered in the same order. An empty text comes back as it
  is, with no findings and `restored: 0`.
- `values` is a list of 1 to 1,000 strings, the values to take out of the key's vault, each found
  exactly as written ([forgetting values](../../applications/forget/)).
- `kind` is `prose` (text people wrote, the default) or `data` (logs, records, a program's output); see
  [prose and data](../../applications/#prose-and-data). `null` or another word is refused. Unmask
  takes no kind.
- The body is a JSON object of at most the server's `apps_request_mb` (8 MiB unless its settings say
  otherwise, never more than its `max_request_mb`).

Scan and unmask with curl, the token handed over on standard input as in
[the first call](../../applications/#a-first-call), which masks; their answers are below:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Refund the order of mario.rossi@acme.it to IT60X0542811101000000123456, please."]}' \
  https://latet.acme.lan/_latet/apps/v1/scan
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Refund the order of tommaso.gallo@margie-6e5a.example to IT17F3945471087320356678487, please."]}' \
  https://latet.acme.lan/_latet/apps/v1/unmask
```

## Answers

Every answer of scan, mask and unmask is a JSON object whose `results` hold one entry for each text
sent, in order; a forget answers two counts.

**Scan** says where the values are in the text sent, and keeps nothing:

```json
{"results": [{"findings": [{"type": "EMAIL", "class": "pii", "start": 20, "end": 39},
                           {"type": "IBAN", "class": "pii", "start": 43, "end": 70}],
              "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

**Mask** gives each text with the key's fakes, and where they are in the text it gives:

```json
{"results": [{"text": "Refund the order of tommaso.gallo@margie-6e5a.example to IT17F3945471087320356678487, please.",
              "replacements": [{"type": "EMAIL", "class": "pii", "start": 20, "end": 53},
                               {"type": "IBAN", "class": "pii", "start": 57, "end": 84}],
              "counts": {"EMAIL": 1, "IBAN": 1}},
             {"blocked": "the local reviewer could not read the user text (it took longer than the 30 seconds an application's call waits for it, which llm_review.budget_ms changes), so it was not sent"}],
 "degraded": ["llm-review"]}
```

- A result with `blocked` and no `text` is a text Latet does not answer: the local reviewer stopped it,
  its review failed and the key's `llm_review.on_failure` is `stop`, or the dump guard withheld it.
  The words name no value.
- `degraded`, present only when something ran weaker on the call, names what did: a detector that
  could not read a text (such as `ner`) or `llm-review`.

**Unmask** gives each text with every fake of the key's vault replaced by its value, and how many
different values it restored:

```json
{"results": [{"text": "Refund the order of mario.rossi@acme.it to IT60X0542811101000000123456, please.",
              "restored": 2}]}
```

**Forget** says how many of the values the key's vault held, now gone with their fakes, and how many
it did not hold, each value counted once:

```json
{"forgotten": 1, "unknown": 1}
```

### Positions

`start` and `end` count Unicode code points (`end` not included): in Python, `text[start:end]` is the
value. JavaScript, Java and C# count UTF-16 units, where a character beyond the Basic Multilingual
Plane, such as most emoji, takes two. In `Write to 👩‍💻 mario.rossi@acme.it` the address starts at 13
in code points and at 15 in UTF-16 units; in JavaScript, `Array.from(text).slice(start, end).join("")`
gives it.

### Types and classes

`type` is the value's type: `EMAIL`, `PHONE`, `IBAN`, `CREDIT_CARD`, `IP_ADDRESS` and the others of the
[detectors](../detectors/), a secret's rule, or the type of one of the key's terms, recognizers or field
rules. `class` is the family of what found it: `secret`, `custom` (the key's terms and recognizers),
`identity`, `pii`, `ner` (the name model), `llm` (the local reviewer) or `struct` (strict data mode).

## Errors

An error is `{"error": "<words>"}`. The words are for people: they never quote a text that was sent,
and the refusals of the server's front (authentication, limits) begin with `latet: `. Decide by the
status.

| Status | When | What to do |
| --- | --- | --- |
| 400 | The body is not a JSON object with `texts` a list of 1 to 1,000 strings and, for scan and mask, a `kind` that is `prose` or `data`; for forget, with `values` a list of 1 to 1,000 strings. | Fix the call. |
| 401 | No token, two `Authorization` headers, another scheme, or a token the server does not know. | Check the token; ask for a new one. |
| 403 | The token lacks the permission (`this token lacks the mask permission`), it is a person's token, or a web page made the request. | Use a token with the permission. |
| 404 | Another path under `/_latet/apps/v1/`. | |
| 405 | Another method than `POST` (`Allow: POST`). | |
| 413 | The body is larger than `apps_request_mb`. | Send fewer texts in a call. |
| 422 | Scan, mask and unmask: the texts name more than 100,000 values or fakes of the vault, more than one call may weigh. Nothing was masked or restored. | Send fewer texts in a call. |
| 429 | Too many refused tokens from this address, or the key has `requests_in_flight` application requests on this instance (16 unless the settings say otherwise). | Wait for `Retry-After`, then try again. |
| 500 | Another failure; its words name no value. | Try again; tell the administrator if it lasts. |
| 503 | A store of the server did not answer, the key's space is not available on this instance now, or the administration log did not take the entry: for unmask nothing was restored, for forget nothing was forgotten. | Wait (`Retry-After` when given), then try again. |
| 507 | Mask only: the key's vault holds the most values it may. Nothing of the call was answered, and nothing left in clear. | An administrator raises the limit with `latet server keys limit`. |

A mask or an unmask is safe to repeat: a value the vault kept has the same fake again, and a value a
failed call did not keep gets a fake again. A forget is safe to repeat too: a value already gone is
counted as `unknown`. A failed mask call may have kept some of its new values
([a key's vault](../../applications/#a-keys-vault) says when).

## Headers

- Every answer says `Cache-Control: no-store`: no cache keeps a text or a value.
- `Retry-After`, in seconds, comes with every 429, and with some 503s.

## Limits

| Limit | Value | Set by |
| --- | --- | --- |
| Texts in a call, or values in a forget | 1,000 | Latet |
| Body of a call | 8 MiB | the server's `apps_request_mb` |
| Application requests of a key on an instance at once | 16 | the server's `requests_in_flight` |
| Values or fakes of the vault one call's texts may name | 100,000 | Latet |
| Values a key's vault holds | 200,000 | the server's `vault_limit`, or the key's own: `latet server keys limit` |
| Values of a key's vault an instance keeps in memory | 5,000 | the server's `key_working_values` |

## What the server records

- **The administration log** gets one `apps.unmasked` entry for each unmask call, before its answer
  is sent: the token's id, how many texts and how many values were restored (`{"token": "t_...",
  "texts": 3, "restored": 5}`), and the client's address. Never a text or a value. Scan and mask are
  not in it. Read them with `latet server log --action apps.unmasked`. Each forget call is an
  `apps.forgotten` entry, written in the same transaction as the forget: the token's id, how many
  values were asked, forgotten and not held (`{"token": "t_...", "values": 2, "forgotten": 1,
  "unknown": 1}`), and the client's address.
- **The metrics** count the API's requests as `latet_http_requests_total{kind="apps"}`, by status, and
  its texts and values by operation: `latet_apps_texts_total{operation}` and
  `latet_apps_values_total{operation}` (the values scan found, mask replaced and unmask restored; for
  forget, the values asked and those forgotten).
  They have no label for the key: who did what is in the administration log.
- **The server's log** has a line for each call: its method, its path, its status and how long it
  took, never a header, the body or a value. Refusals (401, 403, 404, 405, 413, 429, and the server's
  own) are summed up once a minute, with how many there were, instead of a line each.
