---
title: Production data
description: Masking the results of queries, API answers and logs an agent reads, every value or field by field.
---

An agent looking into a bug often reads real data: a query on the production database, the answer
of a REST API, a log, a page in the browser. Latet's detectors find what has a recognizable shape
there, such as e-mail addresses, IBANs, tokens and ID numbers. A note, a salary or a contract
clause have no shape: in a table they would leave as they are.

Latet reads those outputs by their structure. A tool's output is often a table, a JSON document
or lines of `key=value` pairs, and the name of each column or key says what its values are. Three
things use that:

| | When | What it masks |
| --- | --- | --- |
| **Secret columns** | always | the values of columns and keys named like a secret: `password_hash`, `api_key`, `reset_token` |
| **Field rules** | when you write them | the values of the fields you name, as a type of yours |
| **Strict data mode** | with the `max` profile, or when you turn it on | every other value of the structure |

## The structures Latet reads

- **Tables:** psql, CSV, TSV and `;`-separated, markdown, HTML, and the tables of a browser's
  accessibility snapshot (`columnheader`, `cell`), and the tables drawn with box characters (Node's
  `console.table`, psql's unicode style).
- **`key=value` pairs:** two or more on a line, as in a log.
- **JSON:** a whole document value by value, each with its path (`items[].sku`); a `"key": value`
  fragment inside other text, such as a log line, by its key.

## Secret columns

They are always on. A CSV of users:

```text
id,email,api_key,password_hash,reset_token,role
7,ops@acme-intra.com,ak_live_9fK2LmQx7Rt3vW8zB1nY5pD,$2b$12$Kq3v0rZQ8xGm1o5t,8f14e45fceea167a,admin
```

leaves as:

```text
id,email,api_key,password_hash,reset_token,role
7,oliver@consolidated-e55f.example,ak_live_3zF1ObRs6Wa1eH2mU6tO2fM,p5cp22yIa9h4bDH9pBx9p8n,a03c15f272977c30,admin
```

The key, the hash and the token are secrets because of their column, the e-mail address because
of its shape; `admin` is left alone. A secret column stays masked even when a field rule keeps
its field in clear.

## Strict data mode

The [`max` profile](../profiles/) turns it on. With the other profiles, or to say it outright:

```yaml
data: strict
```

`data: normal` turns it off with `max` too; leave `data` out and the profile decides. Turning it off
where it is on lowers protection: from an agent or the command line, it waits for your approval.

Every value of a table, a JSON document or `key=value` pairs in a tool's output is masked; the
names of columns and keys stay, except a key that is itself a value, such as an email address: it is
masked as values are everywhere. A query result:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | SHIPPED | 1250.5 | call back on +44 20 7946 0958
```

leaves as:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | QXWRLTE | 2939.3 | wkqp xoaj zd +44 20 3318 4452
```

- **The same value always gets the same fake.** Counts, groups and joins still work on what the
  model reads.
- **A query written with a fake runs with the real value.** The model writes
  `WHERE status = 'QXWRLTE'`; the tool runs `WHERE status = 'SHIPPED'`.
- **A fake keeps the value's shape.** A number stays a number of as many digits, an ISO date a valid
  date (within two years of the real one), a time a valid time. When a shape has no fake left, as
  with hundreds of three-digit ids in one output, the fake is a character longer.
- **A link stays a link.** A web address keeps its scheme (`https://`, `s3://`...); the rest of it is
  masked.
- **What names a value or says how a page looks stays in clear:** a placeholder of a template or a
  shell (`${from}`, `{{ subject }}`, `$MAIL_FROM`), and the `class` and `style` of markup a command
  prints, unless the style loads an address. A row of a table that does not fit its columns is masked
  whole.
- **A finding inside a value keeps its own type.** The phone number in the note gets a fake phone
  number; the rest of the note is masked as the note.
- **A value stays in its structure.** `SHIPPED` is masked in the table, never in your code: strict
  data mode does not add its values to the ones masked everywhere.
- **Values of one or two characters stay in clear,** such as `17` above: they say little, and a fake
  that short could not be told apart in an answer. Empty values and words that stand for none or for
  yes and no, such as `null`, `n/a`, `true` and `false`, stay in clear too.
- **A value longer than 16,384 characters is redacted,** not replaced with a fake: the model gets
  `[REDACTED:DATA]`, and nothing is put back.
- **Files are left alone.** The output of the tools that read files (Claude Code's `Read`, `Grep`,
  `Glob`, `LS`...) is code and documents, not data, unless a rule names the file
  ([files that count as data](#files-that-count-as-data)). The other detectors still apply to it.
- **So are web results.** Search results and pages (`WebSearch`, `WebFetch`) are public content,
  not your data, and they come as JSON: strict data mode would turn every title and address into
  random letters, leaving the model nothing to read or cite. The other detectors still apply to
  them, so a key or an email address in a page is masked as anywhere else. To cover a web page
  anyway, name its address in `data_hosts`
  ([web addresses that count as data](#web-addresses-that-count-as-data)). To cover a web tool, add
  it to `data_tools` together with every other tool strict data mode should cover: once `data_tools`
  is set, strict data mode covers only the tools it names, and `"*"` names every tool but the ones
  that read files.

### Which tools

Strict data mode covers the output of every tool, except the ones that read files and the web
tools. To cover only some, name them, with `*` for any characters:

```yaml
data: strict
data_tools: [mcp__postgres__*, Bash]
```

A tool of an MCP server is named after the server: `mcp__crm__*` names every tool of the server
`crm`.

A command you run with `!` in Claude Code counts as the `Bash` tool's output.

Codex reads files with shell commands, so in Codex strict data mode covers the files it reads too.
Name its database tool in `data_tools` to cover only that.

### Files that count as data

```yaml
data: strict
data_files: ["exports/**", "data/*.csv", ".env*"]
```

A file a pattern names is data whether strict data mode is on or off: naming it is enough. A file the agent reads whose path matches a pattern counts as data: every value in it is masked, as
in a tool's output. A pattern without a slash names a file by its name (`*.json`, `.env*`), one with
a slash the end of its path (`exports/**`, `**/*.csv`); `*` matches any characters, and case does not
matter. The values are found by the file's format:

- **JSON** (`.json`, `.jsonl`, or a file of another name that holds JSON): every string and number,
  by the path of its key, as in a tool's JSON output. The keys stay.
- **Tables** (`.csv`, `.tsv`): every cell, by its column.
- **`.env`, properties and ini files:** the value of each `KEY=value` line.
- **YAML:** every value, by its path (`servers[].host`); the keys and the structure stay.
- **Any other file:** the tables, `key=value` pairs and JSON fragments in it.

Each value is replaced where it is: the file keeps its lines, their numbers and its keys, so the
agent can still edit it by the text it read, and an edit written with a fake writes the real value.
Only the tools that return a file's text make it data (Claude Code's `Read` and `NotebookRead`): a
search's lines (`Grep`) and a listing (`Glob`, `LS`) stay as they are. When Claude Code's `Edit`,
`MultiEdit` or `Write` changes a file a pattern names, the lines it shows of the file, which the agent
may never have read, are masked as the file is, even when `data_tools` names other tools.

- **A broad pattern covers more than data.** `*.json` also covers `package.json` and `tsconfig.json`,
  whose values would then be masked everywhere in the conversation: name the folders that hold data
  (`exports/**`).
- **Markdown counts only when a pattern names it** (`*.md`, `docs/**/*.md`): agents read their
  instructions and documents in markdown, and a pattern for every file (`*`) leaves them alone. In a
  markdown file a pattern names, only its tables are masked, never its prose.
- **PDF files are not covered:** Latet does not read the text of a PDF. `images: mask` or `block`
  keeps documents from being sent at all ([images](../images/)).

### Web addresses that count as data

```yaml
data: strict
data_hosts: [api.internal.example.com, "*.salesforce.com"]
```

An address a pattern names is data whether strict data mode is on or off. A page a web tool (`WebFetch`) fetches from a host that matches a pattern counts as data, as a tool's
output does: every value of its tables, JSON and `key=value` pairs is masked. `*` matches any
characters; case and the port do not matter. A pattern for every subdomain also names the domain
itself: `*.salesforce.com` names `salesforce.com` too. A host written with a final dot
(`example.com.`) is the same host as without it. A search (`WebSearch`) has no address of its own.
To cover searches, add `WebSearch` to `data_tools` with the other tools you want covered, or write
`data_tools: ["*"]` for every tool but the ones that read files: a `data_tools` that names
`WebSearch` alone covers searches only, and leaves your database and shell tools out. With strict
data mode on, setting `data_tools` where it was empty narrows it, so it waits for your approval.

Adding a pattern or an address applies at once; removing one covers less, which lowers protection:
from an agent or the command line, it waits for your approval. In the dashboard they are set in
**Rules** → **Protection** → **Production data**, beside the tools.

### In an exchange

Strict data mode is set in **Rules** → **Protection** → **Production data**, or in your settings for
a project; an exchange has no switch for it. While it is off for an exchange (in the exchange's
project, or in your global rules for an exchange of no project), the dashboard asks Latet whether
one of the request's ten most recent tool outputs holds a table, JSON or `key=value` pairs, read as
for [choosing the columns to keep in clear](#choosing-the-columns-to-keep-in-clear), and comes from
a tool strict data mode covers ([which tools](#which-tools): not one that reads files, nor a web
tool, unless a rule names the file, the address or the tool). When one does, a line above the request's blocks says so, with a
link to where strict data mode is set for that exchange: your settings for its project when they set
`data`, else **Rules** → **Protection** → **Production data**. Latet reads the outputs from the
texts the history keeps: with `history: metadata` or `off` there is no line.

## Field rules

A field rule names a column, a JSON key or a JSON path (`items[].sku`); `*` matches any characters,
and case does not matter. Every other character is taken as written, `?` and brackets included, so
`orders[].items[].sku` names that path. Its values are masked as the type you give, with strict data
mode or without:

```yaml
fields:
  - {name: note_*, type: NOTE}
  - {name: ral, type: SALARY}
  - {name: iban_fornitore, type: IBAN, secret: true}
```

- `secret: true` makes its values secrets: one that reaches a request in clear blocks it.
- `mask: false` keeps a field in clear in strict data mode. It belongs in your own settings, not in
  a project's `latet.yaml`, which can only add protection.

For a field, your first rule that matches it (your settings for a project first, then your global
rules) and the first of the project's `latet.yaml` that matches it are weighed: the stricter applies
(redacted, then masked, then in clear), yours when they are as strict.

## Choosing the columns to keep in clear

Strict data mode masks every value, so the model cannot read a state or a date either: `SHIPPED`
becomes `QXWRLTE`, and an agent asked which orders shipped late has nothing to go on. Most tables
have columns that say nothing about a person, and the dashboard proposes them.

In an exchange, under a tool's output strict data mode masked, open *Choose the columns to keep in
clear*. Each column of the output comes with a proposal and its reason:

| Proposal | For |
| --- | --- |
| **Keep in clear** | a name that says ids or codes, dates of events (`created_at`, `order_date`), states (`status`, `state`), yes or no (`active`), counts (`qty`) |
| **Keep masked** | personal data or a secret found in its values; a name about people (`customer`, `created_by`, `doctor`), secrets (`api_key`), health, beliefs or private life (`hiv_status`, `marital_status`); free text; dates of birth or death |
| **Your call** | amounts; dates, yes or no, or a few values that repeat, when the name does not say what they are; the columns nothing tells about |

A column is proposed in clear only when its name says what it holds and nothing in its values is
personal data or a secret: the same detectors check them as everything else. The shape of the values
alone never puts a column in clear, since usernames repeat as states do. The proposals are a start:
look at the values shown beside each column before you apply.

The columns proposed in clear come checked. *Keep 2 columns in clear* adds a rule for each, such as
`{name: status, mask: false}`, at the top of your settings for the project (*This project*) or of your
global rules (*Everywhere*): of your rules, the first that matches wins, so a broader rule of yours
such as `note_*` does not mask it again; a rule of the project's `latet.yaml` that masks the column
still holds there, whatever you choose. The next outputs show those columns as they are. Nothing
applies until you click.

- **Worked out when you open the list.** The columns are read from the output the history keeps,
  on your machine, and nothing new is stored. With `history: metadata` or `off` there is no list.
- **The local reviewer's opinion, if you ask.** With the [local reviewer](../llm-review/) on, *Ask
  the local reviewer* sends it the column names and a few values of each, and shows its opinion
  beside the proposals. It is never applied by itself.
- **A project's own rules hold.** A column your settings for a project mask stays masked there
  whatever *Everywhere* adds: the list says so, and *This project* changes it. A column the
  project's `latet.yaml` masks stays masked there whatever you choose, and the list says which.
- **After a restart,** a project's settings load at its next session: until then the choice can go
  only in your global rules.
- **Codex and OpenCode** send the whole conversation with each request, and Latet masks it again
  each time: from the next request, the outputs already in it also go with those columns in clear.
  In Claude Code an output stays as the plugin masked it when it arrived.

For a single column, click one of its values: the inspector offers *Don't mask `status` in this
project*, and says when a column's name looks like an id, a date, a state or a count.

## The dump guard

```yaml
dump_guard: 50
```

A tool output holding more personal values than this (names, e-mail addresses, phone numbers, ID
numbers...) is withheld: the model receives a short note instead, telling it to ask for fewer rows
or for counts. The dashboard marks the exchange *Withheld*. Not set, or 0: no limit.

In the `max` profile, with the [local name model](../../getting-started/installation/) installed, a
tool output is also read for names and addresses: the texts and keys of a JSON output are read at
once, and one the model cannot read within five seconds is withheld the same way, with a note
telling the model to ask for fewer rows or fields. Nothing it would have found there leaves in clear.

## The browser

An agent driving a browser reads pages as tool outputs: accessibility snapshots, HTML, the results
of scripts, network responses. Strict data mode applies to them as to the rest, tables included.

Its screenshots are images: with [text recognition](../images/) installed, the text in them is
read and masked, and each value is covered with its fake. To send only the images Latet read and
masked in a project, or none:

```yaml
images: mask    # or block: no image or PDF is sent
```

## In a project

These keys work in [config.yaml](../../reference/configuration/), in a project's
[`latet.yaml`](../projects/) and in your own settings for a project. The project file can only
add protection:

| Key | In `latet.yaml` | In your settings for the project |
| --- | --- | --- |
| `data` | only `strict` | `strict` or `normal` |
| `data_tools` | ignored | added to yours |
| `data_files` | added | added to yours |
| `data_hosts` | added | added to yours |
| `fields` | only rules that mask, of a type masked there | any rule, `mask: false` included |
| `dump_guard` | only lower | any value; 0 turns it off |
| `images` | only fewer: `mask`, then `block` | `allow`, `mask` or `block` |
| `ocr` | only `auto` | `auto` or `off` |

A project whose profile is `max`, in its file or in your settings for it, has strict data mode on
unless `data` says otherwise there or in your global rules.
