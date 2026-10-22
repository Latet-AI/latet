---
title: Custom recognizers
description: Teach Latet a format of your own, such as an employee number or a contract code.
---

Latet knows secrets, personal data and national IDs, but not the formats of your company: badge
numbers, contract codes, customer ids. A **recognizer** teaches it one. It is a pattern, optionally
check digits and keywords, and a few invented examples it is checked against.

Use a [term](../detection/#your-own-terms) instead when there is one exact value to hide, such as a
client's name. A recognizer is for every value written in a certain way.

## Never paste real values

A recognizer describes a format. Its examples are invented: write them from the description, or
take a real value and change every letter and digit, keeping the layout. `latet rules add`
refuses an example that Latet has already seen as a real value. When an AI agent writes the
recognizer for you, describe the format to it in words: two capital letters, a hyphen, six digits,
found in the badge column of the HR export.

## Finding the formats

Not sure which formats your files hold? Let Latet look, on this machine:

```sh
latet rules suggest ./my-project
```

It lists the shapes of code-like values that no rule covers yet, such as `AA-999999` (A for a
letter, 9 for a digit), with how many different values it saw, in which files, the check digits
they pass and the labels next to them (*badge*, *invoice_ref*). It never prints a value. In a git
repository it reads only the files git knows, so ignored files stay out. `--yaml` drafts a
recognizer for each shape, with examples invented from the shape; describe what each finds before
you add it. `--show-values` shows three values of each shape at your own terminal, and refuses to
run inside an AI agent or a pipe.

## An example

```yaml
recognizers:
  - name: employee-id
    description: Badge numbers of the HR system
    pattern: '\b[A-Z]{2}-\d{6}\b'
    context:
      words: [badge, employee]
    examples:
      match: ["badge AB-123456", "employee: QX-904417"]
      no_match: ["AB-123456 alone", "build AB-12345"]
```

Put it in a file of its own, such as `rules.yaml`, then:

```sh
latet rules test rules.yaml     # every example, with the reason when one fails
latet rules add rules.yaml      # saves it in config.yaml and applies it
latet rules list
```

From then on `AB-123456` next to the word *badge* is masked as `EMPLOYEE_ID`, and the dashboard
names the rule `recognizer:employee-id`. The fake keeps the shape: two letters, a hyphen, six
digits.

The dashboard's **Rules** page does the same in its *Recognizers* tab: a table of your recognizers,
each row with its name, its type, what it finds (the pattern, cut short), what happens to its values
and how it loaded. *Add recognizer* opens a panel with every field; fill it in, and *Try it* on a
text of your own (nothing typed there is kept). It shows every example's result and the matches in
ordinary code before you save. Select rows to turn them on or off, or to delete them; *Export* and
*Import* move them as a rules file. With the [local reviewer](../llm-review/#recognizers-from-a-description)
on, *Draft from a description* writes the recognizer for you from a sentence (*"badge numbers: BD,
a dash and six digits"*), with invented examples, and opens it in the panel when they pass, for you
to check and save.

## For one project

A recognizer can belong to a [project](../projects/) instead of the whole of Latet:

```sh
latet rules add rules.yaml --repo .          # into the project's latet.yaml, shared with the team
latet rules add rules.yaml --project .       # into your own settings for the project
latet rules list --project .
```

The dashboard's **Rules** page does the same once you pick the project in the selector at the top:
its *Recognizers* table shows the project's own, from your settings and from `latet.yaml`, with
your global ones, and a new one goes where its panel's *Save to* says.

## What a recognizer has

| Key | What it does |
| --- | --- |
| `name` | Lower case letters, digits and hyphens, unique. |
| `type` | The type shown for its values, in capitals. The name in capitals when not set. It cannot be a type Latet already has. |
| `pattern` | An [RE2](https://github.com/google/re2/wiki/Syntax) regular expression. A group named `value` picks the part to mask. |
| `checksum` | Check digits the value must pass: `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm`, or a weighted sum. The fakes pass it too. |
| `context` | Keywords (`words`, any case; `acronyms`, exact case) that must be on the same line, within `window` characters, or in the column header of a table. |
| `examples` | `match` and `no_match`: invented texts, checked every time the rules load. |
| `class` | `custom`, or `secret` for values that must never leave in clear: they are masked as usual, and a request that would carry one in clear is stopped, even in Claude Code. |
| `policy` | `mask` (the default), `redact` or `allow`. An entry in `policies` wins. |
| `surrogate` | `shape`, or `{keep: 3}` to keep a prefix as it is. |
| `enabled` | `false` keeps the rule in the file, turned off. |

The [configuration reference](../../reference/configuration/) lists every key, and the
[JSON Schema](https://docs.latet.ai/config.schema.json) lets your editor complete them.

## Patterns are RE2

RE2 runs in linear time, so no pattern can stall a request. It has no lookarounds (`(?=`, `(?<=`)
and no backreferences (`\1`). Where you would use a lookbehind, match the context and mark the
value with a named group:

```yaml
recognizers:
  - name: contract-code
    pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
    examples:
      match: ["Contract #KLM48213"]
      no_match: ["contract signed"]
```

Only `KLM48213` is masked. Write patterns in single quotes in YAML, so backslashes stay as they are.

A value needs a letter or a digit. A match of signs alone, such as the `---` of a Markdown table, is
left as it is, since its fake would be itself; `latet rules test` and *Try it* say so.

## Precise rules

A rule that fires all over ordinary code would mask the code itself, so Latet checks each one
against a sample of the Python standard library: more than 50 matches and it is turned off.
Keywords and check digits are the cure. A number whose last digit is a Luhn check, for example:

```yaml
recognizers:
  - name: card-ref
    type: CARD_REF
    pattern: '\bCR(?P<value>\d{8})\b'
    checksum: luhn
    class: secret
    examples:
      match: ["ref CR12345674"]
      no_match: ["ref CR12345678"]
```

`latet rules test rules.yaml --corpus src/` also says how many values the rule would find in
your own files. It prints their shapes only (`AA-999999 x12`), never the values.

## When something is wrong

A recognizer with a mistake (a pattern RE2 rejects, an unknown checksum, a misspelled key) is turned
off; the others keep working, and the file keeps it as you wrote it. `latet reload`,
`latet doctor` and the dashboard's **Rules** page say which one and why, and
`latet rules test` explains every example. `latet config validate` checks the whole file.

Writing recognizers with an AI agent? Point it at
[`llms-rules.md`](https://github.com/Latet-AI/latet/blob/main/llms-rules.md) in the repository: the
same steps, written for it.
