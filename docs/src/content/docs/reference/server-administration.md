---
title: Server administration
description: Administer a server from your own computer and keep its credentials private.
---

Run `latet server` in your own terminal. Commands that change administration refuse an agent;
`config get` and `rules export`, to the terminal or to a file, also refuse an agent because they can
print values in clear (an API key's terms, the reviewer's guidance).
Status, identity lists and the administration log contain no credentials.

Name the server and give its token at the hidden prompt:

```sh
latet server --server https://latet.acme.lan status
```

Or use `--admin-token-file FILE`: a private UTF-8 file holding the token alone, or a
`{"url": "https://latet.acme.lan", "token": "<administration token>"}` record.
A record is accepted only for its server. On POSIX use `chmod 600 FILE` (or `0400` for the
account running a container), in a folder other users cannot write. On Windows the file and
its folder must allow only your account, SYSTEM and Administrators; follow the `icacls` fix
the command prints. `--admin-token-file -` reads one line from standard input.

On a computer where agents run, keep administration tokens out of shell profiles and the
account's environment: processes of the same user can read another process's environment.
`LATET_SERVER_ADMIN_TOKEN` is for a CI or container secret store, paired with
`LATET_SERVER_URL`; it is never sent to a different server.

`keys add`, `keys rotate` and `admin-token issue` accept `--out FILE`: the new private
file receives the secret and the command prints only its id and file. Never put a code, key or
administration token in a ticket, shared channel or agent conversation. A person's one-time
code lets whoever uses it connect a computer as that person and open their dashboard; issuing it is recorded.

Use `admin-token issue --label "Daniel's laptop" --days 30` for a token with an end date.
A `--never` token needs a label and can do everything an administrator can until revoked:
people and their codes, API keys, shared settings and administration tokens. A token issues others
that end no later than itself, so only a token that never expires can issue another that never
expires; on the server's machine, `latet server init --new-admin-token --never` makes one. Keep this
token apart, used only to issue others. A CI holding one gives that authority, for good, to whoever can
change its jobs; give CI a token with `--days` and issue it again before it ends.

Removals ask at your terminal even with `--yes`; scripts need `--yes`.
`config reset` replaces only a shared settings row the server cannot open, after confirmation.
`config get` and `rules export` can print values in clear: keep their output private.
`log verify` prints the chain's head; keep a copy elsewhere and compare it next time:
removing entries from the end leaves the remaining chain whole.

## Who administers, and losing a token

**The administrator is whoever holds an administration token.** There is no administrator account:
`latet server init` prints the first token to whoever initializes the server, and each token can issue
others. A token is not a person: the administration log names the token that did each thing
(`admin:a_3fk7…`), and an administrator needs no space of their own on the server, though they can be
added as a person like anyone else. In the Community Edition every token can do everything;
roles, such as a read-only auditor, are planned for the Enterprise Edition.

A token issued without `--days` or `--never` ends after `admin_token_days` (90 days unless the
server's settings say otherwise), or earlier when the token that issued it ends first; one with
`--days` ends after those days. Such a token, forgotten, stops working by itself. A `--never` token
works until it is revoked: list the tokens now and then with `admin-token list`, and revoke the ones
nobody uses.

**A token lost, with another one left:** from any computer, list them, revoke the lost one and issue
a new one:

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Without `--days`, the new token lasts the server's default, cut at the end of the token that issues
it. A token with `--days` cannot end after the token that issues it: the server refuses it, and
`admin-token list` shows when each token ends.

**No token left**, all lost or past their end: where the server's settings, its master key and its
database can be reached, `latet server init --new-admin-token` issues a new one (with `--never`, a
spare kept apart). In the Compose deployment, run the initialization's `docker run` from
[Server operations](../../concepts/server-operations/) again, ending in
`server init --new-admin-token`, with Docker's logging off as before. Revoke the lost ones
afterwards, if they have not ended. People, API keys, settings, vaults and history are unchanged
either way.

**A token that leaked**: revoke it at once from another token, then read what it did:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Losing the **master key** is worse than losing a token: it seals every space's key, and without it
no vault can be read again. Keep a copy apart from the database's backups, as
[Server operations](../../concepts/server-operations/) says.

## Reading the administration log

`latet server log --limit 100000 --json` reads successive pages, newest timestamp first,
then highest id when timestamps match. `--limit` accepts at most 1,000,000 entries. Each
request asks for at most 10,000; a shorter page can still have a continuation because pages
also have a byte limit. JSON output contains only entries, one per line; notices go to stderr.

`--since` includes its boundary and `--until` excludes it. `--actor` selects one identity or
administration token; `--action` selects an action, or a literal prefix ending in `*`.
Actions name what changed (`person.added`, `code.issued`, `token.revoked`, `key.rotated`,
`settings.changed`, `shared.changed`, `admin-token.issued`, `admin-token.revoked`), and reads are
recorded under the call's name (`status.read`, `log.read`): `--action 'admin-token.*'` selects every
change of the administration tokens.
To resume, pass the printed log cursor with `--cursor` and exactly the same original filters.
Use absolute dates or exact epoch seconds when you need to resume later: a relative time such
as `7d` changes between invocations. Never replace `--until` with a rounded time from the
plain output. A cursor is a position, not a credential or proof that the log is unchanged.

Each invocation is bounded by 1,000 page requests, 128 MiB of received bodies including retry
responses, 128 MiB of rendered UTF-8 output, and 300 seconds of retrieval and processing.
Escaped JSON can be larger than the received body. Pages are validated and rendered before
printing; a page that cannot fit is not partly printed. These time checks cannot interrupt
an operating-system output pipe that is blocked.

A budget stop, invalid page or server failure exits nonzero, reports how many entries were
printed and says that retrieval is incomplete. A validated resume cursor, when available,
points after the last printed page. If the first page exceeds the output budget, request
fewer rows. A server refusal such as `log_entry_too_large` does not skip the offending row.
Reaching your requested row limit is a normal stop; the notice says when a continuation remains.

The first page fixes an insertion ceiling: later entries, even with earlier timestamps, do
not join that traversal. Retention or deletion can still remove unread entries. Exhaustion
only means that no more matching rows were visible to those reads; it is not an immutable
export or a chain check. Use `log verify` separately, retaining its missing-tail limitation above.

## Unread image metrics

`latet_ocr_unread_total{reason}` counts terminal unread OCR image occurrences on each
server instance, both when a checked request is refused and when a rewritten image is
withheld. Its only label is `reason`, with five normal values:

| Reason | Meaning |
| --- | --- |
| `busy` | The identity, reader or pool could not admit the image within its wait or queue bound. |
| `too_long` | Reading exceeded its time budget. |
| `too_many_tiles` | The image needed more tiles than OCR permits. |
| `too_wide` | A text line exceeded the recognizer's width bound. |
| `unavailable` | OCR was loading, failed to load, or encountered another handled execution failure. |

A later occurrence of the same original image counts again when it receives a cached
OCR failure. The counter measures unsuccessful image occurrences, not model calls.
Internal retries, tiles, batches and reader repairs without a submitted image add
nothing. Successful OCR, manual withholding, decoding or pixel-policy failures,
OCR off or uninstalled, exact replay of Latet's own painted image or placeholder,
and a computer's local OCR are excluded. Final checks of the same work do not count
again. No label contains image content, a path or an identity.
