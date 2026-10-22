---
title: Profiles & policies
description: Choose how much Latet masks (balanced, max, secrets) and what happens to each kind of value.
---

## Profiles

A profile decides which [detectors](../detection/) are active. There are three:

| Masked | `balanced` (default) | `max` | `secrets` |
| --- | --- | --- | --- |
| Secrets: API keys, tokens, passwords, private keys | ✅ | ✅ | ✅ |
| The values of secret columns and fields (`password`, `api_key`, `reset_token`...) in tables and JSON | ✅ | ✅ | ✅ |
| Your own terms | ✅ | ✅ | ✅ |
| Your own [recognizers](../custom-recognizers/) | ✅ | ✅ | ✅ |
| Values already in the vault | ✅ | ✅ | ✅ |
| Personal data: emails, phone numbers, IBANs, card numbers, VAT numbers, public IP addresses, Bitcoin addresses | ✅ | ✅ | ❌ |
| National ID and tax numbers ([by country](../detection/)) | the countries in `id_regions` | every country | ❌ |
| This machine's identity: your user name in paths, host name, git author and its company domain | ✅ | ✅ | ❌ |
| Private IP addresses (`10.x`, `192.168.x`...) | ❌ | ✅ | ❌ |
| Every value of the tables, JSON and `key=value` pairs a tool prints ([strict data mode](../production-data/#strict-data-mode)) | ❌ | ✅ | ❌ |
| Names and street addresses, with the [optional local name model](../../getting-started/installation/) | in your prompts, attachments and the text in images | everywhere | ❌ |
| What no pattern finds, with the [optional local reviewer](../llm-review/) | when it is on | when it is on | when it is on |
| The text in images, with the [optional text recognition](../images/) | masked | not sent | masked |
| PDFs, and images without text recognition | sent, and flagged | not sent | sent, and flagged |

- **`balanced`** suits most code work. It is the default.
- **`max`** adds private IP addresses and the ID numbers of every country, turns
  [strict data mode](../production-data/#strict-data-mode) on (every value of a table a tool prints
  is masked, an id or a state too), looks for names everywhere when the local model is installed,
  and refuses to send a request that carries an image or a PDF. The error says what to do: remove
  the image from the conversation (in Claude Code, `/rewind`), or switch to `balanced`. A project of
  yours can send images with their text masked, with `images: mask`.
- **`secrets`** masks secrets and your own terms only. Personal data and your identity pass through.
  Pick it when false alarms on personal data get in your way.

### Choosing a profile

- **When you route your agents for good:** `latet global-install --profile max`.
- **In the dashboard:** the **Rules** page. The change applies at once to new requests, without a
  restart.
- **In `config.yaml`:** set `profile: max`, then run `latet reload` to apply it without a
  restart.

### Fine-tuning

Beyond the profile, the dashboard's **Rules** page and `config.yaml` let you choose:

- `pii`: which personal-data types to detect, for example `[EMAIL, IBAN]`. The list replaces the
  profile's choice: a type it leaves out is not detected, the ID numbers of `id_regions` included
  (and of every country in `max`). Left out, every type the profile enables is detected. In your
  settings for a project, `pii` adds types to the global ones instead.
- `phone_regions`: countries whose phone numbers are recognized without an international prefix
  (default `IT`, `US`, `GB`).
- `include_private_ips: true`: mask private IP addresses in `balanced` too.
- `data`: `strict` turns strict data mode on in `balanced` and `secrets` too, `normal` turns it off
  in `max`. Left out, the profile decides.
- `disabled_secret_rules`: gitleaks rules to turn off, such as `generic-api-key`.
- `identity`: turn off the masking of this machine's identity, or write its values yourself.

Every key is listed in the [configuration reference](../../reference/configuration/).

## Policies

A policy decides what happens to one type of value once it is detected. The default is `mask`:

| Policy | What the model sees | What you get back |
| --- | --- | --- |
| `mask` (default) | A surrogate: `+44 20 3318 4452` | The original: `+44 20 7946 0958` |
| `redact` | A fixed marker: `[REDACTED:PHONE]` | The marker. Nothing is restored, so a file the agent writes contains `[REDACTED:PHONE]` too |
| `allow` | The original, unchanged | The original |

Set a policy per type in `config.yaml`, or on the dashboard's **Rules** page:

```yaml
policies:
  PHONE: redact
  IP_ADDRESS: allow
```

The type names are the ones the dashboard shows next to each masked value, such as `EMAIL`,
`PHONE`, `IBAN`, `PASSWORD`, or the type you gave a custom term. Types without a policy are masked.

`redact` suits values the model never needs, not even in fake form. It works one way only: the
model gets the marker, and Latet never puts the original back in answers or files. Whether Latet
keeps the original depends on how the value was found:

- A value found by the detectors, your terms and recognizers included, is not kept.
- A value found by the [local reviewer](../llm-review/), or by you in the
  [Live Reviewer](../live-review/), is kept all the same: sealed in the vault, with a fake of its
  own that is not sent. Latet then finds it again wherever it appears, and redacts it there too.

A value longer than 16,384 characters, such as a whole JSON string in
[strict data mode](../production-data/#strict-data-mode), is redacted whatever its type's policy,
unless that type is `allow`: it is too long for the vault to keep, so it is never put back, and a
file the agent writes from it holds the marker.

## Allow lists

When Latet masks something that is not sensitive (a false positive), tell it to leave that value
alone:

```yaml
allow_values:
  - demo@northwind-legal.com # exact value, any letter case
allow_patterns:
  - "test_[0-9a-f]{16}"      # RE2 regular expression, matched against the whole value
```

In the dashboard you can do it in one click: open a request, click the masked value and choose
**Not sensitive: always allow**. The opposite action, **Always mask this**, adds the value as a
custom term. In a request of a [project](../projects/), choose first whether it applies to
*This project* or *Everywhere*.

Every such click adds a value, so the list grows. On the dashboard's **Rules** page, the *Allowed
values* tab shows it as a table, values and patterns together with their kind: search it, filter it,
open a row to change it (a pattern's panel tries it on a value), or select rows and delete them.
*Export* and *Import* move the list as CSV (`kind,value`); an import adds what is new and, since
it lets values through, applies it only when you tick *Apply these too*.

Latet's own name is always left in clear: it is the tool the agent talks about, never a value of
yours.

To try your rules on a text without sending anything, paste it into the dashboard's
**Playground**.

## In client mode

In [client mode](../how-it-works/) (Claude Code), masking happens in the plugin, with the same
profile, policies and allow lists. The daemon never rewrites a request: it can
only let it through or block it. When it blocks one because a value would leave in clear, open the
request in the dashboard: that value is boxed in red. If it is not sensitive, click it and choose
**Not sensitive: always allow**, as above, then try again.
