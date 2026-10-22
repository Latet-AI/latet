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

## Latet Enterprise

Latet Enterprise is for a company whose security team sets the rules for everyone. On top of the
Community Edition it adds rules for everyone that nobody can lower, administration roles such as a
read-only auditor, central approvals of changes that lower protection, single sign-on, audit reports
and events for a SIEM. [Join the waitlist](https://latet.ai/#waitlist) to hear when it is out.

## More languages

The dashboard, the tutorial and the documentation come in English, Italian, German, French and
Spanish. More languages are open to contributions: see
[Contributing](../contributing/#how-to-add-a-language).
