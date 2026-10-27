---
title: Forgetting values
description: Take a person's values out of an API key's vault when they ask that their data leave it.
sidebar:
  label: Forgetting values
---

An API key's vault keeps every value it has masked, with its fake, for as long as the key exists: an
unmask works on a log line of last year. When a person asks that their data leave your logs or your
RAG index, the server forgets their values. Each value is taken out of the key's vault, its fake
restores nothing from then on, and the value gets a new fake if a text holds it again.

There are two ways: an administrator runs `latet server keys forget`, or an application calls the
applications API with a token that has the `forget` permission. Both do the same thing, and both are
written in the administration log with counts, never a value.

## The administrator

Put the values in a file, one a line, in UTF-8:

```text
mario.rossi@acme.it
+39 347 123 4567
Mario Rossi
```

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

The command says how many values it read, asks before it sends them, and then says how many the
key's vault held and how many it did not. Without `--file`, it asks you to type the values in your
terminal, one a line, and an empty line ends them. It never takes a value on the command line, where
the shell would keep it in its history. It refuses to run inside an AI agent, since the values are a
person's data, and in a script it needs `--yes`. It sends the values in calls of 1,000 at most. Each
call is a `key.forgotten` entry of the administration log, with how many values were asked, forgotten
and not in the vault. Delete the file when you are done: it holds the values.

A key's first forget after an upgrade first indexes what the key's local reviewer remembered and its
history kept before the upgrade: on a key with much history that takes minutes. The command waits up
to five minutes for each call, and an application's call can take as long. If a call stops waiting,
run it again: what the server forgot stays forgotten, and is then counted as not in the vault.

It works whether the key is enabled or disabled. A person's own vault is not forgotten this way:
people forget values in their dashboard (Vault).

## An application

Give the application a token of its own with `forget`. No other permission brings it, and the key's
first token, which may scan, mask and unmask, cannot forget:

```sh
latet server --server https://latet.acme.lan keys add-token "Shop" erasure --forget --out erasure.token
```

The application sends up to 1,000 values a call:

```sh
printf 'Authorization: Bearer %s' "$(cat erasure.token)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"values": ["mario.rossi@acme.it", "+39 347 123 4567"]}' \
  https://latet.acme.lan/_latet/apps/v1/forget
```

```json
{"forgotten": 1, "unknown": 1}
```

`forgotten` is how many values the key's vault held, `unknown` how many it did not, each value
counted once. Every call is an `apps.forgotten` entry of the administration log (the token, how many
values asked, forgotten and not held), written in the same transaction as the forget: a log that
cannot take it forgets nothing, and the call answers 503. With the Python client,
`latet.forget([...])` sends a longer list in several calls and returns `ForgetResult(forgotten,
unknown)`. A token without the permission gets 403.

## What it removes

- **Each value as it is written.** Another case, other spaces or another written form
  (`mario.rossi%40acme.it` in a URL) is another value. The vault keeps each form it met as an entry
  of its own, so list every form that should go.
- **Everything in the vault that names it:** its entry and its fake, sealed with the key's root key,
  the hashes the index keeps of their words, and the local reviewer's remembered answers that found
  the value, whether the vault held it or not. Then every instance drops it from its memory, and the
  texts it keeps masked with its fake, once the calls already running there end.
- **What the key's agents left in Latet.** When the key's first token serves agents through the
  gateway, the value's entry also leaves their conversations, and the key's history loses the texts,
  the model's answers and the local reviewer's findings that named it, with its old fake. An agent
  that sends an earlier message again gets it masked anew, with the new fake.
- **Its old fake restores nothing, and no other value ever gets it.** A log line masked before keeps
  the old fake, and unmask leaves it as it is. The fake is retired: the vault keeps its keyed hash
  alone, tied to no entry, for as long as the vault, so it is never drawn again for another value and
  an old line is never unmasked to someone else's. A retired fake takes no room, since the vault's
  limit counts values, but it stays: the server's status and `latet_vault_near_limit` weigh a key's
  retired fakes with its values, so a token that masks and forgets in a loop shows there. Give
  `forget` only to the process that handles erasure requests.
- **The value gets a new fake if it comes back.** Fakes and entries are drawn from the value, so
  after a forget the key's vault draws them in a new generation: the value gets a new fake and a new
  entry, and the old fake never comes back. The masked texts that left Latet before cannot be tied
  to the new ones by their fakes.
- **An address keeps its company's domain apart.** An address at a company's domain
  (`mario.rossi@acme.it`) keeps the domain as a value of its own, and the fake's domain is that
  domain's fake. Forgetting the address leaves the domain, so the domain part of the old fake still
  restores to `acme.it`; forget `acme.it` too if it should go. A mail provider's domain, such as
  `gmail.com`, is never kept.

What stays:

- the counts in the administration log, and the history's exchanges whose texts went, without those
  texts;
- the masked texts that already left Latet, in your log service or your index, which hold the old
  fake and which you delete there if they should go;
- in Valkey, sealed, for a week after their last use: the texts of the key's agents' conversations as
  they left and the model's answers as it wrote them, which hold the old fake, never the value. An
  earlier message sent again is masked anew; an answer of the model goes back to it as it wrote it;
- for a week after a conversation's last use at most, the old entry's id in that conversation of the
  key's agents (a keyed hash of the value, never the value), when Valkey did not answer right after
  the forget, or when an instance missed the message that tells of it (Valkey keeps no message) and
  wrote the id back from a conversation it holds;
- what PostgreSQL itself keeps of deleted rows, sealed, in its files until it reuses their space, in
  its write-ahead log and in the database's backups.

A call that runs while the forget is made can still use the old fake, and what it keeps at that
moment can still name the value: an answer of the local reviewer, which a second forget a moment
later takes, and an exchange the history records, which stays for the history's retention
(`retention_days`). The instances drop the value once the calls already running there end.

## Who can learn what

The answer says how many values the vault held, so a token that may forget learns whether a value is
one of the key's, as one that may scan or mask can
([who can learn what](../#who-can-learn-what)). Give `forget` only to the process that handles
erasure requests, and read the log with `latet server log --action apps.forgotten`.
