// The rules in tabs, as Rules shows them, the global ones and a project's: a search, the row of tabs, then the
// sections of the tab shown. A link to a section (`/rules#reviewer`, `/rules?project=<id>#project-data`) opens its tab
// and scrolls to it, with any search let go; a tab picked names its first section in the address and leaves the page
// where it is. The tabs not shown keep their sections, hidden, with what is being edited in them.

import { useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate } from 'react-router'
import { tk } from '../i18n'
import type { DocsTarget } from '../lib/docsLinks'
import { formatList } from '../lib/format'
import { matchingSections, tabIn, type RulesTab, type TabRow } from '../lib/rulesTabs'
import { Icon } from './Icon'
import { Section } from './rules'
import { tabIds, Tabs } from './Tabs'

/** The name of each tab of the global rules: a project's tabs that are theirs too have the same. */
export const TAB_LABEL: Record<RulesTab, string> = {
  protection: tk('rules.tabs.protection'),
  terms: tk('rules.tabs.terms'),
  recognizers: tk('rules.tabs.recognizers'),
  values: tk('rules.tabs.values'),
  policies: tk('rules.tabs.policies'),
  reviewers: tk('rules.tabs.reviewers'),
  advanced: tk('rules.tabs.advanced'),
}

/** The state of an address a tab was picked at: it names the tab's first section, and the page stays where it is. */
const PICKED = { rulesTab: true }

/** A section of the rules: the search reads its title and description. */
export interface RulesSection {
  title: string
  description: string
  body: ReactNode
  /** A table: the section's whole width, its title above. */
  wide?: boolean
  /** While the search holds text: how many of the table's rows it finds (the section shows when any). */
  found?: number
}

export interface RulesTabsState<T extends string> {
  /** The tab shown: the one that holds the section the address names, else the first. */
  active: T
  /** Shows the tab `id`, naming its first section in the address. */
  pick: (id: string) => void
  query: string
  setQuery: (query: string) => void
}

/**
 * Which of `tabs` shows, from the address, and what the search holds. `ready`: the sections are on the page, so that a
 * link to one scrolls to it (a page that reads its data first says so once it shows them).
 */
export function useRulesTabs<T extends string>(tabs: TabRow<T>, ready = true): RulesTabsState<T> {
  const location = useLocation()
  const navigate = useNavigate()
  const linked = location.hash.slice(1)
  const picked = (location.state as typeof PICKED | null)?.rulesTab === true
  const active = tabIn(tabs, linked)
  const [query, setQuery] = useState('')
  const arrival = `${location.key}${location.hash}`
  const [arrived, setArrived] = useState(arrival)
  if (arrival !== arrived) {
    setArrived(arrival)
    if (linked && !picked) setQuery('')
  }
  useEffect(() => {
    if (ready && linked && !picked) document.getElementById(linked)?.scrollIntoView({ block: 'start' })
  }, [ready, location.key, linked, picked])
  const pick = (id: string) => {
    const first = tabs.find((tab) => tab.id === id)?.anchors[0]
    if (first) void navigate({ search: location.search, hash: `#${first}` }, { replace: true, state: PICKED, preventScrollReset: true })
  }
  return { active, pick, query, setQuery }
}

/** The sections that show: while the search holds text, those it finds in every tab; else those of the tab shown. */
export function shownSections<T extends string, A extends string>(
  tabs: TabRow<T, A>,
  sections: Record<A, RulesSection>,
  active: T,
  query: string,
): Set<A> {
  if (query.trim() === '') return new Set(tabs.find((tab) => tab.id === active)?.anchors ?? [])
  const all = tabs.flatMap((tab) => tab.anchors.map((anchor) => ({ anchor, ...sections[anchor] })))
  const byTitle = matchingSections(all, query).map((section) => section.anchor)
  return new Set([...byTitle, ...all.filter((section) => (section.found ?? 0) > 0).map((section) => section.anchor)])
}

export function RulesTabs<T extends string, A extends string>({
  state,
  tabs,
  sections,
  labels,
  counts = {},
  dirty,
  idPrefix,
  disabled,
  docs,
}: {
  state: RulesTabsState<T>
  tabs: TabRow<T, A>
  /** Each section, by its anchor. */
  sections: Record<A, RulesSection>
  /** Where each section is explained in the documentation: a "?" beside its title. */
  docs?: Record<A, DocsTarget>
  /** Each tab's name. */
  labels: Record<T, string>
  /** How many rows the tab of a list holds, beside its name. */
  counts?: Partial<Record<T, number | string>>
  /** The tabs that hold unsaved changes: a dot each. */
  dirty: ReadonlySet<T>
  /** Of the ids of the tabs and their panels (`tabIds`). */
  idPrefix: string
  /** Nothing in the sections changes: a save runs, and the form it comes back to is what it sent. */
  disabled: boolean
}) {
  const { t, i18n } = useTranslation()
  const { active, pick, query, setQuery } = state
  const searching = query.trim() !== ''
  const shown = shownSections(tabs, sections, active, query)
  // The tabs whose tables hold rows the search finds, with how many: "Terms (1)".
  const found = tabs
    .map((tab) => ({ tab, count: tab.anchors.reduce((sum, anchor) => sum + (sections[anchor].found ?? 0), 0) }))
    .filter(({ count }) => count > 0)
    .map(({ tab, count }) => `${labels[tab.id]} (${count})`)

  return (
    <>
      {/* The search, then the tabs: while the search holds text, it finds sections in every tab, and the tabs go. */}
      <div className="mb-4 space-y-3">
        <div role="search" className="relative max-w-[420px]">
          <Icon name="search" size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-3" />
          <input
            type="search"
            className="field w-full pl-8"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={t('rules.search')}
            placeholder={t('rules.search')}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        {!searching && (
          <Tabs
            label={t('rules.tabs.label')}
            idPrefix={idPrefix}
            active={active}
            onChange={pick}
            tabs={tabs.map((tab) => ({ id: tab.id, label: labels[tab.id], count: counts[tab.id], dirty: dirty.has(tab.id) }))}
          />
        )}
        {searching && found.length > 0 && (
          <p className="text-ui text-ink-2" role="status">
            {t('rules.searchFound', { tabs: formatList(i18n.resolvedLanguage ?? 'en', found) })}
          </p>
        )}
      </div>

      {/* Every section, up to the save bar. Each tab's panel holds its sections; those of the other tabs stay, hidden,
          with what is being edited. */}
      <fieldset disabled={disabled} className="contents">
        <div className="flex flex-col gap-4">
          {tabs.map((tab) => {
            const ids = tabIds(idPrefix, tab.id)
            return (
              // While a search holds text, the tabs are hidden and each panel shows the sections it found.
              <div
                key={tab.id}
                id={ids.panel}
                role={searching ? undefined : 'tabpanel'}
                aria-labelledby={searching ? undefined : ids.tab}
                hidden={!tab.anchors.some((anchor) => shown.has(anchor))}
                className="flex flex-col gap-4 [&>section]:my-0"
              >
                {tab.anchors.map((anchor) => (
                  <Section
                    key={anchor}
                    anchor={anchor}
                    title={sections[anchor].title}
                    description={sections[anchor].description}
                    listed={false}
                    hidden={!shown.has(anchor)}
                    wide={sections[anchor].wide}
                    docs={docs?.[anchor]}
                  >
                    {sections[anchor].body}
                  </Section>
                ))}
              </div>
            )
          })}
          {searching && shown.size === 0 && (
            <p className="panel p-5 text-ui text-ink-2" role="status">
              {t('rules.searchNone')}
            </p>
          )}
        </div>
      </fieldset>
    </>
  )
}
