---
title: Screenshots and images
description: The text in an image read on your machine, masked like any other text, and covered on the image with its fake.
---

A screenshot of a terminal, a form or an invoice carries the same values as the text around it: an
email address, an IBAN, a customer's name. With text recognition installed, Latet reads the text
of every image a request carries, on your machine, masks it like any other text, and paints each
value it finds over with its fake. The model reads the fake in the image; when it writes it back,
Latet puts the real value back, as it does for text.

## Turning it on

```sh
latet ocr install   # downloads the models once (about 31 MB)
```

- The models run on the `ocr` extra, ONNX Runtime and Pillow, which the installers install with Latet;
  `latet upgrade` adds it to a Latet installed without it.
- The models are PaddleOCR's [PP-OCRv6 small](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx),
  one that finds the lines of text and one that reads them, Apache-2.0 licensed. They come from
  huggingface.co at a pinned revision, and every file is checked against a pinned SHA-256 before use.
- They run on your CPU, in about a second for a full-HD screenshot, in a few seconds for a 4K or 5K
  one, and in several for the screenshot of a whole page. Nothing is sent anywhere, and Latet
  connects to huggingface.co only when you run `latet ocr install`.
- A running daemon starts using them at once. `latet ocr status` shows whether they are in use;
  `latet ocr remove` deletes them. To keep them installed but unused, set `ocr: off`.
- `latet global-install --ocr` downloads them with the rest of the setup. In
  [Docker](../../getting-started/docker/): `docker exec latet latet ocr install --yes`.

## What happens to an image

1. Latet reads the lines of text in it, and where each character is. An image more than about 3,000
   pixels long, such as the screenshot of a 4K or 5K screen, of a whole page much taller than a
   screen, or of several screens side by side, is read in overlapping parts of 2,000 to 3,000
   pixels, at a size where its text is read: were it scaled down to be read at once, its small text
   would be misread, or not found at all.
2. The text read becomes one more text of the request, *text in an image*: the detectors, your terms,
   the vault, the [local reviewer](../llm-review/) and the [Live Reviewer](../live-review/) see it
   like any other.
3. Each value found is covered on the image: a box of the background's colour, with the fake written
   over it in letters of about the same size. A type whose [policy](../profiles/#policies) is
   `redact` gets a plain bar. The image keeps its format, without its metadata; one that would come
   out larger than providers take (5 MB) is sent as a JPEG, or withheld if even that is too large.
4. Sent again, as every agent resends the conversation, the same image leaves the same, byte for
   byte, also after a restart: the provider's prompt cache keeps working.

An image with nothing to mask leaves as it was. An image Latet cannot read is withheld: the model
gets a short note in its place, *[image withheld by Latet: ...]*, with the reason. That happens to
an animated image, one larger than 20 MB or 40 megapixels, one that is not a valid image, one so
long for its width, or so wide for its height, that it would be read in more than 16 parts (the
screenshot of a page more than about 29,000 pixels long), and any image while the models are still
loading (a request waits for them up to 20 seconds).

## Which images

| What | Read and masked |
| --- | --- |
| Images in the messages: Claude's base64 images, the `data:` URLs of OpenAI Chat and Responses, Codex over HTTP and over its WebSocket, Gemini's inline images (`inlineData`) | yes |
| Screenshots returned by tools: Claude Code's Read on an image, MCP servers such as a browser's, OpenAI's computer use | yes |
| Images by their address (an `https://` URL, a file id), PDFs and other documents, audio | no: sent as they are, flagged *Not inspected*, or not sent (see below) |

## The settings

`images` says what may leave, `ocr` whether images are read. Both are in **Rules** → **Protection**
→ **Production data**, in the [configuration](../../reference/configuration/), and in a project.

| `images` | With text recognition | Without it |
| --- | --- | --- |
| `allow` (default) | images are read and masked | images are sent as they are, flagged *Not inspected* |
| `mask` | only images read and masked are sent: a request that carries one Latet cannot read (a PDF, an image by its address) is not sent | no request with an image is sent |
| `block` | no request that carries an image or a document is sent | the same |

- The [`max` profile](../profiles/) does not send images, as `block`. A project of yours can set
  `images: mask` to send them masked.
- A repository's `latet.yaml` can only send fewer images. Sending more, from your settings or a
  project of yours, and `ocr: off`, wait for your approval when an agent asks for them
  ([Agents and your settings](../agents-and-settings/)).
- An agent that is stopped gets an error saying why and what to do.

## In Claude Code

In [client mode](../../agents/claude-code/) the plugin masks the images before Claude Code sends
them:

- **Images that tools return** (Read on a PNG, an MCP server's screenshot) are replaced by their
  masked copy before Claude sees them. Your screen shows the original.
- **A screenshot you paste** cannot be changed, only taken out of the prompt. When Latet finds
  values to mask in one, the plugin takes it out and adds a note to your prompt with the path of a
  masked copy, `.latet/masked-<id>.png` in your project, which Claude opens with Read. Nothing is
  written there: the plugin answers that Read itself.
- The check before each request covers images too: an image with a value in clear that reached
  the request some other way stops it.

## Reviewing images

- **In the [Live Reviewer](../live-review/),** an image's text waits like any text, with the image
  above it: a box where each value gets its fake, the values you mask in its text covered too, and
  you can drag a rectangle over any part of it to cover that with a bar, or not send the image at
  all. Your decision is kept, sealed, for that image: sent again, it leaves the same way.
- **The [local reviewer](../llm-review/#images)** reads the text of every image anyway. It can also
  be shown the image itself, only when you turn that on, to find what text recognition missed.

## In the dashboard

- An exchange shows its images as they were sent (with `history: full`), each with a link to the
  text read in it, and the flags *Image: masked* and *Image: withheld*.
- **Rules** shows whether text recognition is installed and in use, and the command that installs
  it.

## What text recognition cannot do

- **It is best effort, like names.** Text it does not read (very small, faint, handwritten, rotated,
  in a stylised font) leaves as it is. For the screenshots that must never leave, use
  `images: block`, the Live Reviewer's rectangles, or the local reviewer shown the images.
- **A value misread is masked as read.** An `O` read for a `0` in an IBAN or an Italian fiscal code
  is put right by their check digits; another value misread may not be recognised.
- **The model reads the fake.** If it misreads it, its answer keeps the misread fake, which Latet
  cannot put back.
- **PDFs and other documents are not read**, nor images by their address: see the table above.
- **Only text.** Faces, signatures, logos and handwriting are not covered.
