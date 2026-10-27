---
title: Mask logs before they leave
description: An application masks its log lines with a Latet server before shipping them, and a support tool unmasks the line it needs.
sidebar:
  label: Logs
---

An application's logs name its customers: their email addresses, their IP addresses, their IBANs, now
and then a token. Shipped as they are, they sit in a log service for months, read by everyone who
reads the logs. With the [applications API](../), the application masks each batch of lines with a
Latet server before shipping it, and the log service receives fakes. Because a value always gets the
same fake, a customer's lines can still be searched, counted and followed across days; when support
needs to know who it was, a tool with the right token unmasks the line.

## The tokens

One API key for the logs whose customers you want to follow together, with one token for each side:

```sh
latet server --server https://latet.acme.lan keys add "Shop logs" --out shop-logs.key
latet server --server https://latet.acme.lan keys add-token "Shop logs" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop logs" support --unmask --out support.token
```

The application holds `shipper.token`, which can mask and cannot see a real value; the support tool
holds `support.token`, which can unmask and nothing else. Keep the first token, `shop-logs.key`, with
the administrator: it has every permission. Lines masked with another key get other fakes, so keep one
key for the services whose logs you search together.

## Masking the lines

With the Python client, `latet-client` (`pip install latet-client`, Python 3.10 or newer):

```python
from pathlib import Path

from latet_client import Latet, LatetError

latet = Latet("https://latet.acme.lan", Path("shipper.token").read_text().strip())


def ship(lines: list[str]) -> None:
    """Mask a batch of log lines, then send them to the log service."""
    try:
        results = latet.mask(lines, kind="data")
    except LatetError:
        keep_for_later(lines)  # never send the lines as they are
        return
    for result in results:
        if result.text is not None:
            send_to_log_service(result.text)
        else:  # blocked: the reason names no value
            send_to_log_service(f"[line withheld by Latet: {result.blocked}]")
```

```text
2026-10-26T09:14:02Z INFO payment accepted customer=mario.rossi@acme.it iban=IT60X0542811101000000123456
2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

leave as:

```text
2026-10-26T09:14:02Z INFO payment accepted customer=tommaso.gallo@margie-6e5a.example iban=IT17F3945471087320356678487
2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41
```

Send the lines in batches, not one call a line: a call takes up to 1,000 lines, and the client sends a
longer list in several calls, one after the other. The lines of one call are masked together: a value
a line of the call teaches the vault is masked in the other lines of the call too. A value that only
a later call teaches is not masked in the lines of the calls before it, which are already answered.
Use `kind="data"` for logs ([prose and data](../#prose-and-data)).

When the call fails, keep the lines and try again, or drop them: never send them unmasked. The client
tries a 429 and a 503 again by itself (three times, after the wait the server asks for) before it
raises. Masking the same lines twice gives the same fakes, so trying again is safe.

Every refusal of the server is a `LatetError`, with its status and its words, and a call that did not
go through is a `ConnectionFailed` (status 0). `ConnectionFailed` takes the place of the errors of
`httpx`, those of an `httpx.Client` you pass as `client=` too, and holds no request, so that the token
is in no error: catch the client's errors, not `httpx`'s. A plain `http://` address of another
computer gives a `ClearTextWarning`, because the token and the lines would travel in clear. The client
judges by the address alone, so a `client=` of yours that reaches the server over TLS in another way
gets the warning too; Python's `warnings` module silences it.

## The format of the lines

What the detectors recognize is masked wherever it sits in a line: email addresses, IBANs, card
numbers, public IP addresses, tokens and keys, the key's own terms and recognizers. For the rest,
set the key's rules ([shared settings and API keys' rules](../../getting-started/server/#set-the-shared-settings-and-api-keys-rules)):

- **Field rules** mask a field's values by its name, such as `phone` or `customer_name`, and **strict
  data mode** (`data: strict`) masks every value of a record
  ([production data](../../concepts/production-data/)). They find a value by its key in JSON, and
  in `key=value` lines where a value that holds spaces is quoted: `phone="+39 347 123 4567"`. An
  unquoted value ends at the first space. Write your log lines as JSON, or quote such values.
- **Terms** and **recognizers** catch your own names and formats: a client's name, an order number
  written `ORD-123456` ([custom recognizers](../../concepts/custom-recognizers/)).
- **Names** in the free text of a message are found with the local name model installed on the
  server, in `prose` texts; in `data` texts, with the `max` profile.

`scan` with a token that may scan shows what a sample of your lines would mask, without keeping
anything: try it on real lines before you turn masking on.

## Unmasking a line

A support person copies a masked line from the log service; their tool unmasks it:

```python
support = Latet("https://latet.acme.lan", Path("support.token").read_text().strip())
line = "2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41"
print(support.unmask(line).text)
# 2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

To find every line of a customer, mask their address with a token that may mask and search the log
service for the fake. Every unmask is written in the server's administration log, with the token and
how many values it restored; `latet server log --action apps.unmasked` lists them.

## How fast, and how many instances

On one instance, masking runs at about 190 lines a second for a new key and 410 to 440 for a key
whose vault holds 200,000 to 1,000,000 values; unmasking at 1,500 to 2,700
([the measurements](../#how-fast-it-is)). The calls of one key run one after the other on an
instance, so a service that logs faster than that needs more instances behind the load balancer, or
its lines split between keys whose customers need not be followed together.

A call holds the lines in memory until it is answered: ship from a queue of your own, so that a slow
or unreachable server delays the logs instead of losing them or sending them unmasked.

## Without Python

Any language can call the API over HTTPS: a `POST` with a JSON body and the token in an
`Authorization: Bearer` header. The [HTTP reference](../../reference/apps-api/) has the contract, its
errors and an OpenAPI description to generate a client from.
