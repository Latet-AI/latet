// The ways the dashboard opens its documentation (lib/docsLinks.ts): a page's "Documentation" at the top right of its
// header, a "?" beside a section's title and at the top of a panel, and "Why" in a notice that explains what happened.
// Each opens its section in a new tab, in the dashboard's language, from this install's own copy when it has one.

import { useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { docsBundled, docsHref, probeDocs, type DocsTarget } from '../lib/docsLinks'
import { useMe } from '../lib/me'
import { Icon } from './Icon'

// What the public GET /health said of the docs, asked once a page load and shared by every link: what they open until
// /me answers, and on the sign-in page, which cannot ask it (null: not known yet).
let probed: boolean | null = null
let probing = false
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  if (!probing) {
    probing = true
    void probeDocs(() => api.health()).then((found) => {
      probed = found
      for (const tell of listeners) tell()
    })
  }
  return () => listeners.delete(listener)
}

/** A section of the documentation, and the name its "?" gives it (a panel's is its list's, never its row's). */
export interface DocsRef {
  target: DocsTarget
  section: string
}

/** Where `target` opens now: in the dashboard's language, this install's own copy when it has one (`docsBundled`). */
export function useDocsHref(target: DocsTarget): string {
  const { i18n } = useTranslation()
  const { me } = useMe()
  const found = useSyncExternalStore(subscribe, () => probed, () => null)
  return docsHref(target, i18n.resolvedLanguage, docsBundled(me, found))
}

/** "Documentation ↗", with the book: the page that explains the screen it is on. */
export function DocsPageLink({ target }: { target: DocsTarget }) {
  const { t } = useTranslation()
  const href = useDocsHref(target)
  return (
    <a href={href} target="_blank" rel="noreferrer" className="btn btn-ghost" data-docs={target}>
      <Icon name="book" size={14} />
      {t('docs.page')}
      <span aria-hidden="true">↗</span>
    </a>
  )
}

/** A "?" that opens a section of the documentation, its name said on hover and to a screen reader. */
export function DocsSectionLink({ target, section }: DocsRef) {
  const { t } = useTranslation()
  const href = useDocsHref(target)
  const label = t('docs.section', { section })
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      title={label}
      data-docs={target}
      className="-my-1 inline-grid size-6 flex-none place-items-center rounded-md text-ink-3 hover:bg-wash hover:text-ink"
    >
      <Icon name="question" size={14} />
    </a>
  )
}

/** A sub-section's title (an h3 in a section of the rules), with the "?" of its documentation beside it. */
export function TitleWithDocs({
  title,
  docs,
  className = 'text-ui font-medium text-ink',
}: {
  title: string
  docs: DocsTarget
  className?: string
}) {
  return (
    <div className="flex items-center gap-1">
      <h3 className={className}>{title}</h3>
      <DocsSectionLink target={docs} section={title} />
    </div>
  )
}

/**
 * "Why ↗" in a notice: the section that explains what happened. `about`, where several stand together (the flags of a
 * request), names what each explains to a screen reader: "Why: Withheld".
 */
export function DocsWhyLink({ target, about }: { target: DocsTarget; about?: string }) {
  const { t } = useTranslation()
  const href = useDocsHref(target)
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="link whitespace-nowrap"
      aria-label={about === undefined ? undefined : t('docs.whyAbout', { about })}
      data-docs={target}
    >
      {t('docs.why')}
      <span aria-hidden="true"> ↗</span>
    </a>
  )
}
