// Company approvals (Latet Enterprise): what waits for you to decide for your company, the changes that lower
// protection and the texts of the Live Reviewer, and what was decided lately. For the people who decide something:
// the security team, a key's controllers, the admins. A decider's own requests show too, marked: they may withdraw
// them, never approve them. Another person's text, values included, opens only in a session the company's sign-in
// opened [D6]; once a trial ended, nothing is decided and no text is opened.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../../api/client'
import type { ApprovalDecision } from '../../api/types'
import { AgentLabel } from '../../components/badges'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toast'
import { EmptyState, ErrorState, errorText, Loading, PageHeader } from '../../components/ui'
import { TeamReviewsBanner } from '../../components/WhoDecides'
import { tk } from '../../i18n'
import { approveBody, selectedBatch, type AddedMask } from '../../lib/approvals'
import { formatRelative } from '../../lib/format'
import type { ImageChoice } from '../../lib/images'
import { useMe } from '../../lib/me'
import { companySignInNeeded } from '../../lib/refused'
import { useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { BatchView, NotifyButton } from '../../pages/ApprovalsPage'
import { approvalsApi } from '../api'
import { CompanySignInWay } from '../components/CompanyWay'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import {
  companyAnswer,
  companyOrigin,
  decidedByWords,
  decidedWhat,
  floorReasons,
  loosenedLines,
  ROUTE_LABEL,
  sameReading,
  STATE_LABEL,
  waitingItems,
  type CompanyAnswer,
} from '../lib/company-approvals'
import { useCompanyApprovals } from '../lib/company-approvals-context'
import type { Asked, CompanyBatchRow, CompanyChange, Decided } from '../types'

/** What the notifications button says it notifies of, here: something new waiting for your decision. */
const NOTIFY_HELP = tk('ee.companyApprovals.notify.help')
const NOTIFY_ON_HELP = tk('ee.companyApprovals.notify.onHelp')

/** The words of a text masked again since it was read (409 `changed`), as `companyAnswer` gives them. */
const CHANGED_KEY = tk('ee.companyApprovals.answer.changed')

/** Seconds since the epoch, ticking every 15 seconds. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 15000)
    return () => clearInterval(timer)
  }, [])
  return now
}

/** An answer of the deciders' routes, in the reader's language: by its code, else as any error is said. */
function useAnswerWords() {
  const { t } = useTranslation()
  const fmt = useFormat()
  return (error: unknown, answer: CompanyAnswer | null, name: string) =>
    answer ? t(answer.key, { name, reasons: fmt.list(floorReasons(error)) }) : errorText(t, error)
}

export function CompanyApprovalsPage() {
  const { t } = useTranslation()
  const { queue, error, reload, watch } = useCompanyApprovals()
  useEffect(() => watch(), [watch]) // the queue is asked while this page is open, and only then (R39)
  const history = useAsync('ee.approvals.history', (signal) => approvalsApi.history(signal))
  const now = useNow()
  // What was decided lately follows the queue: an item that left it (decided elsewhere, withdrawn) is read there again.
  const listed = queue ? waitingItems(queue).map((item) => item.id).join(' ') : null
  const shown = useRef<string | null>(null)
  const reloadHistory = history.reload
  useEffect(() => {
    if (listed === null) return
    if (shown.current !== null && shown.current !== listed) reloadHistory()
    shown.current = listed
  }, [listed, reloadHistory])
  const header = (
    <PageHeader
      title={t('ee.companyApprovals.title')}
      description={t('ee.companyApprovals.description')}
      actions={<NotifyButton help={NOTIFY_HELP} onHelp={NOTIFY_ON_HELP} />}
    />
  )
  if (!queue) {
    return (
      <>
        {header}
        <div className="panel">{error ? <ErrorState error={error} onRetry={reload} /> : <Loading />}</div>
      </>
    )
  }
  const decided = () => {
    reload()
    history.reload()
  }
  const empty = queue.changes.length === 0 && queue.batches.length === 0
  return (
    <>
      {header}
      {error !== undefined && (
        // The queue was read once and its last read failed: what shows may have changed meanwhile (a role taken away,
        // an instance out of reach), never shown as if it were live.
        <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border border-warn/60 bg-warn/10 px-4 py-2.5 text-ui text-ink">
          <p className="min-w-0">{t('ee.companyApprovals.readFailed')}</p>
          <button type="button" className="btn btn-sm" onClick={reload}>
            <Icon name="refresh" size={14} />
            {t('common.retry')}
          </button>
        </div>
      )}
      {empty && (
        <div className="panel mb-6">
          <EmptyState icon="approvals" title={t('ee.companyApprovals.empty.title')}>
            {t('ee.companyApprovals.empty.body')}
          </EmptyState>
        </div>
      )}
      {queue.changes.length > 0 && (
        <section aria-labelledby="company-changes" className="mb-6 space-y-3">
          <h2 id="company-changes" className="text-title font-semibold text-ink">
            {t('ee.companyApprovals.changes.title')}
          </h2>
          {queue.changes.map((change) => (
            <CompanyChangeCard key={change.id} change={change} now={now} onDecided={decided} />
          ))}
        </section>
      )}
      {queue.batches.length > 0 && <CompanyBatches batches={queue.batches} now={now} onDecided={decided} />}
      {queue.more > 0 && (
        // Past the 200 listed: they show as these are decided (oldest first).
        <p role="status" className="mb-6 flex items-start gap-2 text-ui text-ink-2">
          <Icon name="info" size={15} className="mt-0.5 flex-none" />
          {t('ee.companyApprovals.more', { count: queue.more })}
        </p>
      )}
      <History items={history.data?.items} error={history.error} onRetry={history.reload} />
    </>
  )
}

/** Who asked: their name (an API key's says so), their group, who decides, when. */
function AskedBy({ item, now }: { item: Asked; now: number }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  return (
    <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-ui text-ink-2">
      <span className="font-semibold text-ink">{item.identity.name}</span>
      {item.identity.kind === 'key' && <span className="tag">{t('ee.companyApprovals.apiKey')}</span>}
      {item.group && <span>{t('ee.companyApprovals.group', { group: item.group })}</span>}
      <span className="tag">{t(ROUTE_LABEL[item.route])}</span>
      <span className="text-meta text-ink-3 tabular-nums">
        {t('approvals.arrived', { time: formatRelative(fmt.locale, Math.min(item.created, now), now) })}
      </span>
    </p>
  )
}

function CompanyChangeCard({ change, now, onDecided }: { change: CompanyChange; now: number; onDecided: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const words = useAnswerWords()
  const readOnly = useEnterpriseReadOnly() // a trial ended: read, not decided
  const { reload: reloadMe } = useMe()
  const { remove } = useCompanyApprovals()
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [company, setCompany] = useState(false) // the decision asked for a session the company's sign-in opened
  const name = change.identity.name

  const decide = async (action: 'approve' | 'reject') => {
    if (busy) return
    setBusy(action)
    setCompany(false)
    try {
      const { state } = await approvalsApi.decideChange(change.id, action)
      remove(change.id)
      if (change.mine) toast.show(t('ee.companyApprovals.withdrawn'), 'info')
      else if (state === 'approved') toast.show(t('ee.companyApprovals.approved', { name }))
      else toast.show(t('ee.companyApprovals.rejected', { name }), 'info')
      onDecided()
    } catch (err) {
      if (companySignInNeeded(err)) {
        setCompany(true) // decision (c): said with the way to the company sign-in, no toast repeats it
        return
      }
      const answer = companyAnswer(err)
      toast.show(words(err, answer, name), answer?.tone ?? 'fail')
      if (answer?.ended) {
        remove(change.id)
        onDecided()
      }
      if (answer?.reread) reloadMe() // a trial ended meanwhile: the page turns read-only
    } finally {
      setBusy(null)
    }
  }

  const loosened = loosenedLines(t, change.loosened)
  return (
    <article className="panel p-4" data-company-change>
      <AskedBy item={change} now={now} />
      <p className="mt-1 text-body text-ink-2">{companyOrigin(t, change)}</p>
      {change.asked_by && (
        <p className="mt-1 text-meta text-ink-2">
          {t('ee.companyApprovals.askedBy', { name: change.asked_by.name ?? change.asked_by.id })}
        </p>
      )}
      <p className="mt-3 text-ui font-medium text-ink">{t('ee.companyApprovals.changes.loosened')}</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ui text-ink">
        {loosened.map((reason, i) => (
          <li key={i}>{reason}</li>
        ))}
      </ul>
      {change.keys.length > 0 && (
        <p className="mt-3 flex flex-wrap items-center gap-1.5 text-meta text-ink-2">
          {t('ee.companyApprovals.changes.keys')}
          {change.keys.map((key) => (
            <code key={key} className="tag mono">
              {key}
            </code>
          ))}
        </p>
      )}
      {company && (
        <div className="mt-4">
          <CompanySignInWay text={t('ee.companyApprovals.companySignInDecide')} />
        </div>
      )}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 text-meta text-ink-2">{change.mine && t('ee.companyApprovals.mine')}</p>
        {/* A trial ended (useEnterpriseReadOnly): read, not decided: no button at all. Your own: withdrawn, never approved. */}
        {!readOnly && (
          <div className="flex flex-wrap gap-2">
            {change.mine ? (
              <button type="button" className="btn" disabled={busy !== null} title={t('ee.companyApprovals.withdrawHelp')} onClick={() => void decide('reject')}>
                <Icon name="x" size={14} />
                {t('ee.companyApprovals.withdraw')}
              </button>
            ) : (
              <>
                <button type="button" className="btn" disabled={busy !== null} title={t('ee.companyApprovals.rejectHelp')} onClick={() => void decide('reject')}>
                  <Icon name="x" size={14} />
                  {t('ee.companyApprovals.reject')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy !== null}
                  title={t('ee.companyApprovals.approveHelp')}
                  onClick={() => void decide('approve')}
                >
                  <Icon name="check" size={14} />
                  {busy === 'approve' ? t('approvals.approving') : t('ee.companyApprovals.approve')}
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </article>
  )
}

function CompanyBatches({ batches, now, onDecided }: { batches: CompanyBatchRow[]; now: number; onDecided: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const words = useAnswerWords()
  const readOnly = useEnterpriseReadOnly() // a trial ended: read, not decided
  const { reload: reloadMe } = useMe()
  const { remove } = useCompanyApprovals()
  const [selection, setSelection] = useState<{ id: string; index: number } | null>(null)
  const [masks, setMasks] = useState<Record<string, AddedMask[]>>({})
  const [images, setImages] = useState<Record<string, Record<number, ImageChoice>>>({})
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState<{ id: string; message: string } | null>(null)
  const [company, setCompany] = useState(false) // a decision asked for a session the company's sign-in opened [D6]
  const list = useRef<HTMLUListElement>(null)
  const row = selectedBatch(batches, selection)
  // The text itself, read when it is opened (R14): the queue holds none.
  // Not once a trial ended (R61): a text is read to decide on it, and nobody decides then.
  const text = useAsync(row && !readOnly ? `ee.approvals.batch.${row.id}` : null, (signal) => approvalsApi.batch(row!.id, signal))
  // A read refused because a trial ended meanwhile: /me is read again, and the page turns read-only.
  const trialEnded = companyAnswer(text.error)?.reread === true
  useEffect(() => {
    if (trialEnded) reloadMe()
  }, [trialEnded, reloadMe])
  if (!row) return null
  const name = row.identity.name
  // The text on screen: the last reading of this one, never an older copy over a read that failed since (useAsync keeps
  // the data it had when a read fails).
  const opened = !text.error && text.data?.id === row.id ? text.data : null
  const readAnswer = companyAnswer(text.error)

  const decide = async (action: 'approve' | 'reject') => {
    if (busy) return
    setBusy(action)
    setError(null)
    setCompany(false)
    try {
      let body: ApprovalDecision & { version?: string } = { action: 'reject' }
      if (action === 'approve') {
        // Read again first, and approved only as it is on screen (the review of Task 10, I1): masked again meanwhile
        // (the asking person's rules or vault changed), it shows anew with a notice, and this click approves nothing.
        // The server checks the version too (409 `changed`), past this read.
        const now = await approvalsApi.batch(row.id)
        if (!opened || !sameReading(opened, now)) {
          text.mutate(() => now)
          setError({ id: row.id, message: t('ee.companyApprovals.answer.changed') })
          return
        }
        body = { ...approveBody(masks[row.id] ?? [], images[row.id]), version: now.version }
      }
      await approvalsApi.decideBatch(row.id, body)
      remove(row.id)
      if (row.mine) toast.show(t('ee.companyApprovals.textWithdrawn'), 'info')
      else if (action === 'approve') toast.show(t('ee.companyApprovals.textApproved', { name }))
      else toast.show(t('ee.companyApprovals.textRejected', { name }), 'info')
      onDecided()
    } catch (err) {
      const answer = companyAnswer(err)
      if (companySignInNeeded(err)) {
        setCompany(true) // [D6]: said with the way to the company sign-in, no toast repeats it
      } else if (answer?.ended) {
        remove(row.id)
        toast.show(words(err, answer, name), answer.tone)
        onDecided()
      } else if (answer) {
        setError({ id: row.id, message: words(err, answer, name) })
        if (answer.reread) reloadMe() // a trial ended meanwhile: the page turns read-only
        if (answer.key === CHANGED_KEY) text.reload() // masked again past the read above: read anew, shown as it is
      } else if (err instanceof ApiError && err.status === 422) {
        setError({ id: row.id, message: t('approvals.refused', { message: err.message }) }) // the mask's own problem
      } else {
        setError({ id: row.id, message: errorText(t, err) })
      }
    } finally {
      setBusy(null)
    }
  }

  // The arrow keys move through the texts waiting, as on Approvals.
  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0
    if (!step) return
    event.preventDefault()
    const index = batches.indexOf(row) + step
    const next = batches[index]
    if (!next) return
    setSelection({ id: next.id, index })
    list.current?.querySelectorAll<HTMLElement>('[role="option"]')[index]?.focus()
  }

  return (
    <section aria-labelledby="company-batches" className="mb-6">
      <h2 id="company-batches" className="mb-3 text-title font-semibold text-ink">
        {t('ee.companyApprovals.batches.title')}
      </h2>
      {company && <CompanySignInWay text={t('ee.companyApprovals.companySignIn')} />}
      <div className="grid grid-cols-[300px_minmax(0,1fr)] items-start gap-4 max-xl:grid-cols-1">
        <ul
          ref={list}
          role="listbox"
          aria-label={t('ee.companyApprovals.batches.label')}
          className="panel max-h-[70vh] overflow-y-auto"
          onKeyDown={onKeyDown}
        >
          {batches.map((batch, index) => (
            <li
              key={batch.id}
              role="option"
              aria-selected={batch === row}
              tabIndex={batch === row ? 0 : -1}
              onClick={() => setSelection({ id: batch.id, index })}
              className={`cursor-pointer border-b border-line px-3 py-2.5 last:border-b-0 ${batch === row ? 'bg-wash-2' : 'hover:bg-wash'}`}
            >
              <AskedBy item={batch} now={now} />
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                <AgentLabel agent={batch.agent} />
                {batch.mine && <span className="tag border-warn/60">{t('ee.companyApprovals.mineTag')}</span>}
              </div>
            </li>
          ))}
        </ul>
        {opened ? (
          <div className="min-w-0" data-company-text>
            <BatchView
              key={row.id}
              batch={opened}
              masks={masks[row.id] ?? []}
              setMasks={(update) => setMasks((all) => ({ ...all, [row.id]: update(all[row.id] ?? []) }))}
              images={images[row.id] ?? {}}
              setImage={(item, choice) => setImages((all) => ({ ...all, [row.id]: { ...all[row.id], [item]: choice } }))}
              projectName={null}
              now={now}
              busy={busy}
              error={error?.id === row.id ? error.message : null}
              onDecide={(action) => void decide(action)}
              stopOnly={row.mine}
              hideActions={readOnly}
              imageUrl={(item, view, mark) => approvalsApi.imageUrl(row.id, item, view, mark)}
              canKeep={false} // "Always mask" would change another person's terms
              banner={
                <div className="space-y-2">
                  <div className="panel px-3 py-2.5">
                    <AskedBy item={row} now={now} />
                  </div>
                  {row.mine && <TeamReviewsBanner deciders={opened.deciders} />}
                </div>
              }
            />
          </div>
        ) : (
          <div className="panel">
            {readOnly ? (
              <p className="px-4 py-6 text-ui text-ink-2">{t('ee.companyApprovals.textAfterTrial')}</p>
            ) : companySignInNeeded(text.error) ? (
              <div className="p-4">
                <CompanySignInWay quiet text={t('ee.companyApprovals.companySignIn')} /> {/* [D6] */}
              </div>
            ) : readAnswer ? (
              <div role="alert" className="flex flex-col items-start gap-3 px-4 py-6">
                <p className="text-ui text-ink-2">{words(text.error, readAnswer, name)}</p>
                {!readAnswer.ended && (
                  <button type="button" className="btn" onClick={text.reload}>
                    <Icon name="refresh" size={14} />
                    {t('common.retry')}
                  </button>
                )}
              </div>
            ) : text.error ? (
              <ErrorState error={text.error} onRetry={text.reload} />
            ) : (
              <Loading />
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function History({ items, error, onRetry }: { items: Decided[] | undefined; error: unknown; onRetry: () => void }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  let body
  if (items === undefined) body = error ? <ErrorState error={error} onRetry={onRetry} /> : <Loading />
  else if (items.length === 0) body = <p className="px-4 py-3 text-ui text-ink-3">{t('ee.companyApprovals.history.empty')}</p>
  else {
    body = (
      <div className="overflow-x-auto">
        <table className="data-table w-full">
          <thead>
            <tr>
              <th>{t('ee.companyApprovals.history.when')}</th>
              <th>{t('ee.companyApprovals.history.what')}</th>
              <th>{t('ee.companyApprovals.history.who')}</th>
              <th>{t('ee.companyApprovals.history.decision')}</th>
              <th>{t('ee.companyApprovals.history.by')}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td className="tabular-nums">{fmt.dateTime(item.decided)}</td>
                <td>
                  {decidedWhat(t, item)}
                  {item.loosened && item.loosened.length > 0 && (
                    <span className="block text-meta text-ink-3">{loosenedLines(t, item.loosened).join('; ')}</span>
                  )}
                </td>
                <td>
                  {item.identity.name}
                  {item.identity.kind === 'key' && <span className="tag ml-2">{t('ee.companyApprovals.apiKey')}</span>}
                </td>
                <td>{t(STATE_LABEL[item.state])}</td>
                <td className={item.by ? undefined : 'text-ink-3'}>{decidedByWords(t, item)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  return (
    <section aria-labelledby="company-history" className="panel">
      <h2 id="company-history" className="border-b border-line px-4 py-2.5 text-title font-semibold text-ink">
        {t('ee.companyApprovals.history.title')}
      </h2>
      {body}
    </section>
  )
}
