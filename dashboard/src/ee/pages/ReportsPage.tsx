// Reports (Enterprise, on a server): what Latet did for the company over a period, by group, project, agent, API key
// and kind of value, with the period before for the trend; people only as the company chose. Counts only, never a value.

import { useMemo } from 'react'
import { ApiError } from '../../api/client'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router'
import { agentName } from '../../components/badges'
import { BarList, ChartCard, ColumnChart, DataTable, StatTile, type ColumnDatum } from '../../components/charts'
import { Icon } from '../../components/Icon'
import { EmptyState, ErrorState, Loading, PageHeader, Segmented } from '../../components/ui'
import { tk } from '../../i18n'
import { toneOf } from '../../lib/labels'
import { useTypeClass } from '../../lib/me'
import { useFormat } from '../../lib/useFormat'
import { CompanySignInWay } from '../components/CompanyWay'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { useReports } from '../lib/useReports'
import {
  FILTERS,
  PAUSED_TEXT,
  PEOPLE_NOTE,
  PRESETS,
  RANGE_PROBLEM,
  pausedOf,
  presetRange,
  rangeProblem,
  reasonLabel,
  reportQuery,
  splitOutcomes,
  trendOf,
  hasResidual,
  type Filter,
  type Preset,
} from '../lib/reports'
import type { Report, ReportCounts, ReportResidual, ReportRow } from '../types'

const FILTER_LABEL: Record<Filter, string> = {
  group: tk('ee.reports.filters.group'),
  project: tk('ee.reports.filters.project'),
  agent: tk('ee.reports.filters.agent'),
  category: tk('ee.reports.filters.category'),
}
const CHARTS = [
  { key: 'exchanges', title: tk('ee.reports.charts.exchanges'), help: tk('ee.reports.charts.exchangesHelp'), color: 'var(--data)' },
  { key: 'masked', title: tk('ee.reports.charts.masked'), help: tk('ee.reports.charts.maskedHelp'), color: 'var(--data)' },
  { key: 'stopped', title: tk('ee.reports.charts.stopped'), help: tk('ee.reports.charts.stoppedHelp'), color: 'var(--crit)' },
] as const

export function ReportsPage() {
  const { t } = useTranslation()
  const readOnly = useEnterpriseReadOnly()
  const [params, setParams] = useSearchParams()
  const today = useMemo(() => new Date(), [])
  const asked = params.get('period') ?? ''
  const preset: Preset = asked === 'custom' || (PRESETS as readonly string[]).includes(asked) ? (asked as Preset) : '30'
  const range = preset === 'custom' ? { from: params.get('from') ?? '', to: params.get('to') ?? '' } : presetRange(preset, today)
  const problem = rangeProblem(range.from, range.to)
  const filters = Object.fromEntries(FILTERS.map((name) => [name, params.get(name)])) as Record<Filter, string | null>
  const q = reportQuery(range, filters)
  const report = useReports(q, readOnly)
  const paused = pausedOf(report.error)
  const data = !problem && !report.busy && !report.error ? report.data : undefined

  const set = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setParams(next, { replace: true })
  }

  return (
    <>
      <PageHeader
        title={t('ee.reports.title')}
        description={t('ee.reports.description')}
        actions={
          <>
            {data?.viewer.siem && (
              <Link to="/reports/siem" className="btn">
                <Icon name="integrations" size={14} />
                {t('ee.reports.siemLink')}
              </Link>
            )}
            {data && !readOnly && <><button type="button" className="btn" onClick={() => void report.exportAs('csv')}>
              <Icon name="download" size={14} />
              {t('ee.reports.exportCsv')}
            </button>
            <button type="button" className="btn" onClick={() => void report.exportAs('json')}>
              <Icon name="download" size={14} />
              {t('ee.reports.exportJson')}
            </button></>}
          </>
        }
      />
      {report.company && <CompanySignInWay text={t('ee.reports.companyExport')} />}
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Segmented<Preset>
          label={t('ee.reports.period')}
          value={preset}
          onChange={(value) =>
            set(value === 'custom' ? { period: value, ...range } : { period: value === '30' ? null : value, from: null, to: null })
          }
          options={[
            ...PRESETS.map((p) => ({ value: p, label: t('ee.reports.lastDays', { count: Number(p) }) })),
            { value: 'custom' as const, label: t('ee.reports.dates') },
          ]}
        />
        {preset === 'custom' && (
          <>
            <label className="flex flex-col gap-1">
              <span className="label">{t('ee.reports.from')}</span>
              <input type="date" min="2000-01-01" max="2999-12-31" className="field" value={range.from} onChange={(e) => set({ from: e.target.value })} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="label">{t('ee.reports.to')}</span>
              <input type="date" min="2000-01-01" max="2999-12-31" className="field" value={range.to} onChange={(e) => set({ to: e.target.value })} />
            </label>
          </>
        )}
        {FILTERS.map((name) => (
          <label key={name} className="flex flex-col gap-1">
            <span className="label">{t(FILTER_LABEL[name])}</span>
            <select className="field w-[170px]" value={data ? filters[name] ?? '' : ''} disabled={!data} onChange={(e) => set({ [name]: e.target.value || null })}>
              <option value="">{t('ee.reports.filters.all')}</option>
              {choices(data, name, t).map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      {problem ? (
        <p role="alert" className="text-meta text-crit-ink">
          {t(RANGE_PROBLEM[problem])}
        </p>
      ) : report.busy ? (
        <Loading />
      ) : paused ? (
        <div className="panel">
          <EmptyState icon="pause" title={t('ee.reports.paused.title')}>
            {t(PAUSED_TEXT[paused.why], { date: paused.expires ?? '' })}
          </EmptyState>
        </div>
      ) : report.error || !data ? (
        report.company ? null : <ErrorState error={reportError(report.error, t)} onRetry={report.reload} />
      ) : (
        <div>
          <ReportView report={data} />
        </div>
      )}
    </>
  )
}

/** What a filter offers: what the period holds; a value chosen that it no longer holds stays listed. */
function choices(report: Report | undefined, name: Filter, t: (key: string) => string): { value: string; label: string }[] {
  if (!report) return []
  const { options, filters } = report
  const list =
    name === 'group'
      ? options.groups.map((group) => ({ value: group.id, label: group.name }))
      : name === 'agent'
        ? options.agents.map((agent) => ({ value: agent, label: agentName(t, agent) }))
        : (name === 'project' ? options.projects : options.categories).map((value) => ({ value, label: value }))
  const chosen = filters[name]
  return chosen && !list.some((choice) => choice.value === chosen) ? [{ value: chosen, label: chosen }, ...list.slice(0, 199)] : list.slice(0, 200)
}

function reportError(error: unknown, t: (key: string) => string): Error {
  const key = error instanceof ApiError
    ? error.status === 403 ? tk('ee.reports.errors.forbidden') : error.status === 413 ? tk('ee.reports.errors.large') : error.status === 429 ? tk('ee.reports.errors.busy') : tk('ee.reports.errors.failed')
    : tk('ee.reports.errors.failed')
  return new Error(t(key))
}

function approvers(report: Report, t: (key: string, options: { count: number }) => string) {
  const residual = report.others.by_approver
  return [...(report.by_approver ?? []), ...(residual && hasResidual(residual) ? [{ ...residual, key: t('ee.reports.others', { count: residual.count }) }] : [])]
}

export function ReportView({ report }: { report: Report }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const classOf = useTypeClass()
  const { totals, before } = report
  const decisions = splitOutcomes(totals.approvals, ['approved'])
  const changes = splitOutcomes(totals.changes, ['approved'])
  const decided = decisions.through + decisions.other + changes.through + changes.other

  if (totals.exchanges === 0 && totals.masked === 0 && totals.stopped === 0 && decided === 0 && totals.uncounted === 0 && !Object.values(totals.apps).some(count => count > 0)) {
    return (
      <div className="panel">
        <EmptyState icon="stats" title={t('ee.reports.empty.title')}>
          {t('ee.reports.empty.body')}
        </EmptyState>
      </div>
    )
  }

  const trend = (now: number, then: number) => {
    const moved = trendOf(now, then)
    if (moved.direction === 'new') return t('ee.reports.trend.new')
    if (moved.direction === 'same') return t('ee.reports.trend.same')
    const words = moved.direction === 'up' ? tk('ee.reports.trend.up') : tk('ee.reports.trend.down')
    return t(words, { value: fmt.percent(moved.ratio ?? 0) })
  }
  const weekly = report.period.step === 'week'
  const short = !weekly && report.period.days <= 7
  // These three charts are narrow. Keep both endpoints and two evenly spaced dates; full dates remain
  // in every point's accessible label, tooltip and table row. Short weekday labels fit all seven days.
  const axisDates = new Set([0, 1, 2, 3].map((part) => Math.round(part * (report.series.length - 1) / 3)))
  const series = (pick: (counts: ReportCounts) => number): ColumnDatum[] =>
    report.series.map((point, index) => ({
      key: point.key,
      label: short ? fmt.day(point.key, 'weekday') : axisDates.has(index) ? fmt.day(point.key) : '',
      longLabel: weekly ? t('ee.reports.charts.weekOf', { day: fmt.day(point.key) }) : `${fmt.day(point.key, 'weekday')} ${fmt.day(point.key)}`,
      value: pick(point),
    }))
  const head = weekly ? t('ee.reports.charts.week') : t('ee.reports.charts.day')

  return (
    <div className="space-y-5">
      {totals.uncounted > 0 && <p className="text-meta text-ink-3">{t('ee.reports.uncounted', { count: totals.uncounted })}</p>}
      <div className="grid grid-cols-5 gap-3 max-xl:grid-cols-2 max-sm:grid-cols-1">
        <StatTile label={t('ee.reports.tiles.exchanges')} value={fmt.compact(totals.exchanges)}>
          {trend(totals.exchanges, before.exchanges)}
        </StatTile>
        <StatTile label={t('ee.reports.tiles.masked')} value={fmt.compact(totals.masked)}>
          {trend(totals.masked, before.masked)}
        </StatTile>
        <StatTile label={t('ee.reports.tiles.stopped')} value={fmt.number(totals.stopped)}>
          {totals.exchanges === 0
            ? t('common.none')
            : t('ee.reports.tiles.stoppedShare', { value: fmt.percent(totals.stopped / totals.exchanges, 1) })}
        </StatTile>
        {Object.keys(totals.apps).length > 0 && (
          <StatTile label={t('ee.reports.tiles.apps')} value={fmt.compact(Object.values(totals.apps).reduce((a, n) => a + n, 0))}>
            {t('ee.reports.tiles.appsHelp', { mask: fmt.number(totals.apps.mask ?? 0), scan: fmt.number(totals.apps.scan ?? 0) })}
          </StatTile>
        )}
        {decided > 0 && (
          <StatTile label={t('ee.reports.tiles.decisions')} value={fmt.number(decisions.through + decisions.other)}>
            {t('ee.reports.tiles.decisionsHelp', {
              approved: fmt.number(decisions.through),
              refused: fmt.number(decisions.other),
              changes: fmt.number(changes.through + changes.other),
            })}
          </StatTile>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3 max-xl:grid-cols-1">
        {CHARTS.map((chart) => {
          const data = series((counts) => counts[chart.key])
          return (
            <ChartCard
              key={chart.key}
              title={t(chart.title)}
              subtitle={t(chart.help)}
              table={<DataTable head={[head, t(chart.title)]} rows={data.map((d) => [d.longLabel, fmt.number(d.value)] as [string, string])} />}
            >
              <ColumnChart data={data} color={chart.color} format={(v) => fmt.number(v)} measure={t(chart.title)} />
            </ChartCard>
          )
        })}
      </div>

      <div className="grid grid-cols-2 gap-3 max-xl:grid-cols-1">
        <Breakdown title={t('ee.reports.charts.byGroup')} rows={report.by_group} residual={report.others.by_group} label={(row) => row.label ?? t('ee.reports.noGroup')} />
        <Breakdown title={t('ee.reports.charts.byProject')} rows={report.by_project} residual={report.others.by_project} label={(row) => row.label ?? t('ee.reports.noProject')} />
        <Breakdown title={t('ee.reports.charts.byAgent')} rows={report.by_agent} residual={report.others.by_agent} label={(row) => agentName(t, row.key)} />
        <Breakdown title={t('ee.reports.charts.byKey')} rows={report.by_key} residual={report.others.by_key} label={(row) => row.label ?? row.key} />
        {report.by_person && (
          <Breakdown title={t('ee.reports.charts.byPerson')} rows={report.by_person} residual={report.others.by_person} label={(row) => row.label ?? row.key} />
        )}
      </div>

      {report.by_approver && (report.by_approver.length > 0 || hasResidual(report.others.by_approver)) && (
        <ChartCard
          title={t('ee.reports.charts.byApprover')}
          subtitle={t('ee.reports.charts.byApproverHelp')}
          table={
            <ReportTable
              head={[t('ee.reports.charts.name'), t('ee.reports.charts.approvals'), t('ee.reports.charts.changes')]}
              rows={approvers(report, t).map((r) => [r.key, fmt.number(r.approvals), fmt.number(r.changes)])}
            />
          }
        >
          <BarList
            rows={approvers(report, t).map((r) => ({
              key: r.key,
              label: r.key,
              value: r.approvals + r.changes,
              title: t('ee.reports.charts.decidedHelp', { approvals: fmt.number(r.approvals), changes: fmt.number(r.changes) }),
            }))}
            format={(v) => fmt.number(v)}
            labelWidth={170}
          />
        </ChartCard>
      )}

      <div className="grid grid-cols-2 gap-3 max-xl:grid-cols-1">
        <ChartCard
          title={t('ee.reports.charts.byCategory')}
          table={
            <DataTable
              head={[t('ee.reports.charts.category'), t('ee.reports.charts.values')]}
              rows={report.by_category.map((r) => [r.key, fmt.number(r.masked)] as [string, string])}
            />
          }
        >
          {report.by_category.length === 0 ? (
            <p className="text-meta text-ink-3">{t('ee.reports.none')}</p>
          ) : (
            <BarList
              rows={report.by_category.slice(0, 12).map((r) => ({
                key: r.key,
                label: <span className="mono">{r.key}</span>,
                value: r.masked,
                tone: toneOf(classOf(r.key)),
              }))}
              format={(v) => fmt.number(v)}
              labelWidth={170}
            />
          )}
        </ChartCard>
        <ChartCard
          title={t('ee.reports.charts.byReason')}
          table={
            <DataTable
              head={[t('ee.reports.charts.reason'), t('ee.reports.charts.count')]}
              rows={report.stopped_by_reason.map((r) => [t(reasonLabel(r.key)), fmt.number(r.count)] as [string, string])}
            />
          }
        >
          {report.stopped_by_reason.length === 0 ? (
            <p className="text-meta text-ink-3">{t('ee.reports.noneStopped')}</p>
          ) : (
            <BarList
              rows={report.stopped_by_reason.map((r) => ({ key: r.key, label: t(reasonLabel(r.key)), value: r.count }))}
              format={(v) => fmt.number(v)}
              labelWidth={220}
            />
          )}
        </ChartCard>
      </div>

      <p className="text-meta text-ink-3">{t(PEOPLE_NOTE[report.people])}</p>
      <p className="text-meta text-ink-3">{t('ee.reports.typeCodes')}</p>
    </div>
  )
}

/** One breakdown: its first 12 rows as bars by requests, and its table twin with every row. */
function Breakdown({ title, rows, residual, label }: { title: string; rows: readonly ReportRow[]; residual: ReportResidual | null; label: (row: ReportRow) => string }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const other = residual && hasResidual(residual) ? [{ key: '\u0000others', label: t('ee.reports.others', { count: residual.count }), ...residual }] : []
  const name = (row: ReportRow) => row.key === '\u0000others' ? row.label ?? '' : label(row)
  return (
    <ChartCard
      title={title}
      subtitle={t('ee.reports.charts.breakdownHelp')}
      table={
        <ReportTable
          head={[t('ee.reports.charts.name'), t('ee.reports.charts.exchanges'), t('ee.reports.charts.masked'), t('ee.reports.charts.stopped')]}
          rows={[...rows, ...other].map((row) => [name(row), fmt.number(row.exchanges), fmt.number(row.masked), fmt.number(row.stopped)])}
        />
      }
    >
      {rows.length === 0 && other.length === 0 ? (
        <p className="text-meta text-ink-3">{t('ee.reports.none')}</p>
      ) : (
        <BarList
          rows={[...rows.slice(0, 12), ...other].map((row) => ({
            key: row.key || '-',
            label: name(row),
            value: row.exchanges,
            title: t('ee.reports.charts.rowHelp', { masked: fmt.number(row.masked), stopped: fmt.number(row.stopped) }),
          }))}
          format={(v) => fmt.number(v)}
          labelWidth={170}
        />
      )}
    </ChartCard>
  )
}

/** Ranked tables retain every count, including the residual's different approver columns. */
function ReportTable({ head, rows }: { head: string[]; rows: string[][] }) {
  return <div className="max-h-[260px] overflow-auto"><table className="data-table text-meta">
    <thead><tr>{head.map((text, index) => <th key={text} scope="col" className={index ? 'num' : ''}>{text}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{row.map((text, column) => <td key={column} className={column ? 'num text-ink' : 'text-ink-2'}>{text}</td>)}</tr>)}</tbody>
  </table></div>
}
