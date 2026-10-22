# Contributing to Latet

Thank you for helping. This page covers the development setup and the checks a change has to pass;
the [contributing guide](https://docs.latet.ai/contributing/) in the documentation goes
further (the dashboard, the documentation site, adding a language).

## Before you start

- **Issues first.** A bug goes in an issue, with the steps that reproduce it; a pull request that
  fixes it is welcome. For a new feature, open an issue before you write the code, so that what it
  does is agreed first.
- **The contributor license agreement.** Latet is offered under the Latet Sustainable Use License,
  the Latet Enterprise License and commercial licenses ([LICENSING.md](LICENSING.md)). For your
  contribution to be part of all of them, you accept the [contributor license agreement](CLA.md) once:
  tick its box in your first pull request. You keep your copyright, and the agreement covers your
  later contributions too.

## Set up

You need [uv](https://docs.astral.sh/uv/) and Node.js 22.12 or newer.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync                          # Python, the dev tools and the optional ner, ocr and server extras
cd dashboard && npm ci && cd ..  # the dashboard (React)
cd docs && npm ci && cd ..       # the documentation site (Astro Starlight)
```

Run it from the checkout with `uv run latet ...`, for example `uv run latet serve` for the daemon
in the foreground. To keep a test instance apart from your real one, set `LATET_DATA_DIR` and
`LATET_CONFIG` to a scratch folder and pick another port in that `config.yaml`. The dashboard has a
mock mode for UI work: `npm run dev:mock` in `dashboard/`.

## Checks

CI runs all of these on Windows, macOS and Linux:

```sh
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run pytest -q
uv run python scripts/gen_reference.py --check
uv run python scripts/gen_notices.py --check
```

In `dashboard/`: `npm run lint`, `npm run check:i18n`, `npm test` and `npm run build`. In `docs/`:
`npm run build`. For the Claude Code plugin: `claude plugin test` in `src/latet/claude_plugin/`.

The documentation each install serves itself, at `/_latet/docs/`, where every link of the dashboard
opens it: `npm run build:bundle` in `docs/` builds it into `src/latet/docs/`, which the wheel and the
image carry. Then, from the repository's root:

```sh
uv run python scripts/check_docs_bundle.py                            # writes its .csp.json too
uv run python scripts/gen_docs_anchors.py --check --root src/latet/docs
uv run python scripts/check_docs_anchors.py --root src/latet/docs     # every dashboard link lands
```

The check refuses a bundle above 14 MB zipped (it is about 13 MB): the wheel carries it to every install. When new
pages or pictures cross the limit, make the bundle lighter (pictures it can do without, as it does without the video)
or raise `MAX_MB` in `scripts/check_docs_bundle.py` on purpose, saying why in the commit.

A link the dashboard gains goes in `dashboard/src/lib/docsLinks.json`, by page and English anchor;
`uv run python scripts/gen_docs_anchors.py` then writes its anchor in each translation.

## The server's tests

The tests of a Latet server's backends are in `tests/server/`, marked `server`. They need PostgreSQL
and Valkey, which the script below starts in Docker. Without the two services they are skipped, and
each skip says why. To run them, start the services and give the tests their addresses:

```sh
uv run python scripts/server-services.py up     # PostgreSQL, Valkey and a PostgreSQL with TLS
uv run python scripts/server-services.py env    # prints the variables the tests read
uv run pytest -q -m server                      # with those variables set in your shell
uv run python scripts/server-services.py down   # stops the services and removes their containers
```

The services listen on `127.0.0.1` only, with a password made for tests. The first `up` downloads
their images; name the services you want (`up postgres valkey`) to start fewer. CI runs the same
tests in a job of its own with `LATET_TEST_REQUIRE_SERVICES=1`, which makes a server test that cannot
run fail instead of being skipped; set it to get the same on your computer.

## Generated files

Do not edit these by hand; regenerate them:

- `docs/src/content/docs/reference/*.md`: `uv run python scripts/gen_reference.py`
- `THIRD_PARTY_NOTICES.md`: `uv run python scripts/gen_notices.py`

## Measuring a local reviewer

Which local model reviews best is still open, and your measurements are welcome. Serve a model on this
computer (llama.cpp, Ollama or LM Studio), point `LATET_LLM_URL` and `LATET_LLM_MODEL` at it, and run
the benchmark on its 93 invented cases:

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Send what it wrote in `benchmarks/llm-review/` (your rows in `results/`, and `tables.md`) in a pull
request, with your graphics card or CPU, the server and the model's quantization.
`benchmarks/llm-review/README.md` says how the published numbers were made.

## Ground rules

- Tests never touch the network (pytest-socket allows only 127.0.0.1 and ::1) or your real
  configuration (`tests/conftest.py` points Latet at temporary folders). The opt-in tests of the real
  agents (`LATET_AGENT_TESTS=1`, in `tests/agents/`) run third-party programs, Claude Code and Codex,
  as subprocesses, and pytest-socket cannot fence those: it patches the sockets of the test process
  alone. They are given fake providers on 127.0.0.1, invented keys and data, no update check and no
  telemetry, and a proxy at a port nothing listens on for everything else. Read
  `tests/agents/test_real_agents.py` before you opt in.
- Every user-facing string of the dashboard lives in `dashboard/src/locales/en.json`.
- The documentation is in English, the reference version: a change to Latet updates the English
  pages it affects in the same pull request. Translations are welcome and follow the English pages.
- Never commit real credentials, personal data or captured traffic. The pre-commit hooks (ruff and
  gitleaks) catch most of it: `uvx pre-commit install`.
- Report security problems privately, as [SECURITY.md](SECURITY.md) explains.
