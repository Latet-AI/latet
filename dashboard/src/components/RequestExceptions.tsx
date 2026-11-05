// Your request exceptions (the request-coverage plan's P2-7, D3, D4, R-75): each lets through exactly what it names, on
// one destination's request profile, which Latet would refuse otherwise. The list with each one's state, the warning
// before one ends, revoking one (it raises protection: at once), the permanent mark; what a change that asks for one
// would let through and for whom, shown before you approve it; and what their uses are counted as while the daemon runs.
// Names alone: a header's, a query parameter's, a body member's, never a value of a request.

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { ExceptionChange, ExceptionComponent, ExceptionState, RequestException, RequestStatus } from '../api/types'
import { tk } from '../i18n'
import { exceptionUses, expiringSoon, ordered, withoutRevoked } from '../lib/exceptions'
import { useMe } from '../lib/me'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'
import { Icon, StatusGlyph } from './Icon'
import { useToast } from './Toast'
import { EmptyState, ErrorState, errorText, Loading } from './ui'

const COMPONENT: Record<ExceptionComponent['kind'], string> = {
  header: tk('exceptions.component.header'),
  query: tk('exceptions.component.query'),
  body: tk('exceptions.component.body'),
}

const ROLE: Record<ExceptionComponent['role'], string> = {
  credential: tk('exceptions.role.credential'),
  checked: tk('exceptions.role.checked'),
}

const REASON: Record<RequestException['reason'], string> = {
  'destination-credential': tk('exceptions.reason.destination-credential'),
  'custom-header': tk('exceptions.reason.custom-header'),
  'query-parameter': tk('exceptions.reason.query-parameter'),
  other: tk('exceptions.reason.other'),
}

const STATE_LABEL: Record<ExceptionState, string> = {
  pending: tk('exceptions.state.pending'),
  'needs approval again (profile revised)': tk('exceptions.state.revised'),
  expired: tk('exceptions.state.expiredLabel'),
  expiring: tk('exceptions.state.expiringLabel'),
  active: tk('exceptions.state.activeLabel'),
  permanent: tk('exceptions.permanent'),
  forbidden: tk('exceptions.state.forbidden'),
}

const STATE_GLYPH: Record<ExceptionState, 'ok' | 'warn' | 'fail'> = {
  pending: 'warn',
  'needs approval again (profile revised)': 'fail',
  expired: 'fail',
  expiring: 'warn',
  active: 'ok',
  permanent: 'warn',
  forbidden: 'fail',
}

const ISSUER: Record<NonNullable<RequestException['issuer']>, string> = {
  owner: tk('exceptions.issuer.owner'),
  person: tk('exceptions.issuer.person'),
  administration: tk('exceptions.issuer.administration'),
}

const HOW: Record<ExceptionChange['how'], string> = {
  added: tk('exceptions.change.added'),
  widened: tk('exceptions.change.widened'),
  'made permanent': tk('exceptions.change.madePermanent'),
  renewed: tk('exceptions.change.renewed'),
}

/** What an exception names, one component a line: its kind, its exact name, what Latet does with its value. */
function Components({ components }: { components: ExceptionComponent[] }) {
  const { t } = useTranslation()
  return (
    <ul className="space-y-0.5">
      {components.map((c) => (
        <li key={`${c.kind}:${c.name}`} className="min-w-0">
          <span className="text-ink-2">{t(COMPONENT[c.kind])}</span> <code className="mono break-all text-ink">{c.name}</code>
          {c.type && <span className="text-meta text-ink-3"> ({c.type})</span>}
          <span className="block text-meta text-ink-3">{t(ROLE[c.role])}</span>
        </li>
      ))}
    </ul>
  )
}

function Scope({ of }: { of: Pick<RequestException, 'scope' | 'project'> }) {
  const { t } = useTranslation()
  return <>{of.scope === 'project' ? t('exceptions.scope.project', { name: of.project ?? '' }) : t('exceptions.scope.identity')}</>
}

function StateCell({ item }: { item: RequestException }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const ends = item.ends ? fmt.day(item.ends) : null
  return (
    <span className="flex items-start gap-1.5">
      <span className="mt-0.5">
        <StatusGlyph status={STATE_GLYPH[item.state]} size={14} />
      </span>
      <span className="min-w-0">
        {item.permanent ? (
          <span className="tag border-warn/60 bg-warn/12 text-ink" data-permanent>
            {t('exceptions.permanent')}
          </span>
        ) : (
          <span className="font-medium text-ink">{t(STATE_LABEL[item.state])}</span>
        )}
        {item.permanent && item.state !== 'permanent' && (
          <span className="block font-medium text-ink">{t(STATE_LABEL[item.state])}</span>
        )}
        {item.state === 'expiring' && item.days_left !== null && (
          <span className="block text-meta text-ink-2">{t('exceptions.state.expiring', { count: item.days_left })}</span>
        )}
        {ends && item.state === 'active' && <span className="block text-meta text-ink-2">{t('exceptions.state.until', { date: ends })}</span>}
        {ends && item.state === 'expired' && <span className="block text-meta text-ink-2">{t('exceptions.state.ended', { date: ends })}</span>}
        {item.issuer && item.state !== 'pending' && (
          <span className="block text-meta text-ink-3" data-issuer={item.issuer}>
            {t(ISSUER[item.issuer])}
          </span>
        )}
        {item.state === 'pending' && (
          <Link to="/approvals?tab=settings" className="link block text-meta">
            {t('exceptions.state.approveThere')}
          </Link>
        )}
      </span>
    </span>
  )
}

/** The list, with each one's state; `onRevoke` takes an approved one away. */
export function ExceptionList({
  items,
  onRevoke,
  busy,
  server,
}: {
  items: RequestException[]
  onRevoke: (id: string) => void
  busy: string | null
  server: boolean
}) {
  const { t } = useTranslation()
  const soon = expiringSoon(items)
  return (
    <>
      {server && <p className="mb-2 max-w-[80ch] text-ui text-ink-2">{t('exceptions.yoursAlone')}</p>}
      {soon.length > 0 && (
        <p role="status" className="mb-3 flex max-w-[80ch] items-start gap-2 rounded-md border border-warn/60 bg-warn/12 px-3 py-2 text-ui text-ink">
          <StatusGlyph status="warn" size={16} />
          <span>{t('exceptions.expiryWarning', { count: soon.length })}</span>
        </p>
      )}
      {items.length === 0 ? (
        <div className="panel">
          <EmptyState icon="shield" title={t('exceptions.empty')} />
        </div>
      ) : (
        <div className="panel overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('exceptions.columns.what')}</th>
                <th scope="col">{t('exceptions.columns.where')}</th>
                <th scope="col">{t('exceptions.columns.who')}</th>
                <th scope="col">{t('exceptions.columns.state')}</th>
                <th scope="col">
                  <span className="sr-only">{t('exceptions.revoke')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {ordered(items).map((item, i) => (
                <tr key={item.id ?? `pending-${i}`}>
                  <td className="min-w-[220px]">
                    <Components components={item.components} />
                    <span className="mt-1 block text-meta text-ink-3">{t(REASON[item.reason])}</span>
                    {item.description && <span className="mt-1 block text-meta break-words text-ink-2">{item.description}</span>}
                  </td>
                  <td className="whitespace-nowrap">
                    <span className="mono text-ink">{item.destination}</span>
                    <span className="block mono text-meta text-ink-3">{item.profile}</span>
                    {item.id && <span className="block mono text-meta text-ink-3">{item.id}</span>}
                  </td>
                  <td className="min-w-[160px] text-ink-2">
                    <Scope of={item} />
                  </td>
                  <td className="min-w-[170px]">
                    <StateCell item={item} />
                  </td>
                  <td className="whitespace-nowrap text-right">
                    {item.id && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={busy !== null}
                        aria-label={t('exceptions.revokeOne', { id: item.id })}
                        onClick={() => item.id && onRevoke(item.id)}
                      >
                        <Icon name="x" size={13} />
                        {busy === item.id ? t('exceptions.revoking') : t('exceptions.revoke')}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/** What their uses are counted as since the daemon started (M1, D2): in memory alone, cleared at a restart; with the
 * history off, all that is kept of them. Nothing when the status cannot be read. */
export function ExceptionStatus({ status, recording }: { status: RequestStatus | null | undefined; recording: boolean }) {
  const { t } = useTranslation()
  if (!status) return null
  const { uses, overflow, full, server } = exceptionUses(status)
  return (
    <div className="mt-3 max-w-[80ch] space-y-1 text-meta text-ink-2" data-exception-status>
      <p>{t('exceptions.status.uses', { count: uses })}</p>
      {!recording && <p>{t('exceptions.status.historyOff')}</p>}
      {server === 'summed' && <p>{t('exceptions.status.summed')}</p>}
      {server === 'alone' && <p>{t('exceptions.status.instanceAlone')}</p>}
      {(overflow > 0 || full) && <p className="text-ink">{t('exceptions.status.overflow', { count: overflow })}</p>}
    </div>
  )
}

/** In a waiting change: each exception it would let through, and for whom, before you approve it (R-75). */
export function ExceptionsAsked({ items }: { items: ExceptionChange[] }) {
  const { t } = useTranslation()
  if (items.length === 0) return null
  return (
    <div className="mt-3">
      <p className="text-ui font-medium text-ink">{t('exceptions.change.title')}</p>
      <ul className="mt-1 space-y-2">
        {items.map((item, i) => (
          <li key={item.id ?? `asked-${i}`} className="rounded-md border border-line bg-surface px-3 py-2 text-ui">
            <p className="text-meta text-ink-3">
              {t(HOW[item.how])} · <span className="mono">{item.destination}</span> · <span className="mono">{item.profile}</span>
            </p>
            <div className="mt-1">
              <Components components={item.components} />
            </div>
            {item.scope === 'identity' ? (
              <p className="mt-2 flex items-start gap-1.5 font-medium text-ink" data-identity-warning>
                <StatusGlyph status="warn" size={14} />
                <span>{t('exceptions.change.identityWarning')}</span>
              </p>
            ) : (
              <p className="mt-2 text-ink-2">{t('exceptions.change.projectOnly', { name: item.project ?? '' })}</p>
            )}
            <p className="mt-1 text-meta text-ink-2">
              {item.permanent ? t('exceptions.change.permanent') : t('exceptions.change.lasts', { count: item.days })}
            </p>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The section of the Integrations page: the list, read again after a revoke. */
export function RequestExceptionsSection() {
  const { t } = useTranslation()
  const { me } = useMe()
  const toast = useToast()
  const data = useAsync('exceptions', (signal) => api.exceptions(signal))
  const [busy, setBusy] = useState<string | null>(null)
  const [gone, setGone] = useState<string[]>([])

  const revoke = async (id: string) => {
    if (busy) return
    setBusy(id)
    try {
      await api.revokeException(id)
      setGone((ids) => [...ids, id])
      toast.show(t('exceptions.revoked', { id }))
      data.reload()
    } catch (err) {
      toast.show(errorText(t, err), 'fail')
    } finally {
      setBusy(null)
    }
  }

  const items = data.data ? gone.reduce((left, id) => withoutRevoked(left, id), data.data.items) : []
  return (
    <section id="exceptions" aria-labelledby="exceptions-title" className="mt-8 scroll-mt-4">
      <h2 id="exceptions-title" className="mb-1 text-title font-semibold text-ink">
        {t('exceptions.title')}
      </h2>
      <p className="mb-3 max-w-[80ch] text-ui text-ink-2">{t('exceptions.help')}</p>
      {data.loading ? (
        <Loading />
      ) : !data.data ? (
        <ErrorState error={data.error} onRetry={data.reload} />
      ) : (
        <>
          <ExceptionList items={items} onRevoke={(id) => void revoke(id)} busy={busy} server={Boolean(me?.server)} />
          <ExceptionStatus status={me?.request_status} recording={me?.recording ?? true} />
        </>
      )}
    </section>
  )
}
