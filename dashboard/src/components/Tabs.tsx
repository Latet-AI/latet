// A row of tabs (`.tabs`, `.tab` in index.css): one is selected; Tab reaches the selected one, and the arrow keys,
// Home and End move between them, selecting as they go. The page shows the panel of the selected tab, with the ids
// `tabIds` gives, so that each tab is tied to its panel.

import { useEffect, useRef, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'

export interface TabItem {
  id: string
  label: string
  /** Something in the tab is changed and not saved: a dot, and the words for a screen reader. */
  dirty?: boolean
  /** How many things the tab holds, shown beside its label (a number, or words such as "4 yours · 38 in all"). */
  count?: number | string
  /** The count in the warm tone of a value as it is on your machine: something there asks for your attention. */
  countTone?: 'warm'
}

/** The ids of the tab `id` of the row `prefix`, and of its panel. */
export const tabIds = (prefix: string, id: string) => ({ tab: `${prefix}-tab-${id}`, panel: `${prefix}-panel-${id}` })

const MOVES: Record<string, (index: number, count: number) => number> = {
  ArrowRight: (index, count) => (index + 1) % count,
  ArrowLeft: (index, count) => (index - 1 + count) % count,
  Home: () => 0,
  End: (_, count) => count - 1,
}

export function Tabs({
  tabs,
  active,
  onChange,
  label,
  idPrefix = 'tabs',
}: {
  tabs: readonly TabItem[]
  active: string
  onChange: (id: string) => void
  /** What the row of tabs is, for a screen reader. */
  label: string
  /** Of the ids of the tabs and their panels (`tabIds`): one per page. */
  idPrefix?: string
}) {
  const { t } = useTranslation()
  const row = useRef<HTMLDivElement>(null)
  const buttons = useRef<(HTMLButtonElement | null)[]>([])
  const selectedIndex = tabs.findIndex((tab) => tab.id === active)
  // In a row too wide for the window, which scrolls on its own, the selected tab is brought into sight: sideways only,
  // so that a link to a section further down the page keeps its place.
  useEffect(() => {
    const list = row.current
    const button = buttons.current[selectedIndex]
    if (!list || !button) return
    const box = list.getBoundingClientRect()
    const tab = button.getBoundingClientRect()
    if (tab.left < box.left) list.scrollLeft -= box.left - tab.left
    else if (tab.right > box.right) list.scrollLeft += tab.right - box.right
  }, [selectedIndex])
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const to = MOVES[event.key]
    if (!to || event.altKey || event.ctrlKey || event.metaKey) return
    event.preventDefault()
    const next = to(index, tabs.length)
    const tab = tabs[next]
    if (!tab) return
    buttons.current[next]?.focus()
    if (tab.id !== active) onChange(tab.id)
  }
  return (
    <div ref={row} role="tablist" aria-label={label} className="tabs">
      {tabs.map((tab, index) => {
        const selected = tab.id === active
        const ids = tabIds(idPrefix, tab.id)
        return (
          <button
            key={tab.id}
            ref={(button) => {
              buttons.current[index] = button
            }}
            type="button"
            role="tab"
            id={ids.tab}
            aria-selected={selected}
            aria-controls={ids.panel}
            tabIndex={selected ? 0 : -1}
            data-dirty={tab.dirty ? '' : undefined}
            className="tab"
            onClick={() => {
              if (!selected) onChange(tab.id)
            }}
            onKeyDown={(event) => move(event, index)}
          >
            {/* A space before the count and the dot: what a screen reader says keeps the words apart ("Texts 2"). */}
            {tab.label}
            {tab.count !== undefined && (
              <>
                {' '}
                <span
                  data-tone={tab.countTone}
                  className={`ml-0.5 inline-grid h-[18px] min-w-[18px] place-items-center rounded-full px-1.5 align-middle text-micro font-semibold tabular-nums ${
                    tab.countTone === 'warm' ? 'bg-real-low text-real-ink' : 'bg-wash-2 text-ink-2'
                  }`}
                >
                  {tab.count}
                </span>
              </>
            )}
            {tab.dirty && (
              <>
                {' '}
                <span className="ml-0.5 inline-block size-1.5 rounded-full bg-real align-middle" title={t('tabs.unsaved')} aria-hidden="true" />
                <span className="sr-only">{t('tabs.unsaved')}</span>
              </>
            )}
          </button>
        )
      })}
    </div>
  )
}
