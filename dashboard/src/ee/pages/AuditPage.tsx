// The server's administration log: literal metadata, with addresses visible to current administrators alone.
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { Icon } from '../../components/Icon'
import { EmptyState, ErrorState, Loading, PageHeader, Segmented } from '../../components/ui'
import { tk } from '../../i18n'
import { useFormat } from '../../lib/useFormat'
import { CompanySignInWay } from '../components/CompanyWay'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { actorLabel, auditQuery, auditTime, detailsText, filterProblem, newestFirst, validAuditLog } from '../lib/audit'
import { AuditResponseError } from '../lib/auditSession'
import { PAUSED_TEXT, PRESETS, RANGE_PROBLEM, pausedOf, presetRange, rangeProblem, type Preset } from '../lib/reports'
import { useAudit } from '../lib/useAudit'
import type { AuditLogView } from '../types'

export function AuditPage() {
  const { t } = useTranslation(), readOnly = useEnterpriseReadOnly()
  const [params, setParams] = useSearchParams(), today = useMemo(() => new Date(), [])
  const asked = params.get('period') ?? ''
  const preset: Preset = asked === 'custom' || (PRESETS as readonly string[]).includes(asked) ? asked as Preset : '7'
  const range = preset === 'custom' ? { from: params.get('from') ?? '', to: params.get('to') ?? '' } : presetRange(preset, today)
  const problem = rangeProblem(range.from, range.to)
  const action = params.get('action') ?? '', actor = params.get('actor') ?? ''
  const q = auditQuery(range, action, actor), invalidFilter = filterProblem(q), log = useAudit(q, readOnly)
  const paused = pausedOf(log.error)
  const malformed = log.data !== undefined && !validAuditLog(log.data)
  const data = !problem && !invalidFilter && !log.busy && !log.error && !malformed ? log.data : undefined
  const set = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) { if (value) next.set(key, value); else next.delete(key) }
    setParams(next, { replace: true })
  }
  const actions = data?.actions ?? [], actors = data?.actors ?? []
  return <>
    <PageHeader title={t('ee.audit.title')} description={t('ee.audit.description')} actions={data && !readOnly && log.canExport ? <>
      <button type="button" className="btn" onClick={() => void log.exportAs('csv')}><Icon name="download" size={14} />{t('ee.audit.exportCsv')}</button>
      <button type="button" className="btn" onClick={() => void log.exportAs('json')}><Icon name="download" size={14} />{t('ee.audit.exportJson')}</button>
    </> : undefined} />
    {log.company && <CompanySignInWay text={t('ee.audit.companyExport')} />}
    <div className="mb-3 flex flex-wrap items-end gap-3">
      <Segmented<Preset> label={t('ee.reports.period')} value={preset} onChange={value => set(value === 'custom' ? { period: value, ...range } : { period: value === '7' ? null : value, from: null, to: null })}
        options={[...PRESETS.map(p => ({ value: p, label: t('ee.reports.lastDays', { count: Number(p) }) })), { value: 'custom', label: t('ee.reports.dates') }]} />
      {preset === 'custom' && <>
        <label className="flex flex-col gap-1"><span className="label">{t('ee.reports.from')}</span><input type="date" min="2000-01-01" max="2999-12-31" className="field" value={range.from} onChange={e => set({ from: e.target.value })} /></label>
        <label className="flex flex-col gap-1"><span className="label">{t('ee.reports.to')}</span><input type="date" min="2000-01-01" max="2999-12-31" className="field" value={range.to} onChange={e => set({ to: e.target.value })} /></label>
      </>}
      <label className="flex flex-col gap-1"><span className="label">{t('ee.audit.filters.action')}</span>
        <select className="field w-[200px]" value={action} onChange={e => set({ action: e.target.value || null })}>
          <option value="">{t('ee.audit.filters.all')}</option>
          {action && !actions.includes(action) && <option value={action}>{action}</option>}
          {actions.map(value => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <label className="flex flex-col gap-1"><span className="label">{t('ee.audit.filters.actor')}</span>
        <select className="field w-[200px]" value={actor} onChange={e => set({ actor: e.target.value || null })}>
          <option value="">{t('ee.audit.filters.all')}</option>
          {actor && !actors.some(value => value.id === actor) && <option value={actor}>{actor}</option>}
          {actors.map(value => <option key={value.id} value={value.id}>{actorLabel(t, value)}</option>)}
        </select>
      </label>
    </div>
    <p className="mb-1 text-meta text-ink-3">{t('ee.audit.timezone')}</p>
    <p className="mb-5 text-meta text-ink-3">{t('ee.audit.filters.hint')}</p>
    {problem || invalidFilter ? <p role="alert" className="text-meta text-crit-ink">{t(problem ? RANGE_PROBLEM[problem] : tk('ee.audit.errors.filter'))}</p>
      : log.busy ? <Loading />
      : paused ? <div className="panel"><EmptyState icon="pause" title={t('ee.reports.paused.title')}>{t(PAUSED_TEXT[paused.why], { date: paused.expires ?? '' })}</EmptyState></div>
      : log.error || malformed || !data ? log.company ? null : <ErrorState error={auditError(malformed ? new AuditResponseError() : log.error, t)} onRetry={log.reload} />
      : <AuditTable log={data} addresses={log.addresses && data.addresses} />}
  </>
}
function auditError(error: unknown, t: (key: string) => string): Error {
  const key = error instanceof AuditResponseError ? tk('ee.audit.errors.malformed') : error instanceof ApiError
    ? error.status === 403 ? tk('ee.audit.errors.forbidden') : error.status === 413 ? tk('ee.audit.errors.large') : error.status === 429 ? tk('ee.audit.errors.busy') : tk('ee.audit.errors.failed') : tk('ee.audit.errors.failed')
  return new Error(t(key))
}
function AuditTable({ log, addresses }: { log: AuditLogView; addresses: boolean }) {
  const { t } = useTranslation(), fmt = useFormat()
  return <div className="space-y-3">
    {log.truncated && <p role="status" className="text-meta text-ink-3">{t('ee.audit.truncated')}</p>}
    {!log.entries.length ? <div className="panel"><EmptyState icon="audit" title={t('ee.audit.empty.title')}>{t('ee.audit.empty.body')}</EmptyState></div>
      : <div className="panel overflow-x-auto"><table className="data-table text-meta">
        <thead><tr><th scope="col">{t('ee.audit.columns.time')}</th><th scope="col">{t('ee.audit.columns.actor')}</th><th scope="col">{t('ee.audit.columns.action')}</th><th scope="col">{t('ee.audit.columns.target')}</th><th scope="col">{t('ee.audit.columns.details')}</th>{addresses && <th scope="col">{t('ee.audit.columns.address')}</th>}</tr></thead>
        <tbody>{newestFirst(log.entries).map(entry => <tr key={entry.id}>
          <td className="whitespace-nowrap text-ink-2">{auditTime(entry.ts, fmt.dateTime, t)}</td>
          <td className="text-ink">{actorLabel(t, entry.actor)}</td><td className="mono text-ink">{entry.action}</td>
          <td className="text-ink-2">{entry.target ? entry.target.name ?? entry.target.id : t('common.none')}</td>
          <td className="mono max-w-[32rem] break-words text-ink-3">{detailsText(entry.details)}</td>
          {addresses && <td className="mono whitespace-nowrap text-ink-3">{entry.address ?? t('common.none')}</td>}
        </tr>)}</tbody>
      </table></div>}
    <p className="text-meta text-ink-3">{t('ee.audit.note')}</p>
  </div>
}
