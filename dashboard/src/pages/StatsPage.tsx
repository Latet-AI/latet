import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Agent, Stats } from '../api/types'
import { agentName } from '../components/badges'
import { BarList, ChartCard, ColumnChart, DataTable, Meter, StatTile, type ColumnDatum } from '../components/charts'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, Loading, PageHeader, Segmented } from '../components/ui'
import { CLASS_LABEL, CLASS_ORDER, CLASS_TONE, toneOf, type ViewClass } from '../lib/labels'
import { useDeploymentKey, useTypeClass } from '../lib/me'
import { fillDays, OTHER, rankEntries, totals } from '../lib/stats'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'

const PERIODS = ['1', '7', '14', '30'] as const
type Period = (typeof PERIODS)[number]

export function StatsPage() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const raw = params.get('days')
  const period: Period = PERIODS.includes(raw as Period) ? (raw as Period) : '7'
  const days = Number(period)
  const stats = useAsync(`stats:${days}`, (signal) => api.stats(days, signal))

  return (
    <>
      <PageHeader title={t('stats.title')} description={t('stats.description')} docs="page.stats" />
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Segmented<Period>
          label={t('stats.period')}
          value={period}
          onChange={(value) => setParams(value === '7' ? {} : { days: value }, { replace: true })}
          options={PERIODS.map((p) => ({ value: p, label: p === '1' ? t('stats.today') : t('stats.lastDays', { count: Number(p) }) }))}
        />
      </div>
      {stats.loading ? (
        <Loading />
      ) : !stats.data ? (
        <ErrorState error={stats.error} onRetry={stats.reload} />
      ) : (
        <div className={`transition-opacity ${stats.stale ? 'opacity-60' : ''}`}>
          <StatsView stats={stats.data} days={days} />
        </div>
      )}
    </>
  )
}

function StatsView({ stats, days }: { stats: Stats; days: number }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const classOf = useTypeClass()
  const filled = useMemo(() => fillDays(stats.by_day, days, new Date()), [stats.by_day, days])
  const sum = totals(filled)

  if (sum.exchanges === 0) {
    return (
      <div className="panel">
        <EmptyState icon="stats" title={t('stats.empty.title')}>
          {t('stats.empty.body')}
        </EmptyState>
      </div>
    )
  }

  const series = (pick: (d: (typeof filled)[number]) => number): ColumnDatum[] =>
    filled.map((d) => ({
      key: d.day,
      label: days <= 7 ? fmt.day(d.day, 'weekday') : fmt.day(d.day),
      longLabel: `${fmt.day(d.day, 'weekday')} ${fmt.day(d.day)}`,
      value: pick(d),
    }))
  const labelEvery = filled.length > 16 ? 4 : filled.length > 8 ? 2 : 1
  const exchanges = series((d) => d.exchanges)
  const masked = series((d) => d.masked)
  const blocked = series((d) => d.blocked)
  const tableRows = (data: ColumnDatum[]) => data.map((d) => [d.longLabel, fmt.number(d.value)] as [string, string])

  const types = rankEntries(stats.by_type, 12)
  const typeClasses = new Set<ViewClass>(types.filter((r) => r.key !== OTHER).map((r) => classOf(r.key)))
  const agents = rankEntries(stats.by_agent)
  const agentTotal = agents.reduce((n, a) => n + a.value, 0)

  return (
    <div className="space-y-5">
      {stats.partial && (
        // The period holds more exchanges than one read counts: the numbers are those of the latest.
        <p role="note" className="flex items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-ui text-ink-2" data-stats-partial>
          <Icon name="info" size={15} className="mt-0.5 flex-none" />
          {t('stats.partial', { latest: fmt.number(stats.latest ?? 0) })}
        </p>
      )}
      <div className="grid grid-cols-4 gap-3 max-xl:grid-cols-2 max-sm:grid-cols-1">
        <StatTile label={t('stats.tiles.exchanges')} value={fmt.compact(sum.exchanges)}>
          {t('stats.tiles.perDay', {
            value: fmt.number(sum.exchanges / filled.length, { maximumFractionDigits: sum.exchanges / filled.length < 10 ? 1 : 0 }),
          })}
        </StatTile>
        <StatTile label={t('stats.tiles.masked')} value={fmt.compact(sum.masked)}>
          {t('stats.tiles.maskedHelp')}
        </StatTile>
        <StatTile label={t('stats.tiles.blocked')} value={fmt.number(sum.blocked)}>
          {sum.blocked === 0 ? t('stats.tiles.blockedNone') : t('stats.tiles.blockedShare', { value: fmt.percent(sum.blocked / sum.exchanges, 1) })}
        </StatTile>
        <StatTile label={t('stats.tiles.cache')} value={stats.cache_read_ratio === null ? t('common.none') : fmt.percent(stats.cache_read_ratio)}>
          {stats.cache_read_ratio !== null ? (
            <div className="space-y-1.5">
              <Meter ratio={stats.cache_read_ratio} label={t('stats.tiles.cache')} />
              <p>{t('stats.tiles.cacheHelp')}</p>
            </div>
          ) : (
            t('stats.tiles.cacheNone')
          )}
        </StatTile>
      </div>

      {days > 1 && (
      <div className="grid grid-cols-3 gap-3 max-xl:grid-cols-1">
        <ChartCard
          title={t('stats.charts.exchanges')}
          subtitle={t('stats.charts.exchangesHelp')}
          table={<DataTable head={[t('stats.charts.day'), t('stats.charts.exchangesMeasure')]} rows={tableRows(exchanges)} />}
        >
          <ColumnChart data={exchanges} color="var(--data)" format={(v) => fmt.number(v)} measure={t('stats.charts.exchangesMeasure')} labelEvery={labelEvery} />
        </ChartCard>
        <ChartCard
          title={t('stats.charts.masked')}
          subtitle={t('stats.charts.maskedHelp')}
          table={<DataTable head={[t('stats.charts.day'), t('stats.charts.maskedMeasure')]} rows={tableRows(masked)} />}
        >
          <ColumnChart data={masked} color="var(--data)" format={(v) => fmt.number(v)} measure={t('stats.charts.maskedMeasure')} labelEvery={labelEvery} />
        </ChartCard>
        <ChartCard
          title={t('stats.charts.blocked')}
          subtitle={t(key('stats.charts.blockedHelp'))}
          table={<DataTable head={[t('stats.charts.day'), t('stats.charts.blockedMeasure')]} rows={tableRows(blocked)} />}
        >
          <ColumnChart data={blocked} color="var(--crit)" format={(v) => fmt.number(v)} measure={t('stats.charts.blockedMeasure')} labelEvery={labelEvery} />
        </ChartCard>
      </div>
      )}

      <div className="grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-3 max-xl:grid-cols-1">
        <ChartCard
          title={t('stats.charts.byType')}
          subtitle={
            <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {CLASS_ORDER.filter((c) => typeClasses.has(c)).map((c) => (
                <span key={c} className="inline-flex items-center gap-1.5">
                  <span className="swatch" data-tone={CLASS_TONE[c]} aria-hidden="true" />
                  {t(CLASS_LABEL[c])}
                </span>
              ))}
            </span>
          }
          table={
            <DataTable
              head={[t('stats.charts.type'), t('stats.charts.maskedMeasure')]}
              rows={types.map((r) => [r.key === OTHER ? t('stats.other') : r.key, fmt.number(r.value)] as [string, string])}
            />
          }
        >
          {types.length === 0 ? (
            <p className="text-ui text-ink-3">{t('stats.noneMasked')}</p>
          ) : (
            <BarList
              rows={types.map((r) => {
                const cls = r.key === OTHER ? null : classOf(r.key)
                return {
                  key: r.key,
                  label: r.key === OTHER ? t('stats.other') : <span className="mono">{r.key}</span>,
                  value: r.value,
                  tone: toneOf(cls),
                  title: cls ? t(CLASS_LABEL[cls]) : undefined,
                }
              })}
              format={(v) => fmt.number(v)}
              labelWidth={170}
            />
          )}
        </ChartCard>
        <div className="grid content-start gap-3">
          <ChartCard
            title={t('stats.charts.byAgent')}
            table={
              <DataTable
                head={[t('stats.charts.agent'), t('stats.charts.exchangesMeasure')]}
                rows={agents.map((r) => [agentName(t, r.key as Agent), fmt.number(r.value)] as [string, string])}
              />
            }
          >
            <BarList
              rows={agents.map((r) => ({
                key: r.key,
                label: agentName(t, r.key as Agent),
                value: r.value,
                title: agentTotal ? fmt.percent(r.value / agentTotal) : undefined,
              }))}
              format={(v) => (agentTotal ? t('stats.withShare', { value: fmt.number(v), share: fmt.percent(v / agentTotal) }) : fmt.number(v))}
              labelWidth={110}
              valueWidth={96}
            />
          </ChartCard>
          <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
            <StatTile label={t('stats.tiles.latency')} value={stats.latency_ms.p50 === null ? t('common.none') : fmt.duration(stats.latency_ms.p50)}>
              {stats.latency_ms.p95 !== null && t('stats.tiles.p95', { value: fmt.duration(stats.latency_ms.p95) })}
            </StatTile>
            <StatTile label={t('stats.tiles.overhead')} value={stats.overhead_ms.p50 === null ? t('common.none') : fmt.duration(stats.overhead_ms.p50)}>
              {stats.overhead_ms.p95 !== null && t('stats.tiles.p95', { value: fmt.duration(stats.overhead_ms.p95) })}
            </StatTile>
          </div>
        </div>
      </div>
    </div>
  )
}
