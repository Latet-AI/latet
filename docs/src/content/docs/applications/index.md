---
title: The applications API
description: Scan, mask and unmask text from your own programs, with an API key of a Latet server.
sidebar:
  label: Overview
---

Latet protects what an agent sends to a model. Programs send sensitive text to other places too:
their logs to Datadog, Splunk or Elastic, their documents to the index of a RAG application. The
applications API lets a program ask a Latet server, with an API key's token, to:

- **scan** a text: what is sensitive in it, and where;
- **mask** it: the same text with realistic fakes in place of its values;
- **unmask** it: the real values back in place of the fakes;
- **forget** values: take them out of the key's vault, when a person asks that their data leave it
  ([forgetting values](forget/)).

It is a feature of a Latet server: API keys and their tokens exist only there, and a computer without
a server has none of it. To try it on your own computer, run a Community server there with the server
profile of `compose.yaml` ([server operations](../concepts/server-operations/)).

What it gives that a redaction rule does not:

- **The same fake every time.** A value gets the same fake in every call, on every instance of the
  server, for as long as the key's vault keeps it. In a log service, charts and searches still work,
  and one customer can be followed across days of logs without anyone seeing who they are. In a RAG
  index, a person stays the same person across documents.
- **One vault for a whole flow.** Documents and questions masked with the same key share their
  fakes, so a model reasons on consistent fakes, and unmask puts the real values back in its answer.
- **Reversible, under control.** Unmask is a permission of its own, it reaches the key's own fakes
  only, and every unmask through the applications API is written in the server's administration log.

Two pages walk through the two uses it was made for: [logs](logs/) and [RAG](rag/). The
[HTTP reference](../reference/apps-api/) gives the contract for programs in any language, and the
API's [OpenAPI description](../apps-api.openapi.json) lets a client be generated from it. Python
programs can use the [`latet-client`](https://pypi.org/project/latet-client/) package, which works
with LangChain, and n8n workflows the [Latet nodes for n8n](n8n/).

## A first call

A program sends a list of texts and gets a list back, in the same order:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Refund the order of mario.rossi@acme.it to IT60X0542811101000000123456, please."]}' \
  https://latet.acme.lan/_latet/apps/v1/mask
```

```json
{"results": [{"text": "Refund the order of tommaso.gallo@margie-6e5a.example to IT17F3945471087320356678487, please.",
  "replacements": [{"type": "EMAIL", "class": "pii", "start": 20, "end": 53},
                   {"type": "IBAN", "class": "pii", "start": 57, "end": 84}],
  "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

`shop.key` holds the key's first token, which `keys add` writes ([below](#api-keys-and-tokens)).
`printf`, which the shell runs itself, hands the header to curl on its standard input (`-H @-`), so
the token is not on curl's command line, where other users of the computer could read it. The fakes
are the key's own: another key, or another server, gives other ones. With a token that may unmask,
the same text sent to `/_latet/apps/v1/unmask` comes back as it was, with `"restored": 2`.

## API keys and tokens

An API key is one space on the server: its rules, its vault of fakes, its history. It can have
several **tokens**, the secrets that act in that space, and each token has its own **permissions**:

| Permission | What the token may do |
| --- | --- |
| `scan` | Say what a text holds and where; it changes nothing. |
| `mask` | Replace a text's values with the key's fakes. A token that may mask may scan too. |
| `unmask` | Put the real values back in place of the key's fakes. |
| `forget` | Take values out of the key's vault ([forgetting values](forget/)). No other permission brings it. |

Give each program only what it needs. An application that ships its logs needs `mask`; the support
tool that reads a line copied from the log service needs `unmask`; a RAG application that masks its
documents and restores the model's answer needs both.

```sh
latet server --server https://latet.acme.lan keys add "Shop" --out shop.key
latet server --server https://latet.acme.lan keys add-token "Shop" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop" support --unmask --out support.token
latet server --server https://latet.acme.lan keys tokens "Shop"
```

The first token of a key, the one `keys add` makes, has `scan`, `mask` and `unmask`, and so does the
token every key made before tokens had permissions: those keys work as before. None of them may forget:
`forget` is given to a token of its own (`keys add-token "Shop" erasure --forget`). The key's first
token also has an address for models, the
[gateway](../getting-started/server/#give-a-program-an-api-key), and serves the key's agents through it:
`keys tokens` marks it `serves agents (has an address)`, so that it is not revoked in place of a token
added with the same permissions.
A token made with `keys add-token` has no address, whatever its permissions, `--mask --unmask`
included: the gateway and the plugin's bridge refuse it (403), and it calls the applications API alone.

Each token stops on its own: `keys tokens "Shop" --revoke t_4kxq2m7zap` revokes one by the id
`keys tokens` lists, within seconds on every instance. `keys rotate "Shop" --token t_4kxq2m7zap --out
shipper.token` replaces that token with a new one, of a new secret and a new id, with the same name and
permissions; a key with one token needs no `--token`. Each change is in the administration log
(`token.added`, `token.revoked`, `key.rotated`), with the tokens' ids, never a secret.

The applications API takes an API key's tokens only: a person's computer token is refused (403), and
so is a request a web page makes. People use their dashboard.

## Prose and data

`scan` and `mask` take a `kind` for the texts of a call:

- `prose` (the default): text people wrote, such as messages, tickets and documents. It is masked as
  the gateway masks your own messages.
- `data`: logs, records, a program's output. It is masked as the gateway masks a tool's output.

The difference is in what is likely: in the `balanced` profile, names are looked for in prose only
(with the [local name model](../concepts/server-operations/#recognition-models-on-a-server)
installed on the server), and a run of digits counts as a phone number in prose only, where in a log
it is far more often an id. With `phone_regions: [IT]` in the key's rules, the number in
`Please call me at 3471234567 tomorrow.` gets a fake as prose and stays as it is as data. A JSON
document sent as a text is masked as text, its layout kept, so the positions in the answer are
positions in it.

## What a mask reads

`scan` and `mask` use the key's settings as they are when the call arrives: the key's own rules over
the server's shared settings, as the gateway does for the key's requests. There are no rules for the
API alone. Set them with `latet server config --key` and `latet server rules --key`
([shared settings and API keys' rules](../getting-started/server/#set-the-shared-settings-and-api-keys-rules)).

- **The local reviewer**, when the key's rules set one, reads every text of a `mask` call before the
  answer, whatever `llm_review.inline` says for the gateway: an application has no later moment. It
  is given `llm_review.budget_ms` when set, and 30 seconds otherwise. A text whose review fails
  follows `llm_review.on_failure`: with `stop` (the default) it is answered `blocked`, with `send` it
  is answered as the rules masked it. So with a reviewer that is down, every text is blocked until it
  answers again.
- **`scan` does not run the local reviewer.** The reviewer teaches the vault what it finds, and scan
  changes nothing. On a key with a reviewer, `scan` can report less than `mask` replaces.
- **The Live Reviewer never holds an application's text.** An application cannot wait for a person:
  a text is masked by the rules and the local reviewer alone, even when the key's rules turn the Live
  Reviewer on.
- **The dump guard** applies to `data` texts as to a tool's output: one holding more personal values
  than `dump_guard` allows is answered `blocked`.

A blocked text comes back as `{"blocked": "<why>"}` with no text, and the words name no value. When
something ran weaker on the call (a detector that could not read a text, a review that did not end),
the answer says so in `"degraded"`, with the names of what did, such as `ner` or `llm-review`.

## Masked twice, unmasked twice

`mask` takes every value it finds as real, the key's own fakes too: no one can tell from a string
whether it is a fake or a real value that looks like one. A text masked twice holds fakes of fakes,
and one unmask takes it back one step:

| Step | Text |
| --- | --- |
| Original | `Write to mario.rossi@acme.it` |
| Masked | `Write to tommaso.gallo@margie-6e5a.example` |
| Masked again | `Write to simone.desantis@humongous-0e54.example` |
| Unmasked once | `Write to tommaso.gallo@margie-6e5a.example` |

Mask a text once, from its original. The gateway follows the same rule: a text masked through the API
and then sent to a model through Latet's gateway is masked again there, and the model's answer comes
back with the API's fakes in it. A RAG application therefore sends the masked text to the model
directly and unmasks the answer through the API ([RAG](rag/)).

## Who can learn what

**Unmask** gives the real values of the key's own fakes, whatever call or conversation of the key
made them, last year's too. It reaches no other key's fakes and no person's: text that is not one of
the key's fakes is left as it is. A person's name comes back whole only: one of its parts alone (a
first name) may be an ordinary word, or another person's, and stays as it is. Every unmask is written
in the administration log before its answer is sent (`apps.unmasked`, with the token, how many texts
and how many values; never a text or a value), and a log that cannot take it refuses the call (503).
The key's first token, which may scan, mask and unmask, also serves agents through the gateway: their
answers are restored as any agent's are, with no administration log entry for each restore. A token
made with `keys add-token` never does, so each of its restores is an `apps.unmasked` entry. Give
`unmask` to the tools of the people who may see the real data, and read the log with
`latet server log --action apps.unmasked`.

**Mask reveals something too.** With a token that may mask and access to the masked logs, someone can
confirm a guess: mask `mario.rossi@acme.it` and search the logs for its fake. If it is there, Mario
Rossi is a customer. So give `mask` only to the programs that need it, keep the token where they keep
their secrets, and treat the masked logs as pseudonymous data, not anonymous data.

**Scan reveals something too.** It makes no fake and changes nothing, but it reports the values the
key's vault already knows, wherever they appear. A token that may only scan can therefore confirm that
a value no detector would find on its own (a name in a `data` text, a value a rule of the key found
before the rule was removed) is one of the key's. Give `scan` with the same care as `mask`.

**Forget reveals something too.** Its answer says how many of the values the key's vault held, so a
token that may forget can confirm that a value is one of the key's, as one that may scan can. Give
`forget` only to the process that handles erasure requests; each forget is in the administration log
(`apps.forgotten`, with the token and the counts, never a value).

## A key's vault

A key's vault keeps every value it has masked, with its fake, for as long as the key exists: an unmask
works on a log line of last year, and a customer keeps the same fake for years. Values do not expire: a
value leaves the vault when it is forgotten ([forgetting values](forget/)), and every value goes with
the key, as `latet server keys remove` deletes its space, and its vault with it. A value found once is
masked from then on wherever it appears, even where no detector would find it again: a customer's
address that a rule of the key once found, then removed, is still masked.

A log space can hold millions of values, so an instance does not hold a key's vault in memory. It
keeps the values the key used last (`key_working_values`, 5,000 by default, about 10 MiB a key) and
finds every other one in PostgreSQL as the texts of a call need it, with one lookup for all of them,
through an index of the words of the values and fakes. That index holds hashes made with a secret
key, never a word in clear.

**Secrets glued to other letters.** From PostgreSQL, a secret written as one run of letters and digits,
with no space or punctuation, is found by its word, and by its first and last six characters when
other letters or digits are glued to one side of it. Glued on both sides (`xhunter2y`), or glued at
all when the word at its edge has six characters or fewer, no word of the text finds it. So an
instance holds a key's short secrets in memory for good, those of one run of 16 characters or fewer
or with such a short word at an edge: the newest `key_held_secrets` of them (1,000 unless the server's
settings say otherwise), found however they are glued. An older one is found where nothing is glued
to it, or where one side is free and it has seven characters or more; a longer one, wherever one side
is free. Raise `key_held_secrets` for a key that sees many short secrets, such as a session token in
each log line: each one held takes about 2.2 KB on every instance, and every text the key masks or
unmasks is read for it, which slows its calls a little. Measured as below with a session token of its
own in every line (one more value to keep in each), a new key with 1,000 of them held masked 73 lines
a second and unmasked 1,935, against 75 and 2,103 with none held. A fake glued so that none of its words stands apart is not
found either; masking never makes one. As everywhere in Latet, a value is matched again from four
characters, a person's name from three ([known values](../concepts/detection/#known-values)).

**What a call that fails keeps.** A mask call that fails while it masks (a 507, a commit that does not
reach PostgreSQL) keeps none of its new values when it held one of the instance's transactions for
them, three at a time with the default `database_pool`: the key's space forgets them. When those were
taken, the call kept each new value as it went, and those stay in the key's vault. A failure after the
masking, in the local review or in rendering the answer, comes after the commit, so the call's new
values stay as well. Masking the texts again is safe either way: a value the vault kept has its fake,
and one it forgot gets a fake again. An agent of the same key that used a forgotten value through the
gateway meanwhile has already sent that value's fake, which no unmask restores later.

## Limits

- **The vault.** A key's vault holds the server's `vault_limit` values (200,000 unless the server's
  settings say otherwise), or a limit of its own: `latet server keys limit "Shop" 5000000` (from
  1,000 to 100,000,000; `default` gives it the server's again). A full vault refuses the mask call that
  would add a value with 507: nothing of the call is answered, nothing leaves in clear. The server's
  status lists the identities at 90% of their limit, and the metric `latet_vault_near_limit` counts
  them, so raise a limit before it is reached. Each instance counts a call's new values before it
  commits them: two instances, or a call and an agent of the same key, can pass the limit by one
  call's new values each before the next value is refused.
- **A call** takes at most 1,000 texts and the server's `apps_request_mb` (8 MiB unless its settings
  say otherwise, never more than its `max_request_mb`): a call waits in memory for the key's thread,
  and `latet-client` splits a longer list into calls of 8 MiB at most. Texts whose words name more than 100,000 values or fakes of the vault are refused
  with 422, nothing masked: send fewer texts in a call.
- **Calls at once.** A key has at most `requests_in_flight` application requests on an instance (16
  unless the settings say otherwise); past it, 429 with `Retry-After`.

## How fast it is

The calls of one key run one after the other on an instance, in its space's thread; calls of
different keys, and calls that reach different instances, run side by side. To go faster with one
key, run more instances behind the load balancer.

Measured on one instance (Intel i9-10850K, 64 GB, Windows 10, PostgreSQL in Docker Desktop on the same
computer), log lines of about 200 characters with an email address and a public IPv4 address each,
half of them naming a customer seen before, 500 lines a call as `data`; and documents of about 20 KB
of prose with ten values each, one a call:

| The key's vault | Mask, lines a second | Unmask, lines a second | A document, mask / unmask |
| --- | ---: | ---: | ---: |
| New, learning its values as it goes | 189 | 2,691 | 77 / 28 ms |
| 200,000 values | 440 | 1,691 | 87 / 30 ms |
| 1,000,000 values | 410 | 1,498 | 95 / 32 ms |

Mask is bound by the detectors: on one thread, with every value in memory and no database, the engine
masks about 900 such lines a second. A new key also writes each new value to PostgreSQL. An instance
keeps at most `database_pool` connections to PostgreSQL (10 unless the server's settings say
otherwise) for every identity's work, and lets three tenths of them (three of ten) be held by calls
that keep their new values in one transaction each; the others keep each value as it comes, which is
slower. Raise `database_pool` for an instance that masks for many new keys at once, and PostgreSQL's
`max_connections` with it ([server operations](../concepts/server-operations/#connections-budgets-and-trust)).
`scripts/bench_apps.py` in the repository measures a server of yours the same way.

## Upgrading a server

After an upgrade that changes how the words of a vault are indexed, the first instance that builds a
key's space makes them again before it answers the key's first call: about 40 seconds for 200,000
values and four and a half minutes for 1,000,000 on the computer above. A call that arrives meanwhile
waits, and an instance builds four spaces at once, so a few big keys delay the first calls of the
other keys on it. The changelog says when a release does this; then send each big key one call, with
a long enough timeout, before its programs start again.

While instances of two versions run side by side, a value an older instance writes is found by the
newer ones while they hold it in memory, but not in PostgreSQL until the key's space is built again.
When the applications API is in use, stop every older instance before the new ones start, even when
the tables stay the same ([upgrading](../concepts/server-operations/#upgrading-and-adding-instances)).
