---
title: Company endpoints
description: Claude Code and Codex through your company's gateway, Amazon Bedrock, Google Vertex, Microsoft Foundry or Azure OpenAI, with Latet in front.
---

At work, Claude Code and Codex often reach the model through a route your company chose rather than
straight to Anthropic or OpenAI: its own LLM gateway (LiteLLM, Portkey...), Amazon Bedrock, Google
Vertex, Microsoft Foundry or Azure OpenAI. The model is the same; what changes is who sells it and
where the requests go.

Latet reads that route from the agent's own settings and goes in front of it. The requests reach the
same endpoint, with the same keys, after Latet has checked them (Claude Code) or masked them (Codex).
Nothing to configure by hand: `latet claude`, `latet codex` and `latet global-install` do it.

## How it works

- **Latet reads where the agent goes.** For Claude Code: the variables of your shell, your own
  `settings.json` and your organization's managed settings, as Claude Code merges them. For Codex:
  `config.toml` and the profiles' files (`<name>.config.toml`). A repository's settings are never
  read: a project you clone cannot decide where your requests go.
- **A company's address becomes a destination.** It is saved in `config.yaml` under
  [`upstreams`](../../reference/configuration/#upstreams-items), named after the address
  (`https://llm.acme.com` becomes `llm-acme-com`), and the agent sends to
  `http://127.0.0.1:5283/llm-acme-com` instead. The same address always gets the same destination.
  Latet keeps some names for its own routes, and a destination cannot take them:
  `anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex` and `backend-api`.
- **Bedrock and Vertex have routes of their own**, `/bedrock/<region>/` and `/vertex/`, which reach
  only Amazon's and Google's addresses.
- **When Latet cannot go in front, it says so.** The agent does not start, and Latet says why and
  what to do. It never lets an agent go around it in silence.

For a session, `latet claude` passes its settings with Claude Code's `--settings`, which beats your
settings and the project's; `latet codex` adds `-c` options. For good, `latet global-install`
writes the same into the agents' files, and `latet global-uninstall` gives back every address it
replaced, even if you edited the file since.

## Your company's gateway

Claude Code set up with your company's gateway:

```json title="~/.claude/settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "sk-acme-..." } }
```

With `latet claude`, Claude Code sends to `http://127.0.0.1:5283/llm-acme-com`, and Latet forwards
each request to `https://llm.acme.com`, with your company's key as Claude Code sent it. Codex works the
same way, with `openai_base_url` or a provider of its own:

```toml title="~/.codex/config.toml"
model_provider = "acme"

[model_providers.acme]
name = "Acme gateway"
base_url = "https://llm.acme.com/v1"
env_key = "ACME_API_KEY"
```

Only that provider's address moves; its key, and everything else, stay as you wrote them. A provider
written in a profile's file (`~/.codex/work.config.toml`) moves there, and `latet codex --profile work`
routes that profile's providers and its `openai_base_url`.

## Amazon Bedrock

Claude Code on Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) proves who it is in one of two ways, and Latet
handles both.

- **A Bedrock API key** (`AWS_BEARER_TOKEN_BEDROCK`) travels with each request like a password: it
  passes through Latet as it is.
- **Your AWS login** (an AWS profile, an SSO login, keys in `~/.aws`): Claude Code signs each request
  for the address it sends it to. A signature made for Latet's address would not hold at Amazon, like a
  registered letter whose address was changed. So Claude Code sends unsigned requests to Latet
  (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`), and Latet signs each one for Amazon, after checking it, with
  your AWS profile.

To sign, Latet reads your profile the way Amazon's own tools do: with the AWS CLI
(`aws configure export-credentials`) when it is installed, which covers SSO logins and roles, or from
the keys written in `~/.aws`. Latet looks for `aws` on its own PATH, then in `/usr/local/bin`,
`/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` and `/snap/bin` (in `C:\Program Files\Amazon\AWSCLIV2` on
Windows): the Latet that starts at login does not see your shell's PATH. If yours is somewhere else,
start Latet from a shell where `aws` runs. The credentials stay in Latet's memory, are never written
anywhere, and sign only requests to Amazon Bedrock. When your login ends, Claude Code says so in plain words:
`run aws sso login --profile work`; once you have, it works again without restarting anything.
Claude Code's own `awsAuthRefresh` and `awsCredentialExport` settings are not used while Latet signs:
Latet reads your AWS profile itself, so run the login they would have run yourself. `latet doctor` warns
when they are set.

The route that signs carries a key only your Latet can make
(`/bedrock/eu-west-1/profile/work/<key>`), so no other program or user of your computer can have
requests signed in your name. Credentials that live only in your shell's variables (aws-vault, for
example) are handed by `latet claude` to Latet for that session. Latet keeps them in memory, and
only while that session runs: `latet claude` holds a connection open to Latet, and when it ends
(even if you kill it) Latet forgets the credentials within seconds. It forgets them at their own
expiry too, when your tool sets one (`AWS_SESSION_EXPIRATION` or `AWS_CREDENTIAL_EXPIRATION`). After
that Claude Code says in plain words that the credentials are gone: renew them and start
`latet claude` again. `global-install` needs an AWS profile, which Latet can read by itself.

With your AWS login, Bedrock's public address written out in your settings (`ANTHROPIC_BEDROCK_BASE_URL`
set to `https://bedrock-runtime.eu-west-1.amazonaws.com`) is the same as writing none: Latet signs for
it. The region in the address is the one used, even when `AWS_REGION` says another: the address is where
the requests go. Bedrock's FIPS address and a VPC endpoint are other cases: see
[Where Latet cannot go in front](#where-latet-cannot-go-in-front).

Requests to Bedrock are checked, never rewritten: they come from Claude Code, whose plugin has already
masked them. Bedrock's answers reach Claude Code exactly as Amazon sent them; Latet reads them only to
show them in the dashboard. Model ids and inference profile ARNs reach Amazon exactly as Claude Code
wrote them.

## Google Vertex

Claude Code on Vertex (`CLAUDE_CODE_USE_VERTEX=1`) sends your Google login as a token, which passes
through Latet as it is: nothing to sign. Each request names its location, and Latet sends it to
Google's address for it: the global endpoint, the `eu` and `us` multi-regions, or a region such as
`us-east5`. Different models in different regions work too.

## Microsoft Foundry and Azure OpenAI

Claude Code on Microsoft Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, with `ANTHROPIC_FOUNDRY_RESOURCE` or
`ANTHROPIC_FOUNDRY_BASE_URL`) and Codex on Azure OpenAI (a provider whose `base_url` is
`https://<resource>.openai.azure.com/openai/v1`) are destinations like a gateway: the resource's
address is saved (`foundry-acme`, `azure-acme`) and the key passes as it is.

## A model on this computer

An address on this computer (`http://localhost:11434` for Ollama, say) is left alone: a model running
here sends nothing away, and Latet cannot tell it from a proxy that forwards to the cloud. If it is
such a proxy, LiteLLM for example, add it as a destination, and Latet goes in front of it:

```sh
latet config add upstreams "{name: litellm, url: http://localhost:4000}"
```

Then start a new `latet claude` or `latet codex` session; if you set your agents up with
`latet global-install`, run `latet agents apply` again: until then their settings still send to the
proxy directly.

Latet reminds you of this each time it leaves such an address alone. Addresses on your local network
and on the internet always go through Latet.

## Where Latet cannot go in front

- **Managed settings that force an address.** When your organization's managed settings set
  `ANTHROPIC_BASE_URL` (or a cloud's address) to something else, nothing overrides them. Latet says
  so and does not start Claude Code. Your IT can set that address to Latet, as below.
- **Amazon Bedrock's Mantle API** (`CLAUDE_CODE_USE_MANTLE`): not one Latet reads yet. Use Bedrock's
  Invoke API (`CLAUDE_CODE_USE_BEDROCK`) or your company's gateway.
- **Amazon Bedrock through a VPC endpoint** (an `ANTHROPIC_BEDROCK_BASE_URL` on
  `vpce-….vpce.amazonaws.com`): not supported yet. Latet would have to sign for that endpoint, and
  Amazon refuses a signature made for Latet's address. Latet says so and does not start Claude Code:
  use Bedrock's public address, or your company's gateway. With a Bedrock API key
  (`AWS_BEARER_TOKEN_BEDROCK`) nothing has to be signed, and the endpoint is a destination like any
  company address.
- **Amazon Bedrock's FIPS address** (an `ANTHROPIC_BEDROCK_BASE_URL` on
  `bedrock-runtime-fips.us-east-1.amazonaws.com`): not supported yet. Latet can only send to the
  standard address, which is not a FIPS endpoint, so it says so and does not start Claude Code, rather
  than drop your FIPS requirement. Use your company's gateway, or the standard address if you do not
  need FIPS. With a Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`) nothing has to be signed, and the FIPS
  address stays the destination, like any company address.
- **More than one cloud at once** (`CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX`, say): Latet
  cannot tell which one Claude Code uses, so it says so and does not start it. Keep one.
- **An address two programs could read differently** (a backslash, a space, a user name or password in
  it), or a server outside your local network reached over `http`: Latet does not route it, and says
  why.
- **Claude Desktop's Code tab** ignores these variables: the plugin masks there, but the daemon does
  not see the requests (see [Claude Code](../claude-code/#coverage)).
- **OpenCode** goes through Latet with Anthropic, OpenAI and OpenRouter; its other providers do not.

## Latet for everyone in a company

Where managed settings decide Claude Code's address, IT can point it at each computer's Latet, and
give every Latet the company's endpoint as a destination:

```json title="managed-settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com" } }
```

```sh
latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Each computer still needs Latet's plugin for Claude Code: run `latet global-install` there, or start
Claude Code with `latet claude`. Without the plugin, Claude Code's requests reach Latet unmasked, so Latet
only checks them and stops each one that carries a sensitive value in clear. The address names port
5283: on a computer whose Latet runs on another port, Latet says that the managed settings send Claude
Code elsewhere, and does not start it.

## Checks along the way

- **The plugin checks every session.** When Claude Code starts, the plugin looks at where its requests
  really go, settings included. If a project's `.claude/settings.json` sets where they go (an address,
  a cloud, a proxy), the plugin stops its prompts and names the file, even when it points at Latet:
  put the line in your own settings instead. A project cannot point the plugin at another Latet either.
  When the check itself fails, prompts are stopped too.
- **`latet agents status`** shows each agent's route. **`latet doctor`** shows it too, with a
  variable in your shell that Claude Code's settings override (not your company's address when Latet
  put its own in front of it: that is as it should be), and whether the AWS CLI is there when Latet
  signs for Bedrock.
- **The dashboard** lists your destinations under Protected agents → Where requests go, with the agent that
  named each one and when it was last used, and shows on each exchange where it went and whether Latet
  signed it. Removing one there takes away that one destination and no other. One still in use cannot be
  removed: Claude Code's or Codex's settings, or your organization's managed settings, point at it, or it
  was registered since Latet started, by a `latet claude` or `latet codex` session,
  `latet agents apply` or `latet global-install`. Latet does not see a destination that only
  another agent uses (OpenCode, Hermes, a program started with `latet run`): check that none does
  before you remove it.
- **Moving a destination waits for you.** Changing a destination's address, or removing it, outside the
  dashboard (an agent's command, `config.yaml` edited by hand) waits for your approval in Approvals,
  with the address before and after: it would send your requests, and your keys, somewhere else.
  Adding one does not: only an agent pointed at it uses it. If a change is waiting when an agent adds
  one, Approvals shows the change again, with the new destination in it and what it would move now; a
  change that no longer lowers anything applies at once.

## What it was tried with

The real Claude Code (2.1.294) and Codex (0.161.0), through Latet, against providers that answer the
way each cloud does, on one computer: a company's gateway, Bedrock with an API key, with a profile
Latet signed for and with a model ARN, Vertex, and a Codex provider in Azure's form. Latet's
signatures match those of AWS's own library, request by request. If you use one of these clouds at
work, [tell us how it goes](https://github.com/Latet-AI/latet/issues).
