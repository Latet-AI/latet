# Writing Latet recognizers: a guide for AI agents

You are an AI agent, and the user wants Latet to hide a kind of value it does not know yet: an
employee number, a contract code, a customer id. Latet is a local privacy firewall: it masks
sensitive values before they reach a model. This guide is how you teach it a new format, safely.
Follow the steps in order. The user-facing version is the
[Custom recognizers](https://docs.latet.ai/concepts/custom-recognizers/) page.

## 1. Never handle real values

This rule comes before everything else, and it binds you too:

- Do not ask the user to paste real values. Ask them to **describe** the format: "two capital
  letters, a hyphen, six digits, in the badge column of the HR export".
- Do not read files to collect real values, and never copy one into a recognizer, its examples,
  your answer, a commit or a ticket.
- **Invent** the examples from the description. If the user shows you a real value anyway, change
  every letter and digit, keep the layout, and tell them you did.
- `latet rules add` refuses an example that Latet has already masked as a real value. Treat
  that as a sign you broke this rule.

## 2. Not sure what to look for?

When the user knows there are internal codes in their files but not their formats, look for them
without looking at them:

```sh
latet rules suggest <folder>
```

It reads the folder on this machine (a git repository: the files git knows) and prints the shapes of
code-like values that no rule covers, such as `AA-999999` (A a letter, 9 a digit), with how many
different values, in which files, the check digits they pass and the labels next to them. It never
prints a value. `--yaml` drafts a recognizer for each shape, with examples invented from the shape:
read them, ask the user what each shape is, give it a real name and description, then test it
(step 5). `--show-values` refuses to run inside an agent: do not try to work around it.

## 3. A term, or a recognizer?

- One exact value (a client's name, a project code name): that is a **term**. Add it in the
  dashboard's **Rules** page or under `terms:` in config.yaml. You are done.
- Every value written in a certain way: that is a **recognizer**. Go on.

## 4. Write the recognizer

Put it in a file of its own, for example `rules.yaml`:

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

The keys:

| Key | Required | What it does |
| --- | --- | --- |
| `name` | yes | Lower case letters, digits and hyphens; unique. The dashboard shows `recognizer:<name>`. |
| `pattern` | yes | An RE2 regular expression. A group named `value` picks the part to mask. |
| `type` | no | The type of its values, in capitals; the name in capitals when not set. Not a type Latet already has (EMAIL, PHONE, IBAN, PASSWORD...). |
| `description` | no | What it finds, in words. Write one: the user will read it in a year. |
| `checksum` | no | `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm`, or `{algorithm: weighted, weights: [...], modulus: 11, remainder: 0}` over the digits. |
| `context` | no | `words` (any case) and `acronyms` (exact case) that must be near the value: same line, within `window` characters (48), or in the column header of a table (`header: true`). |
| `examples` | strongly advised | `match` and `no_match`: invented texts, checked every time the rules load. |
| `class` | no | `custom` (default), or `secret` for values that must never leave in clear. |
| `policy` | no | `mask` (default), `redact` (one way, never restored) or `allow`. |
| `surrogate` | no | `shape` (default: same length and kinds of characters) or `{keep: 3}` to keep a prefix. |
| `min_length` | no | Shorter values are skipped. |
| `enabled` | no | `false` keeps it in the file, turned off. |

`latet config schema` prints the full JSON Schema of config.yaml, recognizers included. It is
also at <https://docs.latet.ai/config.schema.json>.

### RE2, not PCRE

- No lookarounds (`(?=`, `(?!`, `(?<=`, `(?<!`) and no backreferences (`\1`): RE2 refuses them.
  Match the context and mark the value with a named group instead:

  ```yaml
  recognizers:
    - name: contract-code
      pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
      examples:
        match: ["Contract #KLM48213"]
        no_match: ["contract signed"]
  ```

- `\b` is a word boundary, `(?i)` makes the whole pattern ignore case.
- In YAML, write patterns in single quotes: backslashes stay as they are.
- A pattern that matches an empty text (`\d*`) is refused.

### Make it precise

Latet checks every recognizer against a sample of the Python standard library: more than 50
matches, and it is turned off, because it would mask ordinary code. To stay below:

- use the format's check digits when it has them (`checksum`);
- add `context` keywords when the value alone looks like any number or word;
- anchor the pattern with `\b` and exact lengths (`\d{6}`, not `\d+`).

Values that must never reach a model in clear (an internal token, a signing key) get
`class: secret`: a request that carries one in clear is stopped, even in Claude Code, where Latet
checks requests without rewriting them.

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

## 5. Test it

```sh
latet rules test rules.yaml
```

For each example it says what was found, or why not:

| Reason | What to change |
| --- | --- |
| `the pattern does not match` | The pattern, or the example. |
| `matched 'X', but the luhn check fails` | An invented example needs valid check digits: change its last digit until it passes. |
| `matched 'X', but no keyword (...) within 48 characters` | Put a keyword in the example, or raise `context.window`. |
| `matched 'X', but shorter than min_length` | The example, or `min_length`. |
| `matched 'X', but it has no letter or digit` | Signs alone are never masked: make the pattern, or its `value` group, need a letter or a digit. |
| `too broad: N matches` | Add keywords or a checksum; tighten the pattern. |
| `not a valid RE2 expression` | Remove lookarounds and backreferences (see above). |

Exit code 0: all good. 1: an example fails. 2: the recognizer cannot load. `--json` prints the same
report for you to read. To see how many values it would find in the user's code, without printing
any of them:

```sh
latet rules test rules.yaml --corpus src/
```

It prints counts and shapes only (`AA-999999 x12`). If the count is far from what the user expects,
the pattern is too loose or too strict.

## 6. Add it

```sh
latet rules add rules.yaml
latet rules list
```

`add` saves the recognizers in config.yaml (a recognizer with the same name is updated) and applies
them to the running daemon. `latet rules remove <name>` takes one out. Tell the user what you
added, in words, and how to see it: the dashboard's **Rules** page, or **Playground** to try a
text.

## 7. Rules for one project

When the formats belong to one project, put them in that project instead of the global config.yaml:

- **The project file**, `latet.yaml` at the root of its repository, is shared with everyone who
  works on the project: `latet rules add rules.yaml --repo .` adds the recognizers there, and
  `latet project init` writes the file if it has none. It can only add protection: values never
  to mask and Latet off are ignored there. It is committed, so never put a real value in it, not
  even as a term (the user's clients' names go in their own settings).
- **The user's own settings for the project**, in their config.yaml: `latet rules add rules.yaml
  --project .` adds them there.

Check the file afterwards with `latet project check` (exit code 0: all is well) and show the user
what applies with `latet project show`.

A project whose tools read production data (query results, API answers) can mask by field instead of
by shape: `fields: [{name: note_*, type: NOTE}]` masks a column, and `data: strict` masks every value
of tool outputs. Keeping a field in clear (`mask: false`) is for the user's own settings, never the
project file. See the [production data](https://docs.latet.ai/concepts/production-data/)
page.

## 8. If something is wrong later

A recognizer with a mistake is turned off and reported; the others keep working, and the file keeps
it as written. `latet doctor`, `latet reload` and `latet config validate` name it and say why;
`latet rules test` (without a file: the recognizers in config.yaml) explains every example.
