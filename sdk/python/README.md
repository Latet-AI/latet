# latet-client

A light Python client for the applications API of a [Latet](https://latet.ai) server. An application masks its log
lines or its documents with an API key's token before they leave, and a person or a program with the right token
restores them later. The package only talks HTTP (`httpx`): it has no detector and no model, and the server does the
work.

It is released on its own, under the MIT license, so that any application can embed it. Latet itself is under its own
license: see [latet.ai](https://latet.ai).

## Install

```bash
pip install latet-client
pip install "latet-client[langchain]"   # with the LangChain transformer and runnables
```

Python 3.10 or newer. You need a Latet server, an API key on it, and a token of that key with the permissions of the
operations you call: `scan`, `mask`, `unmask` or `forget` (a token with `mask` can `scan` too; `forget` is given on its
own, and the key's first token does not have it).

## Use

```python
from latet_client import Latet

with Latet("https://latet.example.com", token) as latet:
    masked = latet.mask("Invoice 12 was paid by mario.rossi@acme.it")
    print(masked.text)  # Invoice 12 was paid by a fake address
    print(masked.counts)  # {'EMAIL': 1}

    lines = latet.mask(log_lines, kind="data")  # a list in, a list out, in the same order
    original = latet.unmask(lines[500].text)  # needs a token with the unmask permission
    print(original.text, original.restored)
```

One text in, one result out; a list in, a list out. `kind` is `"prose"` (text people wrote, the default) or `"data"`
(logs, records, a program's output).

- `scan(texts)` says where the server's rules find values, and keeps nothing: `ScanResult(findings, counts)`, each
  finding `Finding(type, entity_class, start, end)` with positions in the text you sent.
- `mask(texts)` gives each text with the fakes of the API key's vault in place of its values:
  `MaskResult(text, replacements, counts, blocked, degraded)`, the positions of the replacements in the text it
  answers. The same value gets the same fake in every call, on every instance of the server, for as long as the vault
  keeps it. When the server's local reviewer stops a text, `text` is `None` and `blocked` says why. `degraded` names
  what ran weaker on the call, if anything did.
- `unmask(texts)` gives each text with every fake of the API key's vault replaced by its value, whichever call made it:
  `UnmaskResult(text, restored)`. The server writes each unmask in its administration log.
- `forget(values)` takes values out of the API key's vault: `ForgetResult(forgotten, unknown)`, how many the vault
  held and how many it did not (see below).

`AsyncLatet` is the same with `await` (`async with AsyncLatet(url, token) as latet`, `await latet.mask(...)`), over
`httpx.AsyncClient`.

### Forgetting values

When a person asks that their data leave your logs' vault, a token with the `forget` permission takes their values out
of it:

```python
with Latet("https://latet.example.com", eraser_token) as latet:
    found = latet.forget(["mario.rossi@acme.it", "+39 347 123 4567"])
    print(found.forgotten, found.unknown)  # how many the vault held, how many it did not
```

Each value is found exactly as you write it: another case, other spaces or another written form (`%40` for `@`) is
another value, which the vault keeps as an entry of its own if it met it. A value forgotten goes with its fake: the
fake restores nothing from then on, and the value gets a new fake if a text holds it again. An address at a company's
domain keeps the domain as a value of its own (its fake is the domain of the address's fake): forget it too if it should
go. A list goes in calls of `max_texts` values at most, each value once, and the counts are summed. The server writes
each call in its administration log with its counts, never a value, and forgets nothing it cannot write there. A forget
is safe to repeat: a value already gone is counted as not held. When a call of a long list fails, its error is raised
and the counts of the calls before it are not returned: what they forgot stays forgotten, and sending the whole list
again forgets the rest. A key's first forget after the server's upgrade first indexes what the key's local reviewer
remembered and its history kept before it: on a key with much history it can take minutes, longer than the client's
`timeout` (90 seconds). Give the client a longer `timeout` for it, or send the list again after a `ConnectionFailed`:
what the server forgot meanwhile stays forgotten.

The answer says whether the vault held each value, so a token with `forget` learns which values the vault knows, as one
with `scan` or `mask` can: give it only to what needs it.

### Long lists

A list goes to the server in calls of at most `max_texts` texts (1000) and `max_bytes` bytes of JSON (8 MiB); a text
larger than `max_bytes` goes alone, and the server answers 413 if it is past its own limit. Every text is checked and
written before the first call: a text that cannot be written as UTF-8 (a lone surrogate, as a log read with
`errors="surrogateescape"` can hold) is a `ValueError` naming its place, and nothing is sent.

The calls of one list are sent one after the other, in order. The texts of one call are masked together: a value that a
text of the call teaches the server is masked in the other texts of the call too. A value that only a later call
teaches is not applied to the texts of the calls before it, which are already masked and answered: if that matters,
keep the texts that must be masked together in one call (raise `max_texts` and `max_bytes`, up to what the server
takes).

### Errors and retries

The server's refusal is a `LatetError(status, message)`, with the server's words as `message`:

| Status | Class | Meaning |
| --- | --- | --- |
| 401 | `Unauthorized` | no token, or a token the server does not know |
| 403 | `Forbidden` | the token lacks the permission, or it is a person's token |
| 413 | `TooLarge` | the call is larger than the server takes |
| 429 | `RateLimited` | too many calls or refused tokens; `retry_after` is the seconds the server asked |
| 503 | `Unavailable` | a store of the server does not answer, or its log did not take an unmask or a forget |
| 507 | `VaultFull` | the key's vault holds the most values it may; nothing was masked |
| 0 | `ConnectionFailed` | the call did not go through: no connection, a broken one, a timeout |

Only a 429 and a 503 are tried again: `retries` times (3), after the `Retry-After` the server gave (seconds; 30 at most
each time; without it 1, 2, 4 seconds), and they are raised after that. Any other status is raised at once, and so is
`ConnectionFailed`: a 422 means the texts name more of the vault's values than one call may weigh (send fewer texts in a
call), and the call masked nothing; a 400, 404, 405 or 500 is a plain `LatetError`. A masking call is safe to repeat (the
same value gets the same fake), and so are an unmask and a forget, so a program may try again after a `ConnectionFailed`
if it wants to.

`ConnectionFailed` has status 0 and a message that names the kind of failure and the server's host
(`the call to latet.example.com did not go through (ConnectTimeout)`). It replaces `httpx`'s own error, which carries the
request and so its `Authorization` header, and it is raised without it as `__cause__` or `__context__`.

### The token

The token is in the `Authorization` header of each call and nowhere else: not in an exception's message, not in a
`repr`, not in a log line, and not in anything an error the client raises holds. Use an `https://` address for any
server that is not on your own computer: a plain `http://` address of another computer makes the client give a
`ClearTextWarning` (a Python warning), because the token and the texts then travel in clear. Pass your own `httpx.Client`
(a proxy, a certificate) as `client=`; the client closes only the one it made.

## LangChain and LangGraph

`latet_client.langchain` masks the documents of a RAG pipeline before they are indexed, masks the question, and
restores the model's answer:

```python
from langchain_core.runnables import RunnableLambda
from latet_client import Latet
from latet_client.langchain import LatetMasker, mask_runnable, unmask_runnable

latet = Latet("https://latet.example.com", token)

masked = LatetMasker(latet).transform_documents(documents)  # index these, not the originals
# each has metadata["latet_counts"]: how many values of each type were masked; a blocked document raises Blocked

chain = mask_runnable(latet) | RunnableLambda(ask_the_model) | unmask_runnable(latet)
answer = chain.invoke("What did mario.rossi@acme.it pay?")  # the model sees fakes, you read the real values
```

The masked documents and the masked question share their fakes, because they come from the same API key's vault, so the
model can tell that they speak of the same person. `unmask_runnable` takes a chat model's message (`AIMessage`) too,
and gives its text back restored. `mask_runnable` and `unmask_runnable` are plain runnables: use them as nodes of a
LangGraph graph as well. With an `AsyncLatet` the transformer has `atransform_documents` and the runnables
`ainvoke`. Only a document's text is masked, not its metadata.

## License

MIT, see `LICENSE`.
