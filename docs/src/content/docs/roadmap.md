---
title: Roadmap
description: What comes next for Latet, the soonest first.
---

What comes next, the soonest first. Ideas and help are welcome: open an
[issue](https://github.com/Latet-AI/latet/issues), or see [Contributing](../contributing/). What
has changed already is in the [changelog](../changelog/).

## Claude Code's auto mode, a plain refusal

When an auto mode review would carry a detail you keep local (your user name, your paths), Latet
stops it today, and Claude Code reports its reviewer as unavailable and refuses the action. Latet
could instead answer with the reviewer's own verdict, *blocked*, so the refusal says what it is.
That answer would come from Latet in place of Anthropic's reviewer, so we ask first: an issue
explains the case with the pull request ready, we send it to Anthropic, and it is merged only with
their agreement, attached to the issue.

## MCP tools masked before Claude Code reads them

An MCP tool's parameter schema can hold a value Latet masks, such as a sample address, and Claude
Code sends it with its requests, where Latet's plugin cannot reach it: today Latet stops those
requests (see [Troubleshooting](../troubleshooting/)). Latet would instead stand between Claude Code
and the MCP servers you configure: it would mask the tools' descriptions and schemas in the list
Claude Code reads, and put the real values back in the calls the model makes, so Claude Code's
requests stay as Claude Code built them. It would cover the servers you add yourself, not the
connectors that come from claude.ai.

## A signed installer for Windows

Antiviruses check the programs they do not know each time they start, and the launchers of a Python
install are unsigned, so on Windows every command can wait seconds before it starts (see
[Troubleshooting](../troubleshooting/#commands-are-slow-on-windows)). A signed installer, through
winget too, lets them start at once.

## Your own name model

Today Latet uses one name model, OpenAI's [Privacy Filter](https://huggingface.co/openai/privacy-filter),
which it downloads on request (`latet ner install`) and which reads many languages. A model of your own would find the names and
terms of your field, such as drugs and diagnoses in a clinic, or parties and courts in a law firm:
an ONNX token classifier on your computer, or an endpoint you run, such as a GLiNER or Presidio
service. More than one could work together, each adding what it finds.

## More of Google's Gemini API

Reading the rest of what Gemini's clients send: the Interactions API, the Live API, the Batch API,
file uploads, image generation, and Vertex's `:predict` requests (its own embedding and image
models). Until then Latet refuses them on its Gemini routes rather than send them unmasked (see
[Gemini CLI and Google GenAI](../agents/gemini/)).

## More agents

OpenCode signed in with ChatGPT, or with its models on Amazon Bedrock or Azure. Amazon Bedrock's
Mantle API for Claude Code, and Bedrock through a VPC endpoint or its FIPS address with your AWS
login (with a Bedrock API key both work already). Antigravity CLI, verified with Latet.

## pi, oh-my-pi and ThinkRail

A plugin for [pi](https://github.com/earendil-works/pi) and
[oh-my-pi](https://github.com/can1357/oh-my-pi) would mask sensitive values inside the agent, before
each request is built, as the plugin for Claude Code does. The masking would not depend on the
provider, so it would cover JetBrains AI and subscriptions as well as API keys, and each request
would also go through Latet's gateway and show in the dashboard. JetBrains ThinkRail runs pi, so its
conversations would be protected too; its subagents and a button for the dashboard wait until
ThinkRail loads extensions made by others.

## Files with sensitive data

The files an agent sends to the model, such as PDFs, Word documents and Excel sheets, would be
masked like text, one format at a time. In each file of a format it reads, Latet would replace the
sensitive values with fakes and send the masked file, then put the real values back in the answer,
in the files the model sends back too. A file in a format Latet does not read is refused, in a
conversation as uploaded on its own through a provider's files API, on a computer as on a server
(see [What is and isn't protected](../concepts/protection-scope/)).

## Latet Enterprise

Latet Enterprise is for a company whose security team sets the rules for everyone: rules that
nobody can lower, single sign-on with groups and roles such as a read-only auditor, central
approvals of changes that lower protection, audit reports, events for a SIEM and synced terms (see
[Editions and license](../enterprise/)). Two additions to it are planned:

- **Groups from Microsoft AD FS.** Connected directly, AD FS gives Latet a person's user name but
  not their groups: it adds them only to an answer sent back by form post. Latet would ask for that
  answer, so that AD FS's groups put people in Latet's groups and decide who gets an account, as
  other providers' groups do, without Keycloak in between.
- **Disconnect on Your computers.** A person would disconnect a computer of theirs, one they no
  longer have for example, from **Your computers** in the dashboard, without asking an
  administrator.

## More languages

The dashboard, the tutorial and the documentation come in English, Italian, German, French and
Spanish. More languages are open to contributions: see
[Contributing](../contributing/#how-to-add-a-language).
