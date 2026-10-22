// The dashboard's links to its documentation. Each target names a page and the English anchor of a section
// (docsLinks.json); a translation's anchor is made from its translated heading, paired with the English one by the
// heading's place on the page (docsAnchors.json, which scripts/gen_docs_anchors.py writes from the built documentation,
// and scripts/check_docs_anchors.py checks in CI). A link opens this install's own copy, the documentation of its
// version, at /_latet/docs/ when `GET /me` says it has one (`docs`), else docs.latet.ai: never a page that is not there.

import anchors from './docsAnchors.json'
import links from './docsLinks.json'

export type DocsTarget = keyof typeof links

/** Every target, in the table's order. */
export const DOCS_TARGETS = Object.keys(links) as DocsTarget[]

const LOCAL = '/_latet/docs/'
const PUBLIC = 'https://docs.latet.ai/'
// The documentation's translations: English, the reference, is at its root.
const TRANSLATIONS = ['it', 'de', 'fr', 'es'] as const
type Translation = (typeof TRANSLATIONS)[number]

const translated = anchors as Record<Translation, Record<string, Record<string, string> | undefined>>
const isTranslation = (lng: string | undefined): lng is Translation => TRANSLATIONS.includes(lng as Translation)

/**
 * Where `target` opens for a reader of `lng`: its page in that language (English for one the documentation does not
 * have), at its section, in this install's copy when `bundled`, else on docs.latet.ai.
 */
/**
 * Whether this install serves its own docs: what `GET /me` says once it has answered; until then, and on the sign-in
 * page (which cannot ask it), what the public `GET /health` said (`probeDocs`); with neither known, the public site.
 */
export function docsBundled(me: { docs: boolean } | null | undefined, probed: boolean | null): boolean {
  return me ? me.docs === true : probed === true
}

/** Whether `/health` (public, no session) says this install serves its own docs; false when it cannot be asked. */
export async function probeDocs(health: () => Promise<{ docs?: boolean }>): Promise<boolean> {
  try {
    return (await health()).docs === true
  } catch {
    return false
  }
}

export function docsHref(target: DocsTarget, lng: string | undefined, bundled: boolean): string {
  const { page, anchor } = links[target] as { page: string; anchor: string | null }
  const lang = isTranslation(lng) ? lng : null
  const section = anchor === null ? null : lang === null ? anchor : (translated[lang][page]?.[anchor] ?? anchor)
  return `${bundled ? LOCAL : PUBLIC}${lang === null ? '' : `${lang}/`}${page}${section === null ? '' : `#${section}`}`
}
