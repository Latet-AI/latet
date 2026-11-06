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
the prints of the whole vault: a 64-bit fingerprint of each word of its values and fakes, made with a
secret key, about 15 MB for a million values. A call's words are looked up among the prints first: a
word with no print is no value's and no fake's, and PostgreSQL is not asked for it. The values and
fakes whose words have one are read from PostgreSQL, all of the call's at once, and each is decrypted
and compared with the text before it masks or restores anything, since two words can share a print.
Neither the prints nor PostgreSQL's index of the words hold a word in clear. Before each call the
instance reads the key's change counter in PostgreSQL and takes in what the other instances kept or
forgot since, so a value kept on one instance before a call starts is found by that call on any other,
even when the message that told of it was lost. A key with few values works the same way: only a
person's vault of 35,000 values or fewer is held whole in memory
([Server operations](../concepts/server-operations/)).

**Secrets glued to other letters.** From PostgreSQL, a secret written as one run of letters and digits, with no
space or punctuation, is found by its word, and by its first and last six characters when other letters or
digits are glued to it: on one side, at the edge of the text's run that holds it, and on both sides
(`xhunter2y`), inside that run, which a call reads six characters at a time. A call reads a million such
positions at most: past them, its later runs are read by their words and edges alone. A secret of six characters
or fewer, or one whose word at a glued edge has six characters or fewer, is found by no word of the text when it
is glued. So an instance holds a key's short secrets in memory for good, those of one run of 16 characters or
fewer or with such a short word at an edge: the newest `key_held_secrets` of them (1,000 unless the server's
settings say otherwise), found however they are glued, past a call's million positions too. An older one, or a
longer one, is found where nothing is glued to it, glued on one side when the word at that edge has seven
characters or more, and glued on both sides when it is one run of seven characters or more, within the call's
million positions. Raise `key_held_secrets` for a key that sees many short secrets, such as a session token in
each log line: each one held takes about 23 KB on every instance that serves the key, most of it in the detector
that finds it, and every text the key masks or unmasks is read for it, which slows its calls a little. Measured
with Latet 0.10.0 on the computer of [how fast it is](#how-fast-it-is), with a session token of its own in every
line (one more value to keep in each), a new key with 1,000 of them held masked 73 lines a second and unmasked
1,935, against 75 and 2,103 with none held. A fake is found glued as its secret is: it keeps the secret's signs
in place, so a fake glued so that none of its words stands apart is one of a secret held in memory, and is found
while the secret is held. As everywhere in Latet, a value is matched again from four characters, a person's name
from three ([known values](../concepts/detection/#known-values)).

**Short secrets.** A secret shorter than twelve characters (`password=1234`, a short token) gets a
longer fake of the same kinds, its signs in place: fifteen digits for a number (its sign and decimal
point kept, so that a JSON number stays one), twelve hexadecimal digits for a hexadecimal value,
otherwise twelve letters and digits. A fake as short as its value would turn up by chance inside the
ids and numbers of other log lines, and an unmask would change lines that never held the secret.

A short fake that a key's vault gave before Latet 0.10.2 (eight characters or fewer) stays, as every
fake does. Unmask, which reads every fake of the key's vault, puts it back only where it stands as a
whole word, with no letter, digit or underscore next to it. The cost: where such a secret's value
was masked inside a longer word or number (`postgres` in `postgresql://`, `1234` in
`order_id=9912343`), unmask leaves its fake there. A conversation of the key's first token, through
the gateway or the plugin, puts it back as before, inside a word too, as it restores only the fakes
the conversation was shown. While an instance of the server still runs 0.10.0, it gives new short
secrets short fakes, and those stay too: upgrade every instance.

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
  call's new values each before the next value is refused. An index of a vault's prints is built up to
  16,777,216 prints, about eight million values of the kind [how fast it is](#how-fast-it-is)
  measures: past that, no index is built while the key's space lasts, and its calls look their words
  up in PostgreSQL alone, more slowly.
- **A call** takes at most 1,000 texts and the server's `apps_request_mb` (8 MiB unless its settings
  say otherwise, never more than its `max_request_mb`): a call waits in memory for the key's thread,
  and `latet-client` splits a longer list into calls of 8 MiB at most. Texts whose words name more
  than 100,000 values or fakes of the vault are refused whole with 422, in fixed words that name no
  value: nothing of the call is masked or restored, so send fewer texts in a call. A 503 is another
  matter: a store of the server (PostgreSQL, Valkey) did not answer, and the same call can be sent
  again later ([HTTP reference](../reference/apps-api/)).
- **Calls at once.** A key has at most `requests_in_flight` application requests on an instance (16
  unless the settings say otherwise); past it, 429 with `Retry-After`.

## How fast it is

The calls of one key run one after the other on an instance, in its space's thread; calls of
different keys, and calls that reach different instances, run side by side. To go faster with one
key, run more instances behind the load balancer, or several workers on one Linux machine
([several instances on one machine](../concepts/server-operations/#several-instances-on-one-machine)).

Measured with Latet 0.11 on one instance (Intel i9-10850K, 64 GB, Windows 10, PostgreSQL 18 in Docker
Desktop on the same computer, the server's default settings): 40,000 log lines of about 200
characters with an email address and a public IPv4 address each, half of them naming a customer seen
before, in calls of 500 as `data`; then 50 documents of about 20 KB of prose with ten values each, one
a call. Lines a second are the lines over the time their calls took; a document's time is the median
of its 50 calls:

| The key's vault | Mask, lines a second | Unmask, lines a second | A document, mask / unmask |
| --- | ---: | ---: | ---: |
| New, learning its values as it goes | 191 | 2,894 | 126 / 31 ms |
| 200,000 values | 871 | 2,960 | 117 / 32 ms |
| 1,000,000 values | 773 | 2,967 | 124 / 32 ms |

Each document brings values new to the key, which it writes to PostgreSQL as it masks them: the same 50
documents masked again, their values known, took 16.6 ms at 200,000 values and 16.4 ms at 1,000,000 (unmask 23
ms at each). A new key writes most of its lines' values too, which is why it masks fewer.

Latet 0.10.0, measured the same way on the same computer, masked 350 and 304 lines a second at
200,000 and 1,000,000 values and 153 for a new key, unmasked 2,114 and 2,014, and masked a document in
163 to 182 ms. Latet 0.11 set out to mask at least 1,000 such lines a second on one instance at
200,000 and at 1,000,000 values, and a document in 30 ms or less: it masks 871 and 773, and a document
in 30 ms only when the key holds its values already. Most of a mask call's time goes to the
detectors, and some to waiting for PostgreSQL.

An instance finds a key's values through the prints of its vault's words ([a key's vault](#a-keys-vault)), which
take about 15 MB for a million values (14.77 MB at 1,000,000) and are built in the background as the key's space
opens: in 1.5 seconds at 1,000,000 values (the median of three openings, 1.42 to 1.62 s). Until they are ready,
the key's calls look their words up in PostgreSQL alone: in an earlier run of this measurement on Latet 0.11,
with the prints turned off, the key of 200,000 values masked 494 lines a second and unmasked 1,382. Each call
first reads the key's change counter, 0.51 ms at the median, and takes in what changed since: 1.87 ms for one
new value and 2.11 ms for 100, at the median too.

In a call of texts masked before, only the new ones are read again, but each of the others is still
looked up and checked against what the key masked: 500 texts masked before and one new took 168 ms,
the new one alone 6.1 ms (medians of three runs).

On one Linux machine, workers add up. Measured in Docker on the same computer (a Linux virtual
machine of 20 processors and 16 GB, PostgreSQL 18.6 and Valkey 9.1.2 in containers beside it), with
`latet server run --workers` and an API key of 200,000 values: 200,000 distinct lines of the kind
above in calls of 62, four connections a worker, each count for 20 seconds, the median of three
rounds; the memory is each worker's resident memory after the rounds:

| Workers | Mask, lines a second | Unmask, lines a second | Memory a worker |
| ---: | ---: | ---: | ---: |
| 1 | 1,190 | 1,947 | 227 MB |
| 2 | 2,260 | 3,711 | 227 MB |
| 4 | 4,253 | 6,594 | 216 MB |
| 8 | 7,477 | 10,147 | 185 MB |

Eight workers masked 6.28 times what one did and unmasked 5.21 times, where Latet 0.11 set out to
reach at least 5 times both; no round of eight workers unmasked at less than 5.07 times. The kernel
deals the connections to the workers by a hash, not in turn, so some workers get more of them than
others: from 1 to 10 of the 32 in a round of eight workers. The process that starts the workers took
47 to 48 MB beside them. These calls come from several connections at once, so compare the rows of
this table with each other, not with the first table.

Writing new values takes connections to PostgreSQL: an instance, and each worker, keeps at most
`database_pool` of them (10 unless the server's settings say otherwise) for every identity's work,
and lets three tenths of them (three of ten) be held by calls that keep their new values in one
transaction each; the others keep each value as it comes, which is slower. Raise `database_pool` for
an instance that masks for many new keys at once, and PostgreSQL's `max_connections` with it
([server operations](../concepts/server-operations/#connections-budgets-and-trust)).
`scripts/bench_apps.py` in the repository measures a server of yours as the first table was measured.

## Upgrading a server

Latet 0.11 changes the tables. Stop every instance and every worker, on every machine, run
`latet server migrate` once with 0.11, then start them all with it
([upgrading](../concepts/server-operations/#upgrading-and-adding-instances)). An instance does not
start on tables of another version, so no call is answered between the stop and the start, and
`latet server migrate` changes nothing while an instance is alive, one of 0.10 for 30 seconds after
it stops. The migration adds to each word row of every vault an empty place for its print and for the
count of the change that wrote it last, with their indexes: on a database of large vaults it takes
longer than earlier migrations did, and the prints themselves are made later, in the background.

A vault kept by Latet 0.10 has no prints yet. After the restart, the first instance that builds a key's space
answers the key's calls at once, looking their words up in PostgreSQL as 0.10 did, and makes the vault's prints
in the background, once, one instance at a time; each instance then builds its own index of them and uses it.
Until then the key's calls mask the same values, more slowly ([how fast it is](#how-fast-it-is): 494 lines a
second at 200,000 values with the prints turned off, in an earlier run), and none is refused for it. While an
instance makes a vault's prints, the other keys and people whose spaces it opens meanwhile may also be served
from PostgreSQL's lookup until their index is built. When the
prints are not all made or an index cannot be built (PostgreSQL or Valkey not answering, a catch-up that
failed), the calls stay on PostgreSQL's lookup until the index is built again: at once when another instance has
made the prints, otherwise 10, 20 and 30 seconds after each try and then once a minute. The log says once when
an index stops being used and once when it is used again. A vault past the size an index is built for
([limits](#limits)) stays on PostgreSQL's lookup while its space lasts. The words themselves stay as 0.10 made
them: 0.11 does not make them again.

A release that changes how the words of a vault are made (the changelog says so) makes a key's words
again in the first instance that builds its space, before that space answers the key's first call:
Latet 0.10.0 took about 40 seconds for 200,000 values and four and a half minutes for 1,000,000 on
the computer above. A call that arrives meanwhile waits, and an instance builds four spaces at once,
so a few big keys delay the first calls of the other keys on it: after such a release, send each big
key one call, with a long enough timeout, before its programs start again.

While instances of two versions run side by side, which a release that keeps the tables allows, a
value an older instance writes is found by the newer ones while they hold it in memory, but not in
PostgreSQL until the key's space is built again. When the applications API is in use, stop every
older instance before the new ones start, even when the tables stay the same.
