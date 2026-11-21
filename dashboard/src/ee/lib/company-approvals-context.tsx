// What waits for you to decide, for the whole app (Latet Enterprise), for the people who decide something only,
// through whichever instance answers: the queue asked every few seconds while the Company approvals page is open, the
// menu's count asked every half minute otherwise (R39), and a browser notification when something new arrives in a
// background tab. Once a trial ended nobody decides (the page is read, not changed): no count and no notification.

import { createContext, useCallback, useContext, useEffect, useEffectEvent, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { enterpriseView } from '../../lib/edition'
import { useMe } from '../../lib/me'
import { approvalsApi } from '../api'
import type { CompanyQueue } from '../types'
import { arrivals, companyReads, companyWaiting, countDelay, menuCount, pollDelay, withoutItem } from './company-approvals'

/** The page's id in `/me`'s `edition.pages`: there for the people who decide something. */
export const COMPANY_PAGE = 'company-approvals'

export interface CompanyApprovalsState {
  /** Null until the first answer, while the page is closed, and for someone who decides nothing. */
  queue: CompanyQueue | null
  error: unknown
  reload: () => void
  /** Takes an item out at once, after you decided on it. */
  remove: (id: string) => void
  /** What waits for you to decide: the menu's count (none once a trial ended: nobody decides then). */
  waiting: number
  /** The page asks for the queue while it is open: called when it opens, it returns what stops it. */
  watch: () => () => void
}

const CompanyApprovalsContext = createContext<CompanyApprovalsState>({
  queue: null,
  error: undefined,
  reload: () => undefined,
  remove: () => undefined,
  waiting: 0,
  watch: () => () => undefined,
})

export function useCompanyApprovals(): CompanyApprovalsState {
  return useContext(CompanyApprovalsContext)
}

export function CompanyApprovalsProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { me } = useMe()
  const view = enterpriseView(me, COMPANY_PAGE)
  const [queue, setQueue] = useState<CompanyQueue | null>(null)
  const [error, setError] = useState<unknown>(undefined)
  const [round, setRound] = useState(0)
  const [watchers, setWatchers] = useState(0)
  const [counted, setCounted] = useState(0)
  // Which reads run (companyReads): the queue while the page is open (read-only too), else the count while they
  // decide; a notification only while they decide.
  const reads = companyReads(view, watchers > 0)
  const watching = reads.queue
  const counting = reads.count
  const deciding = reads.notify
  const seen = useRef<Set<string> | null>(null)
  const lastCount = useRef<number | null>(null)

  /** While the page is closed, a browser notification when the count grows in a background tab: how many, no name. */
  const notifyCount = useEffectEvent((next: number) => {
    const before = lastCount.current
    lastCount.current = next
    if (before === null || next <= before || !document.hidden) return
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    const body = t('nav.ee.companyWaiting', { count: next })
    const notice = new Notification(t('ee.companyApprovals.notification.title'), { body, tag: COMPANY_PAGE })
    notice.onclick = () => {
      window.focus()
      navigate('/company-approvals')
      notice.close()
    }
  })

  // The menu's count while the page is closed: one small read every half minute (a minute in a background tab).
  useEffect(() => {
    if (!counting) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const ask = () => {
      approvalsApi
        .count(controller.signal)
        .then(
          ({ waiting }) => {
            setCounted(waiting)
            notifyCount(waiting)
          },
          () => undefined, // the count stays as it was; the page says why when opened
        )
        .finally(() => {
          if (!controller.signal.aborted) timer = setTimeout(ask, countDelay(document.hidden))
        })
    }
    ask()
    return () => {
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [counting, round])

  /** A browser notification for what arrived, when this tab is in the background and you allowed them: who asked, and
   * whether it is a change or a text; never what it holds. */
  const notify = useEffectEvent((next: CompanyQueue) => {
    const fresh = arrivals(seen.current, next)
    seen.current = new Set([...next.changes, ...next.batches].map((item) => item.id))
    const first = fresh[0]
    if (!deciding || !first || !document.hidden || typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    const body =
      'loosened' in first
        ? t('ee.companyApprovals.notification.change', { name: first.identity.name })
        : t('ee.companyApprovals.notification.batch', { name: first.identity.name })
    const notice = new Notification(t('ee.companyApprovals.notification.title'), { body, tag: first.id })
    notice.onclick = () => {
      window.focus()
      navigate('/company-approvals')
      notice.close()
    }
  })

  // The queue, only while the page is open (what it read while last open is cleared as it opens again: `watch`).
  useEffect(() => {
    if (!watching) {
      seen.current = null
      return
    }
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const ask = () => {
      approvalsApi
        .queue(controller.signal)
        .then(
          (next) => {
            setQueue(next)
            setError(undefined)
            notify(next)
            lastCount.current = companyWaiting(next)
            setCounted(lastCount.current) // the menu's count as the page closes, until the next count
          },
          (err: unknown) => {
            if (!controller.signal.aborted) setError(err)
          },
        )
        .finally(() => {
          if (!controller.signal.aborted) timer = setTimeout(ask, pollDelay(document.hidden))
        })
    }
    ask()
    return () => {
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [watching, round])

  const reload = useCallback(() => setRound((n) => n + 1), [])
  const remove = useCallback((id: string) => setQueue((current) => (current ? withoutItem(current, id) : current)), [])
  const watch = useCallback(() => {
    // What was read while the page was last open is not shown again: it waits for the first answer.
    setQueue(null)
    setError(undefined)
    setWatchers((n) => n + 1)
    return () => setWatchers((n) => n - 1)
  }, [])
  const current = watching ? queue : null
  const waiting = menuCount(view, current, counted)
  const value = useMemo<CompanyApprovalsState>(
    () => ({ queue: current, error: watching ? error : undefined, reload, remove, waiting, watch }),
    [current, watching, error, reload, remove, waiting, watch],
  )
  return <CompanyApprovalsContext.Provider value={value}>{children}</CompanyApprovalsContext.Provider>
}
