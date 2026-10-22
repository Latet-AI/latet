// The Live Reviewer's queue for the whole app: one stream, shared by the menu's count, the page
// title, the Live page and the Approvals page.

import { createContext, useCallback, useContext, useEffect, useEffectEvent, useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { api, type StreamState } from '../api/client'
import type { ApprovalEvent, Batch, LiveReviewSettings, SettingsChange } from '../api/types'
import { applyEvent, changeOrigin, EMPTY_QUEUE, waitingCount, withoutBatch, withoutChange, type QueueState } from './approvals'
import { AGENT_LABEL } from './labels'
import { useMe } from './me'
import { savedChangePending, whenStale } from './refused'
import { DEFAULT_LIVE } from './reviewer'

export interface ApprovalsState extends QueueState {
  stream: StreamState
  /** The global settings (GET /config), once loaded. */
  live: LiveReviewSettings | undefined
  /** Turns the Live Reviewer on or off in the global settings. */
  setLiveEnabled: (enabled: boolean) => Promise<void>
  /** Takes a batch out of the queue at once, after you decided on it. */
  remove: (id: string) => void
  /** Takes a change to your settings out at once, after you decided on it. */
  removeChange: (id: string) => void
  /** The batches and the change to your settings waiting: the menu's count and the tab title. */
  waiting: number
}

const ApprovalsContext = createContext<ApprovalsState>({
  ...EMPTY_QUEUE,
  stream: 'closed',
  live: undefined,
  setLiveEnabled: async () => undefined,
  remove: () => undefined,
  removeChange: () => undefined,
  waiting: 0,
})

export function useApprovals(): ApprovalsState {
  return useContext(ApprovalsContext)
}

export function ApprovalsProvider({ live, children }: { live: LiveReviewSettings | undefined; children: ReactNode }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { me, reloadConfig } = useMe()
  const [queue, setQueue] = useState<QueueState>(EMPTY_QUEUE)
  const [stream, setStream] = useState<StreamState>('connecting')
  // A switch just flipped shows its new state until the saved settings come back.
  const [override, setOverride] = useState<{ base: LiveReviewSettings | undefined; enabled: boolean } | null>(null)
  const shownLive = useMemo(
    () => (override && override.base === live ? { ...(live ?? DEFAULT_LIVE), enabled: override.enabled } : live),
    [override, live],
  )

  /** A browser notification, when this tab is in the background and you allowed them; a click opens `to`. */
  const notify = (title: string, body: string, tag: string, to: string) => {
    if (!document.hidden || typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    const notice = new Notification(title, { body, tag })
    notice.onclick = () => {
      window.focus()
      navigate(to)
      notice.close()
    }
  }
  const notifyBatch = (batch: Batch) => {
    const agent = AGENT_LABEL[batch.agent] ? t(AGENT_LABEL[batch.agent]) : batch.agent
    notify(t('approvals.notification.title'), t('approvals.notification.body', { agent, count: batch.items.length }), batch.id, '/approvals#texts')
  }
  const notifyChange = (change: SettingsChange) =>
    notify(t('approvals.change.notification'), changeOrigin(t, change, Boolean(me?.server)), change.id, '/approvals#settings')

  const onEvent = useEffectEvent((event: ApprovalEvent) => {
    setQueue((current) => applyEvent(current, event))
    if (event.type === 'pending') notifyBatch(event.batch)
    else if (event.type === 'change') notifyChange(event.change)
    // A change approved applies: the settings in use (the Live Reviewer's switch among them) changed.
    else if (event.type === 'change_settled' && event.state === 'approved') reloadConfig()
  })

  useEffect(() => api.approvalsStream((event) => onEvent(event), setStream), [])

  const count = waitingCount(queue)
  useEffect(() => {
    const name = t('app.name')
    document.title = count > 0 ? t('approvals.documentTitle', { count, name }) : name
  }, [count, t])

  const setLiveEnabled = useCallback(
    async (enabled: boolean) => {
      setOverride({ base: live, enabled })
      try {
        // Over the settings as they are now, with their etag: the Live Reviewer's other settings as they are there.
        await api.changeConfig((config) => ({ live_review: { ...(config.live_review ?? DEFAULT_LIVE), enabled } }))
        reloadConfig()
      } catch (error) {
        if (savedChangePending(error)) reloadConfig()
        else {
          setOverride(null)
          whenStale(error, reloadConfig) // the settings changed since they were read: the switch shows them as they are
        }
        throw error
      }
    },
    [live, reloadConfig],
  )

  const remove = useCallback((id: string) => setQueue((current) => withoutBatch(current, id)), [])
  const removeChange = useCallback((id: string) => setQueue((current) => withoutChange(current, id)), [])

  const value = useMemo<ApprovalsState>(
    () => ({ ...queue, stream, live: shownLive, setLiveEnabled, remove, removeChange, waiting: count }),
    [queue, stream, shownLive, setLiveEnabled, remove, removeChange, count],
  )
  return <ApprovalsContext.Provider value={value}>{children}</ApprovalsContext.Provider>
}
