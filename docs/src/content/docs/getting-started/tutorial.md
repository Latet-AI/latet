---
title: Tutorial
description: See Latet at work on your computer in about 30 minutes, with a pretend agent, a pretend model and invented data, before you point a real agent at it.
---

`latet tutorial` shows Latet at work in your browser, in about 30 minutes, before you point a
real agent at it. A pretend agent sends its requests to a pretend model. Latet, between them, is
the real one: the same masking, checks and dashboard you get with your own agent. The data is
invented, and nothing leaves your computer.

## Start it

```sh
latet tutorial
```

It opens the tutorial in your browser, on the menu of chapters, and runs until you end it. The
terminal says where it is:

```text
Tutorial at http://127.0.0.1:52817 — Ctrl+C ends it
```

| Command | What it does |
| --- | --- |
| `latet tutorial` | Opens the menu of chapters. |
| `latet tutorial terms` | Opens one chapter, by its id: here, *Your own terms*. |
| `latet tutorial --list` | Prints the chapters, their minutes and which you finished, then stops. |
| `latet tutorial --print-url` | Prints the sign-in link instead of opening the browser. It works once, for two minutes. |
| `latet tutorial --keep` | Keeps the sandbox's folder when the tutorial ends, and says where it is. |

The tutorial runs on your computer, not in Latet's [container](../docker/).

## The chapters

Each chapter takes a few steps. You send the agent a prompt, typed for you, and Latet's pane shows
what happened to its request, in the dashboard's own views. Some steps ask you to act there: click a
value, add a word to your terms, approve a request, save your guidance.

| Chapter | Minutes | What happens |
| --- | --- | --- |
| `basics`: The basics | 5 | A key in `.env`, an email address and a phone number leave as fakes; the file the agent writes holds the real key. |
| `terms`: Your own terms | 3 | A codename leaves in clear until you add it to your terms; from then on the model gets a fake, a name it has never seen. |
| `false-positives`: A value masked for nothing | 3 | Stripe's test card is masked like a customer's card; *Not sensitive* lets it through. |
| `stopped`: A request stopped | 3 | A key pasted in a conversation from before Latet was on stops the request; a new conversation without it goes through. |
| `live`: The Live Reviewer | 3 | A request waits for your approval: you mask a customer's name by hand, then approve it. |
| `settings`: An agent lowering your rules | 3 | The agent asks Latet to let email addresses through, and the change waits for you: approve it or undo it. |
| `data`: Production data | 4 | Strict data mode masks every value of a query's table; you keep ids, dates and plans in clear. |
| `llm`: The local reviewer | 4 | A support ticket holds an email address written out, an IBAN with a typing mistake, a name and a diagnosis: no pattern finds them, the local reviewer does. Then your guidance has it find the hospital's wards too. The reviewer is simulated: the Tutorial Reviewer. |
| `images`: Screenshots | 3 | The agent sends a screenshot of a refused payment: Latet reads the email address, the IBAN and the phone number in it, and covers each with its fake before the image leaves. The chapter needs text recognition: `latet ocr install`. |

A chapter ends with what you learned and where it is in the dashboard. The last one ends with how
to use Latet with your own agent. The menu marks the chapters you finished, from one run to the
next, and *Start over* puts a chapter back as it began.

## A sandbox

The chapters mask values, stop requests and change settings, so they run on a Latet of their
own, in a sandbox:

- **Its own data, settings and history**, in a temporary folder, and its own server on `127.0.0.1`,
  on a free port. Your own daemon is not touched, and need not be running.
- **Nothing of yours changes:** your settings, your vault and history, and your dashboard's sign-in
  (the tutorial's page signs in with a cookie of its own).
- **Nothing leaves your computer.** The sandbox's one route leads to the pretend model, which runs
  inside the `latet tutorial` command: no request can reach a provider, and no API key or sign-in
  is needed.
- **The same fakes at every run:** the sandbox makes them from a fixed key, not from yours.
- **One file outside it:** `tutorial.json` in your
  [data folder](../uninstall/#3-delete-local-data-optional), with the ids of the chapters you
  finished.

The sandbox has no name detection and none of your identity: the chapters show what the detectors
find, and what you mask by hand. In the local reviewer's chapter, the reviewer is the Tutorial
Reviewer: simulated, it finds what the chapter says, and no request reaches a server. In the
screenshots chapter, the sandbox reads the text in images with your own text recognition models,
once they are installed (`latet ocr install`). Every other page of its dashboard is there to
explore, with a bar on top that brings you back to the chapter.

## End it

Click *End the tutorial* in the page, or press Ctrl+C in the terminal: the sandbox stops, and its
folder is deleted. A run cut off another way, its terminal closed for example, leaves the folder in
your temporary folder, and the next `latet tutorial` deletes it. A run that stops on an error
prints the last lines of its log, and keeps the folder, with the whole log, until the next run.

## In your own dashboard

What the chapters show, in the dashboard of your own Latet (`latet dashboard`):

| What | Where |
| --- | --- |
| Every request: your computer's text, and what the model got | **Live**, then the request |
| Every value Latet replaced, and its fake | **Vault** |
| Your terms | **Rules** → **Terms** |
| The values you let through | **Rules** → **Allowed values** |
| The requests Latet stopped, and why | **Live**, filtered by *Blocked*, then the request |
| What waits for your approval: requests, and changes to your settings | **Approvals** |
| The Live Reviewer, for every project or for one | **Rules** → **Reviewers** → **Live Reviewer** |
| Strict data mode, and the columns you keep in clear | **Rules** → **Protection** → **Production data** |
| What the local reviewer found in a request | **Live**, then the request: *Local reviewer* |
| The local reviewer, and your guidance | **Rules** → **Reviewers** → **Local reviewer** |
| An image a request carried, as it was sent, and the text read in it | **Live**, then the request: *Images* |
| What leaves with an image or a PDF, and whether the text in images is read | **Rules** → **Protection** → **Production data**: *Images and PDFs* |

Then try it on your own agent, one session, with a made-up key: see the
[Quickstart](../quickstart/).
