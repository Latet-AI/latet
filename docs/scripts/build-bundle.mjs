// The copy of the documentation Latet serves itself, under /_latet/docs/ of the daemon and of a Latet server: the
// documentation of its own version, in its package (astro.config.mjs reads LATET_DOCS_BUNDLE). Built into
// ../src/latet/docs, emptied first, which the wheel and the container image carry. Then, from the repository's root,
// `uv run python scripts/check_docs_bundle.py` checks it and writes the hashes of its inline scripts (.csp.json),
// without which the daemon does not serve it.

import { rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'astro'

process.env.LATET_DOCS_BUNDLE = '1'
const docs = fileURLToPath(new URL('..', import.meta.url))
const out = fileURLToPath(new URL('../../src/latet/docs/', import.meta.url))
await rm(out, { recursive: true, force: true })
await build({ root: docs })
