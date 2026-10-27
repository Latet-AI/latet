---
title: Mask an n8n workflow's texts
description: The Latet nodes for n8n scan, mask, unmask and forget the texts of a workflow's items, through the applications API.
sidebar:
  label: n8n
---

The Latet nodes for n8n ([`@latet-ai/n8n-nodes-latet`](https://www.npmjs.com/package/@latet-ai/n8n-nodes-latet),
under the MIT license) call the [applications API](../) of a Latet server from a workflow. They scan,
mask, unmask and forget the texts of the workflow's items: a workflow masks what it sends to a model
or to a service, and unmasks what comes back. They need a Latet server, 0.10.0 or later, and an API
key's token; a person's computer token is refused.

## Install the nodes

In n8n: **Settings > Community Nodes > Install**, then enter `@latet-ai/n8n-nodes-latet`. n8n's
[installation guide](https://docs.n8n.io/integrations/community-nodes/installation/) has the details.

## A token for the workflow

The administrator of the Latet server makes a token with the permissions the workflow uses and no
more ([API keys and tokens](../#api-keys-and-tokens)):

```sh
latet server --server https://latet.acme.lan keys add-token "Support desk" n8n --mask --unmask --out n8n.token
```

| Operation | Permission |
| --- | --- |
| Scan | `scan`, or `mask` |
| Mask | `mask` |
| Unmask | `unmask` |
| Forget | `forget`, which no other permission brings |

In n8n, the **Latet API** credentials hold the server's address (`https://latet.acme.lan`) and the
token, the content of `n8n.token`. Use an `https://` address: over `http://` the token and the texts
travel in clear. n8n tests the credentials with a scan of one word, which keeps nothing and needs
`scan` or `mask`: a token that may only unmask or forget fails the test and still works in the node.

## What each operation does

Each operation reads the text of each item from a field (**Text Field**, `text` by default) and sends
the items in calls of at most 1,000 texts.

- **Scan** says where the values are in each text, by type, and keeps nothing.
- **Mask** gives each text with fakes in place of its values, the same fake for the same value in
  every call of the key. **Kind** says what the texts are: **Prose** for text people wrote, **Data**
  for logs, records and a program's output ([prose and data](../#prose-and-data)).
- **Unmask** puts the real values back in place of the key's fakes. The server writes each unmask in
  its administration log.
- **Forget** takes values out of the key's vault, one per item (**Value Field**, `value` by default),
  written as they were masked ([forgetting values](../forget/)).

A text the server does not answer, because its local reviewer stopped it or its guard against dumps
withheld it, comes out with `blocked` and no text, so that the text in clear goes no further.

## A model that never sees the values

A workflow that answers support messages with a model:

1. A trigger receives the message.
2. **Latet: Mask**, kind **Prose**, masks the message.
3. The model's node (OpenAI, Anthropic, Gemini, an AI Agent) gets the masked message in its prompt.
4. **Latet: Unmask**, with **Text Field** set to the field that holds the model's answer, puts the real
   values back.
5. The workflow sends the answer.

The model sees fakes only, the same ones across the messages of one customer, and the customer reads
the real values. Do not give an AI agent the Unmask operation as a tool: it would hand the model the
very values masking keeps from it.

The package's [README](https://github.com/Latet-AI/n8n-nodes-latet#readme) lists every field the nodes
write and the errors they give.
