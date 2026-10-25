---
title: Detection
description: How Latet recognizes secrets and personal data, entirely on your machine.
---

Detection runs on your machine, or on the Latet server your computer is connected to: no cloud
detection service is ever called. Several
detectors read every piece of text a request carries. This page explains them with examples; the
[detectors reference](../../reference/detectors/) lists every detector and every secret rule.

## Secrets

Secrets are masked in every [profile](../profiles/).

Latet's own credentials are included: `LATET_PERSONAL_TOKEN` (`latet_pat_`), `LATET_COMPUTER_KEY`
(`latet_ck_`), `LATET_API_KEY` (`latet_key_`) and `LATET_ADMIN_TOKEN` (`latet_adm_`), each
prefix followed by 43 URL-safe
characters (`A-Z`, `a-z`, `0-9`, `_`, `-`). In `https://<host>/u/<route-key>`, the 16 to 128
URL-safe characters of the route key are masked as `LATET_ROUTE_KEY`; the host and the other
path segments stay. This applies on your computer and in a server space, including environment
dumps, Claude Code's `settings.json` and Codex's `config.toml`. Local tool calls get the real
values back. An outbound tool call containing a fake is stopped where Latet can intercept it
(see [Protection scope](../protection-scope/)).

- **Known formats.** The default ruleset of [gitleaks](https://github.com/gitleaks/gitleaks)
  (222 rules) is vendored and runs on [RE2](https://github.com/google/re2), with the keywords,
  entropy thresholds and allowlists gitleaks uses. It covers the API keys and tokens of many
  providers, such as GitHub, Anthropic, OpenAI, AWS, Stripe or Slack.
- **Values assigned to sensitive names**, in `.env` files, configuration and code. The name
  decides, whatever the value looks like: `DB_PASSWORD=...`, `"api_key": "..."` and
  `$env:TOKEN = ...` are masked; `APP_NAME=shop` and `DEBUG=true` are not. Placeholders such as
  `changeme`, `${DB_PASSWORD}` or `<your-key>` are left alone, and so are references in code such
  as `process.env.API_KEY`.
- **Private keys**: the whole body of a PEM, OpenSSH or PGP private key block. The `BEGIN` and
  `END` lines stay visible, so the model still knows a key is there.
- **Credentials**: the password in `postgres://user:password@host/db` and other URLs, JSON Web
  Tokens, and `Bearer` or `Basic` credentials. In a connection string (a `connectionString`
  variable, a `connection_string` column) only the credentials are masked: the values of
  `Password=`, `PWD=`, `AccountKey=` and the like, or the password of its URL. The server, the user
  and the database stay, as in `Server=db;User Id=app;Password=…;`.

For example, a `.env` file as it is on your disk:

```sh title=".env"
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=hunter2hunter
PAYMENTS_API_KEY=pk_9f2c4e7a1b3d5f8e0a6c
```

and what the model receives when the agent reads it:

```sh
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=jeeepo3ixhbkw
PAYMENTS_API_KEY=pk_3a8d1f6b0e9c2a7d4b5e
```

The host name is not a secret and stays. The password and the key are replaced by fakes of the
same length and shape (yours will differ).

### Placeholders

A value that only holds the place of a secret is left alone: in an assignment, as the password of a
URL, in a secret column and as a credential of a connection string
(`User Id={{username}};Password={{password}};`). The whole value must be one of these, in any
letter case:

| Form | Examples |
| --- | --- |
| In angle or square brackets | `<password>`, `<your-key>`, `[password]` |
| A variable or a template | `${DB_PASSWORD}`, `$DB_PASSWORD`, `%DB_PASSWORD%`, `{{password}}`, `{env:DB_PASSWORD}`, `{file:./secret}` |
| *Your* and a name | `YOUR_PASSWORD`, `your-api-key`, `your.token` |
| A word that stands for a value | `changeme`, `change_me`, `password`, `passwd`, `secret`, `token`, `placeholder`, `example`, `redacted`, `todo`, `tbd` |
| Filler | `xxx`, `***`, `...`, `---`, `___` |
| Nothing | `null`, `none`, `nil`, `undefined`, `empty`, `true`, `false` |

`changeme123` is not a placeholder: it could be a real password. In an assignment, a reference in
code is left alone too: `process.env.API_KEY`, `os.environ["TOKEN"]`, `os.getenv(...)`,
`config.`, `settings.`. To keep a placeholder of another form in clear, add it to the values never to
mask ([allow lists](../profiles/#allow-lists)), in your settings for a project if it belongs to one.

A value of signs alone is left alone too, whatever finds it and however long: `!!!!!!!!` as a
password, `--------------------` after `Bearer`, a match of one of your
[recognizers](../custom-recognizers/) or a value the [local reviewer](../llm-review/) reports. A
fake keeps the signs of a value, so it would be its own. A Latet route key is the exception:
every character of its fake is drawn again, even when the original consists only of `_` or `-`.

## Personal data

Personal data is masked in the `balanced` and `max` profiles. Each match is validated before it is
masked, which keeps false alarms down in code:

| Type | Example | How a match is confirmed |
| --- | --- | --- |
| Email | `emily.carter@northwind-legal.com` | The top-level domain is checked against the IANA list, shipped offline |
| Phone number | `+44 20 7946 0958` | Parsed and validated with [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers), a port of Google's libphonenumber |
| IBAN | `GB82WEST12345698765432` | Country length and mod-97 checksum |
| Card number | `4111 1111 1111 1111` | Issuer prefix and Luhn checksum |
| National ID and tax number | `SSN NNN-NN-NNNN`, `DNI NNNNNNNNA` | Check digits, and a keyword where the number alone could be anything (below) |
| VAT number | `DENNNNNNNNN`, `CHE-NNN.NNN.NNN MWST` | The country prefix and that country's check digits |
| IP address | `151.101.1.69` | Public addresses; private ones only in `max` or with `include_private_ips` |
| Bitcoin address | `bc1q...` | Base58Check or bech32 checksum |

The email, IBAN, card, codice fiscale and partita IVA recognizers are ported from
[Presidio](https://github.com/data-privacy-stack/presidio).

### ID and tax numbers by country

Latet knows the national ID and tax numbers of 15 countries: Belgium, Canada, Finland, France,
Germany, Italy, Latvia, the Netherlands, Poland, Portugal, Spain, Sweden, Switzerland, the United
Kingdom and the United States. `id_regions` in `config.yaml` chooses the countries (`IT`, `US` and
`GB` by default); `latet global-install` suggests your own, and the `max` profile takes them all.
The dashboard's **Rules** page lists each country's numbers.

Many of these numbers are nine or eleven plain digits, like countless ids in code. So each one is
confirmed by its check digits, and the ones that could still be anything need a keyword nearby:
*SSN* or *social security* next to a US Social Security number, *BSN* next to a Dutch citizen
number, *Steuer-ID* next to a German tax ID. The keyword counts on the same line, a few characters
away, or in the header of the column the number is in (CSV, TSV and Markdown tables):

```text
name,ssn,city
Jane,NNN-NN-NNNN,Boston        ← found: the column is called ssn
```

The Spanish DNI and NIE, the Swiss AHV number and the Finnish personal code carry a check letter
or a distinctive layout, so they need no keyword. A number is never taken from inside a longer
number, a version, a decimal or a path. The [detectors reference](../../reference/detectors/) has
the full list, with the layout and the check of each number.

VAT numbers are recognised for the 27 EU countries, Northern Ireland, the United Kingdom,
Switzerland and Norway, whatever `id_regions` says: they always carry their country prefix.

A few things are left alone on purpose:

- emails at the documentation domains `example.com`, `example.net` and `example.org`, or under
  `.example`, `.test`, `.invalid` and `.localhost`, and system senders such as `git@` or
  `noreply@`;
- in code and tool output, digit runs without a `+`, `00`, space or parentheses, which are far more
  often ids, dates or build numbers than phone numbers. In your own messages the rules are looser.
  Numbers without an international prefix count only for the countries in `phone_regions`
  (`IT`, `US` and `GB` by default);
- version numbers that look like IP addresses, such as `v1.2.3.4`.

### Names

Names in free text, such as *Emily Carter wrote this*, need a statistical model. With the optional
[local name model](../../getting-started/installation/) installed, Latet finds names and street
addresses itself; the [local reviewer](../llm-review/), a language model on your computer or a server of yours, finds
them too, with health details and the other values no pattern describes. Without either, a name is
masked only when it is your own git author name (see below), one of your own terms, or a value
already in the vault.

Take this prompt:

```text
Hi, I'm Emily Carter and I live at 42 Mill Lane, Bristol BS1 4DJ.
```

With the name model, in the `balanced` profile, the model in the cloud receives a version like this:

```text
Hi, I'm Hannah Mitchell and I live at 45 Moore Lane, Derby YN4 9CP.
```

Your agent still sees the real name and address when the answer comes back.

What the model looks for depends on the [profile](../profiles/):

- **`balanced`**: full names (two words or more) and street addresses, in what you type and attach
  and in the text read in images. A city or postcode counts only next to a street. Code, tool output and system text are not read.
- **`max`**: one-word names and cities too, and system text and tool output as well.
- **`secrets`**: never.

Once found, a name joins the [known values](#known-values) and is masked everywhere after that,
tool output included, in every profile, `secrets` too.

The model is best effort:

- **It misses names.** Uncommon names, initials and names written in lowercase are the most often
  missed. Greetings and titles in front of a name (*Hi Paul*, *Dr. Sarah Mitchell*) are left out
  of what is masked.
- **It mistakes some words for names.** Technical words such as *Redis* or *Postgres* are filtered
  out, and `balanced` needs at least two words.
- **It is slow on long texts.** It reads roughly 700 tokens a second on a desktop CPU, in a thread
  of its own, so other requests do not wait for it. A text longer than 4,096 tokens (8,192 in `max`), or 16,000 characters
  (64,000 in `max`), is not read, and the request is flagged in the dashboard.

## Your own terms

Add the words only you know are sensitive: client names, project code names, internal domains.
They are masked in every profile, wherever they appear:

```yaml title="config.yaml"
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Acme Logistics
    type: CLIENT
```

With these terms, *"Project Falcon launches with Acme Logistics in May"* reaches the model as
something like *"Consolidated Margie launches with Northwind Adatum in May"*. By default a term
matches in any letter case, and only as a whole word; `case_sensitive` and `whole_word` change that.
You can also add terms on the dashboard's **Rules** page, or with **Always mask this** on any value
in a request.

A term needs at least one letter or digit: a fake keeps the signs of a value, so signs alone, such
as `--` or `###`, cannot be masked. The dashboard and `latet config` refuse such a term. One written
by hand in `config.yaml` or in a project's `latet.yaml` is left out, and the **Rules** page and
`latet config validate` point it out.

A term holds at most 8,192 characters: a longer one is matched on its own, and one of about a
million letters cannot be matched at all. The dashboard, `latet config` and an import refuse a longer
term. One written by hand in `config.yaml` is left out, and `latet reload`, the daemon's log and
`latet config validate` say which entry; one in a project's `latet.yaml` is left out too, and the
project's page says so.

For a format rather than a value, such as every badge number written `AB-123456`, write a
[custom recognizer](../custom-recognizers/): a pattern, check digits and keywords, tested against
invented examples.

## This machine's identity

In `balanced` and `max`, Latet detects who and where you are each time it starts, and masks it:

- your OS user name inside home paths: `C:\Users\anna\shop\app.py`, `/home/anna/...` or
  `/Users/anna/...`. A user name of six characters or more is masked wherever it appears;
- your host name;
- your git author name and email, from `git config --global`, and the domain of that email when it
  is your company's (`studio-rossi.it`, not `gmail.com`), wherever it appears: `git.studio-rossi.it`
  is masked as well.

Detected again at every start, a new git email or user name is masked with no step of yours. The
`identity` section of the [configuration](../../reference/configuration/) turns this off, or fixes
the values: once you write them there, Latet uses those instead.

## Known values

Once a value has been detected, Latet remembers it in the vault. Later occurrences are masked
even where the detectors would not recognize them on their own: for example the password from your
`.env`, mentioned later in a commit message. Two kinds are not remembered this way: a value strict
data mode or a field rule masks in a table or JSON stays masked in its structure only
([production data](../production-data/#strict-data-mode)), and a value of a type set to `redact` is
not kept at all. Known values are also what the final check looks for in outgoing requests.

A value is matched again from four characters, a person's name from three (*Ugo*, *Ada*), and only
as written: *Ugo* is not *ugo*, nor part of *Ugolino*.

A value is also found where it is written in escapes: as `\u00f2` or `\\u00f2` in JSON that Latet
does not read as a document (a code fence, JSON lines, a document held in a string), as bytes in a
log (`\xc3\xb2`) or in a path git quotes (`\303\262`), encoded in a URL (`%C3%B2`, `%20`, or `+` in
a query or a form), or as an HTML character reference (`&#242;`, `&ograve;`). Your terms and this
machine's identity are found the same way, and, where an escape writes a character, so are personal
data and the values of your recognizers: an address written `mario.rossi%40acme.it` is found too (a
secret or a value of a structure is found as it is written alone). Such a value is kept as it is
written, as a value of its own with a fake of its own, written in the same escapes where they are
part of the text: `Paolo%20Fontana` in a link, `\'` in a quoted string. What comes back to the agent
is the value exactly as it was written, so a link stays a link and an edit finds its line again. Where the
model writes the fake as it reads, `Paolo Fontana`, the agent gets the value as it reads.

## When two detectors find the same text

Overlapping matches are merged into one, covering all of them, so a value is never half replaced.
The stronger kind names the result: secrets first, then your terms and this machine's identity,
then personal data. Your terms and the known values that overlap one another are each found too: with
*Anna Maria* and *Maria Rossi* both known, *Anna Maria Rossi* is masked whole. Overlaps are looked for
up to a bound in each text: past it, the rest of the text is masked as before, one value after the
other.

If any detector other than the personal-data one and the local name model fails (secrets, your
terms, identity, known values), masking fails and the request is blocked: nothing goes out
unchecked. If the personal-data detector or the local name model fails, it is skipped and the
request is flagged in the dashboard.

## Why RE2

RE2 matches in linear time, so a hostile or simply huge input cannot trigger catastrophic regex
backtracking and stall your agent.

## False positives and negatives

Every detector trades missed values against false alarms. [Profiles](../profiles/) choose where to
sit on that trade-off, and [allow lists](../profiles/#allow-lists) fix single false alarms. To see
how a text would be masked, paste it into the dashboard's **Playground**: nothing typed there is
kept or sent to a model.
