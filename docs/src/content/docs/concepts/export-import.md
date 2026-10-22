---
title: Export and import
description: Your rules in a file for a colleague, and your whole installation in an encrypted file for another machine.
---

Two different things travel:

- **Your rules, for a colleague:** terms, recognizers, policies and the rest, in a YAML file.
  Imported, they add to the colleague's own.
- **Your installation, for another machine:** your settings and, if you ask, the vault with its
  key, in one encrypted file.

## Your rules, for a colleague

```sh
latet rules export acme-rules.yaml     # here
latet rules import acme-rules.yaml     # on your colleague's machine
```

Or **Export rules** and **Import rules** at the top of **Rules** in the dashboard: the import shows
what it would do before anything changes. The tables of *Terms*, *Recognizers* and *Allowed values*
have an *Export* and an *Import* of their own, for that list alone: the terms and the values as CSV,
the recognizers as a rules file. Such an export keeps the same guards: a recognizer whose examples
hold a value Latet has seen for real is left out, and a CSV cell a spreadsheet would run as a
formula is written after a quote. Such an import goes through the same preview, adds and never
replaces, and holds back what lowers protection in the same way; the *Recognizers* table takes a
file of recognizers alone.

**What the file holds:**
- the terms, recognizers, policies and field rules;
- the personal-data types, and the countries whose ID and phone numbers are recognised;
- the profile, private IP addresses, local name detection, strict data mode with the files and web
  addresses it covers, the dump guard, images and text recognition;
- the exceptions: values and patterns never to mask, secret rules turned off, the tools strict data
  mode covers;
- the local reviewer's [guidance](../llm-review/#your-guidance).

Never this machine's identity, port, project folders or servers.

A rules file holds at most 2 MB. Past 5,000 terms, 500 recognizers, 500 field rules, 5,000 values or
500 patterns never to mask, only the first are read, and the import says so. A file made by a newer
Latet is refused.

**The terms are values in clear.** A client's name among your terms is in the file as it is: send
the file the way you would send the names themselves. A recognizer whose examples hold values
Latet has seen for real is left out, since examples should be invented.

**An import adds protection, and that applies at once:**
- new terms, recognizers and field rules are added, and yours stay;
- a value stricter than yours is taken: a higher profile, `redact` for a type you mask;
- a recognizer with the name or the type of one of yours, or a rule for a field you have a rule
  for, is left out: yours is kept.

**What would lower protection is listed and left out:** the exceptions, a weaker value, the
reviewer's guidance, a recognizer that lets what it finds through (`policy: allow`), a field kept in
clear. `--all`, or *Apply these too* in the dashboard, applies them as well. Run by an agent or a
script, `--all` makes the whole import one change, which waits for your approval like any
[change that lowers protection](../agents-and-settings/): what adds protection waits with it, and
nothing of the import applies until you approve it. `--dry-run` only says what an import would do.

```text
$ latet rules import acme-rules.yaml
Would add:
  terms: Fonderia Brambilla, Banca Ligure
  recognizer badge-number
  policy for IBAN: redact (yours: mask)
Yours already, or kept as yours:
  recognizer ticket: yours, with the same name, is kept
Left out, it would lower protection:
  values never to mask: acme-dev
--all applies them too: they wait for your approval.
saved C:\Users\you\AppData\Roaming\latet\config.yaml
Rules reloaded.
```

**What applied is the daemon's answer.** The command first says what it would add, then saves
`config.yaml`, asks the daemon to reload it, and says what came of that: *Rules reloaded.* (exit
code `0`), a change that waits for your approval (`3`), or, when the daemon did not answer, that the
file is saved and whether it applies is unconfirmed (`1`: `latet reload` applies it once the daemon
runs). The command does not read the repositories Latet has seen with a `latet.yaml` of their own:
the daemon weighs the import in them when it reloads, and an addition that lowers protection in one
of them waits for your approval too.

A `config.yaml` or a project's `latet.yaml` can be imported too: what in it is not a rule (the
port, the identity...) is left out, and said.

### A project's rules

A project's shared rules travel with it already: its [`latet.yaml`](../projects/), which the
dashboard writes when you change them, is in the repository, and whoever clones it has them.

Your own settings for a project, such as client names you keep out of the repository, or its
exceptions, make a file of their own:

```sh
latet rules export shop-rules.yaml --project C:\dev\shop
latet rules import shop-rules.yaml --project D:\work\shop
```

In the dashboard, a project's **Rules** page has them too, under *Yours, for this project*:
*Export these settings* and *Import settings*.

### On a Latet server

On a computer connected to a [Latet server](../server/), your rules are in your space on the
server, and `latet rules export` and `import` refuse to run. **Export rules** and **Import rules**
at the top of **Rules** in your dashboard on the server work as above, on your own rules: a team
shares its rules as a file, and each person imports it. A project's `latet.yaml` is not applied
there: import it the same way to use its rules.

An administrator exports and imports an API key's rules:

```sh
latet server --server https://latet.acme.lan rules --key "Billing CI" export billing-rules.yaml
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

Rules set for everyone on a server, which nobody can lower, are planned for the Enterprise Edition.

## Your installation, for another machine

```sh
latet backup latet.backup --vault    # here
latet restore latet.backup           # on the other machine
```

`latet backup` asks for a passphrase twice (at least 12 characters: a few words are easy to
remember and hard to guess), and writes one file:

| | Without `--vault` | With `--vault` |
| --- | --- | --- |
| Your settings (`config.yaml`) | yes | yes |
| The certificates the local reviewer trusts | yes | yes |
| The vault and its root key | no | yes |
| The dashboard's history | no | with `--history` |

- **With the vault**, the other machine makes the same fakes as this one, remembers every value
  found so far (by the detectors, the local reviewer, you in the Live Reviewer), and keeps
  restoring conversations started here. The file then holds your real values, encrypted: keep it
  where you would keep them.
- **Without it**, the other machine makes fakes of its own.
- **Never in it:** the local reviewer's API key, the bridge token (between the Claude Code plugin and
  the daemon), the logs, the local name model, the text recognition models and your agents' settings.
  Each machine has its own.

`latet restore` reads and checks the whole file first, shows what it holds and what in its
settings lowers protection compared with yours, and asks you. Then:

- **This machine keeps its own:** its port, its identity (added to the backup's, so both are masked),
  and nothing of `capture_dir` or `api_key_file`, which are paths of the other machine.
- **Project folders in another place:** `--paths C:\Users\anna\dev=D:\work` moves the ones under the
  first folder under the second. Folders that are not here are listed.
- **A vault here already:** if this machine's vault has values of its own, it is replaced only with
  `--replace`. They are saved first in `before-restore-<date>.latet` next to the vault, with the
  same passphrase, so `latet restore` can bring them back.
- **The daemon** is stopped and started again around it, and nothing starts it while a restore is
  under way. A restore cut off (a crash, a power cut) leaves it stopped until `latet restore
  --abort` puts things back.
- **What this machine had** (the vault, the history, `config.yaml`) stays in a dated
  `before-restore-` folder next to the vault.

Then: `latet llm key` for the reviewer's API key, `latet ner install` for local name detection,
`latet ocr install` for the text in images (until then images are sent as they are, flagged *Not
inspected*, or not sent at all with `images: mask`), and `latet global-install` (or `latet claude`)
for your agents.

**At your terminal only.** Both commands refuse to run inside an agent: they handle your vault and
its key. `latet restore` also needs a terminal to ask you, and refuses when the root key comes
from `LATET_ROOT_KEY` or `LATET_ROOT_KEY_FILE`, which a restored key could not replace.

Without the passphrase nobody can read the file, you included. A file changed, cut, or put together
from pieces of others is refused before anything in it is used: see
[Security](../../security/#how-latet-protects-itself).

### In Docker

The vault and its key are in the `latet-data` volume, the settings in `latet-config`: copying
both volumes moves everything. With the commands:

```sh
docker exec -it latet latet backup /data/latet.backup --vault
docker cp latet:/data/latet.backup .
```

On the other machine, with its container stopped:

```sh
docker compose stop
docker cp latet.backup latet:/data/
docker compose run --rm -it latet latet restore /data/latet.backup
docker compose start
```

If the container takes its key from `LATET_ROOT_KEY_FILE`, `latet restore` refuses: move the
`latet-data` and `latet-config` volumes and the key file instead.
