// A change to your settings that lowers protection waits for your approval: the card on the Approvals
// page where you approve it or undo it, and the notice on the Rules page while it waits.

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { ApiError, api } from '../api/client'
import type { DestinationChange, GuidanceChange, SettingsChange } from '../api/types'
import { changeOrigin } from '../lib/approvals'
import { useApprovals } from '../lib/approvals-context'
import { formatRelative } from '../lib/format'
import { useMe } from '../lib/me'
import { refusedWords } from '../lib/refused'
import { useFormat } from '../lib/useFormat'
import { DocsWhyLink } from './DocsLinks'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { errorText } from './ui'

/** Seconds since the epoch, ticking every 15 seconds while `active`. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now() / 1000), 15000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

/** The daemon's error, as a sentence. */
const sentence = (message: string) => message.charAt(0).toUpperCase() + message.slice(1)

/** The change to your settings waiting for you (at most one), each with its Approve and Undo buttons. */
export function PendingChanges() {
  const { changes } = useApprovals()
  const now = useNow(changes.length > 0)
  if (changes.length === 0) return null
  return (
    <div className="mb-4 space-y-3">
      {changes.map((change) => (
        <ChangeCard key={change.id} change={change} now={now} />
      ))}
    </div>
  )
}

function ChangeCard({ change, now }: { change: SettingsChange; now: number }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const toast = useToast()
  const { removeChange } = useApprovals()
  const { me, reloadConfig } = useMe()
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const titleId = `change-${change.id}`
  const server = Boolean(me?.server) // a Latet server has no config.yaml: an undone change leaves your settings as in use

  const decide = async (action: 'approve' | 'reject') => {
    if (busy) return
    setBusy(action)
    try {
      await api.decideChange(change.id, action)
      removeChange(change.id)
      if (action === 'approve') {
        reloadConfig()
        toast.show(t('approvals.change.approved'))
      } else {
        toast.show(server ? t('approvals.change.undoneServer') : t('approvals.change.undone'), 'info')
      }
    } catch (err) {
      const refused = refusedWords(t, err)
      if (refused !== null) {
        // Settled on the server and its settings not kept: none of it applies, and it is asked again once they are read.
        removeChange(change.id)
        toast.show(refused, 'fail')
      } else if (err instanceof ApiError && err.status === 409) {
        removeChange(change.id)
        toast.show(sentence(err.message), 'info')
      } else {
        toast.show(errorText(t, err), 'fail')
      }
    } finally {
      setBusy(null)
    }
  }

  return (
    <section aria-labelledby={titleId} className="rounded-lg border border-warn/60 bg-warn/10 p-4" data-settings-change>
      <div className="flex items-start gap-3">
        <span className="grid size-8 flex-none place-items-center rounded-md border border-warn/60 bg-surface text-ink" aria-hidden="true">
          <Icon name="rules" size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 id={titleId} className="text-title font-semibold text-ink">
              {t('approvals.change.title')}
            </h2>
            <span className="text-meta text-ink-3 tabular-nums">
              {t('approvals.arrived', { time: formatRelative(fmt.locale, Math.min(change.created, now), now) })}
            </span>
          </div>
          <p className="mt-1 text-body text-ink-2">
            {changeOrigin(t, change, server)} <DocsWhyLink target="why.settingsChange" />
          </p>

          {change.loosened.length > 0 && (
            <>
              <p className="mt-3 text-ui font-medium text-ink">{t('approvals.change.loosened')}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ui text-ink">
                {change.loosened.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            </>
          )}

          {(change.guidance ?? []).map((written, i) => (
            <GuidanceWritten key={i} change={written} />
          ))}

          {(change.destinations ?? []).map((moved) => (
            <DestinationMoved key={moved.name} change={moved} />
          ))}

          {change.keys.length > 0 && (
            <p className="mt-3 flex flex-wrap items-center gap-1.5 text-meta text-ink-2">
              {t('approvals.change.keys')}
              {change.keys.map((key) => (
                <code key={key} className="tag mono">
                  {key}
                </code>
              ))}
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="min-w-0 text-meta text-ink-2">{t('approvals.change.help')}</p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn"
                disabled={busy !== null}
                title={server ? t('approvals.change.undoHelpServer') : t('approvals.change.undoHelp')}
                onClick={() => void decide('reject')}
              >
                <Icon name="x" size={14} />
                {busy === 'reject' ? t('approvals.change.undoing') : t('approvals.change.undo')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy !== null}
                title={t('approvals.change.approveHelp')}
                onClick={() => void decide('approve')}
              >
                <Icon name="check" size={14} />
                {busy === 'approve' ? t('approvals.approving') : t('approvals.approve')}
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

const SIDES = ['before', 'after'] as const

/** The reviewer's guidance a change writes, before and after: free text, which could tell it to report nothing. */
function GuidanceWritten({ change }: { change: GuidanceChange }) {
  const { t } = useTranslation()
  return (
    <div className="mt-3">
      <p className="text-ui font-medium text-ink">
        {change.where === null ? t('approvals.change.guidance') : t('approvals.change.guidanceIn', { project: change.where })}
      </p>
      <div className="mt-1 grid gap-2 md:grid-cols-2">
        {SIDES.map((side) => (
          <div key={side} className="min-w-0">
            <p className="text-meta text-ink-3">{side === 'before' ? t('approvals.change.before') : t('approvals.change.after')}</p>
            {change[side] ? (
              <p className="well code max-h-[200px] overflow-auto px-3 py-2 text-ink">{change[side]}</p>
            ) : (
              <p className="text-meta text-ink-3">{t('approvals.change.noGuidance')}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

/** A destination a change moves or removes: where an agent's requests, and its keys, would go. */
function DestinationMoved({ change }: { change: DestinationChange }) {
  const { t } = useTranslation()
  return (
    <div className="mt-3">
      <p className="text-ui font-medium text-ink">{t('approvals.change.destination', { name: change.name })}</p>
      <div className="mt-1 grid gap-2 md:grid-cols-2">
        {SIDES.map((side) => (
          <div key={side} className="min-w-0">
            <p className="text-meta text-ink-3">{side === 'before' ? t('approvals.change.before') : t('approvals.change.after')}</p>
            <p className="mono text-ui break-all text-ink">{change[side] ?? t('approvals.change.destinationGone')}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

/** On the Rules page: a change waits, and the settings shown are still those in use. */
export function PendingChangeNotice() {
  const { t } = useTranslation()
  const { changes } = useApprovals()
  if (changes.length === 0) return null
  return (
    <p role="status" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
      <span className="flex items-start gap-2">
        <Icon name="info" size={15} className="mt-0.5 flex-none" />
        <span>
          {t('approvals.change.notice')} <DocsWhyLink target="why.settingsChange" />
        </span>
      </span>
      <Link to="/approvals#settings" className="btn btn-sm">
        {t('approvals.change.review')}
      </Link>
    </p>
  )
}
