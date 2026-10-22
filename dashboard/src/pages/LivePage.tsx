import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { api, type StreamState } from '../api/client'
import type { ExchangeSummary, ReviewRecord } from '../api/types'
import { AgentLabel, ClassStrip, FlagList, ModeBadge, TransportStatus, TypeChips } from '../components/badges'
import { DocsWhyLink } from '../components/DocsLinks'
import { Icon } from '../components/Icon'
import { ClearHistoryButton } from '../components/ClearHistory'
import { LiveReviewSwitch } from '../components/LiveReviewEditor'
import { CommandLine, EmptyState, ErrorState, errorText, Loading, PageHeader } from '../components/ui'
import { CMD_CLAUDE, CMD_CODEX } from '../lib/commands'
import { hasFlag } from '../lib/flags'
import { foundAfterSending, lateTarget } from '../lib/reviewer'
import { useApprovals } from '../lib/approvals-context'
import { AGENT_LABEL, classCounts, FILTER_FLAGS, filterAgents, FLAG_LABEL, MODE_HELP, MODES, typeClassesFor } from '../lib/labels'
import { useDeploymentKey, useMe, useTypeClass } from '../lib/me'
import { cacheReadRatio } from '../lib/stats'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'

interface Filters {
  agent: string
  flag: string
  session: string
  project: string
}

const MAX_ROWS = 500

function matches(exchange: ExchangeSummary, filters: Filters): boolean {
  if (filters.agent && exchange.agent !== filters.agent) return false
  if (filters.session && exchange.session !== filters.session) return false
  if (filters.project && exchange.project?.id !== filters.project) return false
  return hasFlag(exchange.flags, filters.flag)
}

function StreamIndicator({ state, paused }: { state: StreamState; paused: boolean }) {
  const { t } = useTranslation()
  const label = paused
    ? t('live.stream.paused')
    : state === 'open'
      ? t('live.stream.open')
      : state === 'retrying'
        ? t('live.stream.retrying')
        : t('live.stream.connecting')
  const color = paused ? 'bg-ink-3' : state === 'open' ? 'bg-good' : state === 'retrying' ? 'bg-warn' : 'bg-line-2'
  return (
    <span className="inline-flex items-center gap-2 text-meta text-ink-2" role="status">
      <span className={`size-2 rounded-full ${color} ${state === 'open' && !paused ? 'live-dot' : ''}`} aria-hidden="true" />
      {label}
    </span>
  )
}

export function LivePage() {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { me } = useMe()
  const waiting = useApprovals().batches.length
  const [params, setParams] = useSearchParams()
  const filters: Filters = {
    agent: params.get('agent') ?? '',
    flag: params.get('flag') ?? '',
    session: params.get('session') ?? '',
    project: params.get('project') ?? '',
  }
  // While paused, rows that arrive after this moment wait (and are counted) instead of showing.
  const [pausedAt, setPausedAt] = useState<number | null>(null)
  const paused = pausedAt !== null
  const [generation, setGeneration] = useState(0)
  const sessions = useAsync('sessions', (signal) => api.sessions(100, signal))
  const projects = useAsync('projects', (signal) => api.projects(signal))

  const setFilter = (key: keyof Filters, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }

  const filterKey = `${filters.agent}|${filters.flag}|${filters.session}|${filters.project}|${generation}`
  const anyFilter = Boolean(filters.agent || filters.flag || filters.session || filters.project)

  return (
    <>
      <PageHeader
        title={t('live.title')}
        description={t(key('live.description'))}
        docs="page.live"
        actions={
          <>
            <LiveReviewSwitch compact />
            <ClearHistoryButton
              onCleared={() => {
                setGeneration((g) => g + 1)
                sessions.reload()
              }}
            />
            <button type="button" className="btn" onClick={() => setPausedAt((p) => (p === null ? Date.now() : null))} aria-pressed={paused}>
              <Icon name={paused ? 'play' : 'pause'} size={14} />
              {paused ? t('live.resume') : t('live.pause')}
            </button>
          </>
        }
      />

      {waiting > 0 && (
        <p role="status" className="mb-4 flex items-center justify-between gap-3 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
          <span className="flex items-center gap-2">
            <Icon name="approvals" size={15} className="flex-none" />
            <span>
              {t('approvals.banner', { count: waiting })} <DocsWhyLink target="why.textsWaiting" />
            </span>
          </span>
          <Link to="/approvals" className="btn btn-sm">
            {t('approvals.open')}
          </Link>
        </p>
      )}

      {me && !me.recording && (
        <p role="status" className="mb-4 flex items-start gap-2 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
          <Icon name="info" size={15} className="mt-0.5 flex-none" />
          <span>
            {t('live.notRecording')} <DocsWhyLink target="why.notRecording" />
          </span>
        </p>
      )}

      <div className="mb-3 flex flex-wrap items-end gap-3" role="group" aria-label={t('live.filters.label')}>
        <label className="flex flex-col gap-1">
          <span className="label">{t('live.filters.agent')}</span>
          <select name="agent" className="field w-[160px]" value={filters.agent} onChange={(e) => setFilter('agent', e.target.value)}>
            <option value="">{t('live.filters.anyAgent')}</option>
            {filterAgents(Boolean(me?.tutorial)).map((agent) => (
              <option key={agent} value={agent}>
                {t(AGENT_LABEL[agent])}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('live.filters.flag')}</span>
          <select name="flag" className="field w-[170px]" value={filters.flag} onChange={(e) => setFilter('flag', e.target.value)}>
            <option value="">{t('live.filters.anyFlag')}</option>
            {FILTER_FLAGS.map((flag) => (
              <option key={flag} value={flag}>
                {t(FLAG_LABEL[flag])}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-0 flex-col gap-1">
          <span className="label">{t('live.filters.session')}</span>
          <select name="session" className="field mono w-[300px] max-w-full" value={filters.session} onChange={(e) => setFilter('session', e.target.value)}>
            <option value="">{t('live.filters.anySession')}</option>
            {filters.session && !sessions.data?.items.some((s) => s.key === filters.session) && (
              <option value={filters.session}>{filters.session}</option>
            )}
            {(sessions.data?.items ?? []).map((s) => (
              <option key={s.key} value={s.key}>
                {s.key}
              </option>
            ))}
          </select>
        </label>
        {(projects.data?.items.length ?? 0) > 0 && (
          <label className="flex flex-col gap-1">
            <span className="label">{t('live.filters.project')}</span>
            <select name="project" className="field w-[200px]" value={filters.project} onChange={(e) => setFilter('project', e.target.value)}>
              <option value="">{t('live.filters.anyProject')}</option>
              {(projects.data?.items ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {anyFilter && (
          <button type="button" className="btn btn-ghost" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            <Icon name="x" size={14} />
            {t('live.filters.clear')}
          </button>
        )}
      </div>

      <LiveFeed key={filterKey} filters={filters} pausedAt={pausedAt} recording={me?.recording !== false} onResume={() => setPausedAt(null)} />

      <ModesLegend />

    </>
  )
}

function ModesLegend() {
  const { t } = useTranslation()
  return (
    <details className="mt-5 max-w-[80ch] text-ui text-ink-2">
      <summary className="cursor-pointer text-ink-2 hover:text-ink">{t('live.modes.summary')}</summary>
      <dl className="mt-3 grid grid-cols-[110px_1fr] gap-x-4 gap-y-2.5">
        {MODES.map((mode) => (
          <div key={mode} className="contents">
            <dt>
              <ModeBadge mode={mode} />
            </dt>
            <dd className="text-ink-2">{t(MODE_HELP[mode])}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

interface Arrival {
  exchange: ExchangeSummary
  at: number
}

function LiveFeed({
  filters,
  pausedAt,
  recording,
  onResume,
}: {
  filters: Filters
  pausedAt: number | null
  recording: boolean
  onResume: () => void
}) {
  const { t } = useTranslation()
  const page = useAsync(`exchanges`, (signal) =>
    api.exchanges(
      {
        agent: filters.agent || undefined,
        flag: filters.flag || undefined,
        session: filters.session || undefined,
        project: filters.project || undefined,
      },
      signal,
    ),
  )
  const [arrivals, setArrivals] = useState<Arrival[]>([])
  const [older, setOlder] = useState<ExchangeSummary[]>([])
  const [next, setNext] = useState<string | null | undefined>(undefined)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState<unknown>(null)
  const [stream, setStream] = useState<StreamState>('connecting')
  const [late, setLate] = useState<ReviewRecord[]>([]) // the local reviewer's findings after sending
  const filtersRef = useRef(filters)
  useEffect(() => {
    filtersRef.current = filters
  })

  useEffect(() => {
    if (!recording) return
    return api.stream(
      (exchange) => {
        if (!matches(exchange, filtersRef.current)) return
        setArrivals((list) => [{ exchange, at: Date.now() }, ...list].slice(0, MAX_ROWS))
      },
      setStream,
      (review) => {
        if (!review.inline && review.findings.length > 0) setLate((list) => [review, ...list].slice(0, 50))
      },
    )
  }, [recording])

  const paused = pausedAt !== null
  const live = useMemo(
    () => arrivals.filter((a) => pausedAt === null || a.at <= pausedAt).map((a) => a.exchange),
    [arrivals, pausedAt],
  )
  const pending = arrivals.length - live.length
  const fresh = useMemo(() => new Set(arrivals.map((a) => a.exchange.id)), [arrivals])

  const rows = useMemo(() => {
    const seen = new Set<string>()
    const out: ExchangeSummary[] = []
    for (const exchange of [...live, ...(page.data?.items ?? []), ...older]) {
      if (seen.has(exchange.id)) continue
      seen.add(exchange.id)
      out.push(exchange)
    }
    return out
  }, [live, page.data, older])

  const cursor = next === undefined ? (page.data?.next ?? null) : next

  const loadOlder = useCallback(async () => {
    if (!cursor) return
    setLoadingOlder(true)
    setOlderError(null)
    try {
      const more = await api.exchanges({
        before: cursor,
        agent: filters.agent || undefined,
        flag: filters.flag || undefined,
        session: filters.session || undefined,
        project: filters.project || undefined,
      })
      setOlder((list) => [...list, ...more.items])
      setNext(more.next)
    } catch (error) {
      setOlderError(error)
    } finally {
      setLoadingOlder(false)
    }
  }, [cursor, filters.agent, filters.flag, filters.session, filters.project])

  if (page.loading) return <Loading />
  if (page.error && !page.data) return <ErrorState error={page.error} onRetry={page.reload} />

  const anyFilter = Boolean(filters.agent || filters.flag || filters.session || filters.project)
  const indicator: StreamState = recording ? stream : 'closed'

  const lateCount = foundAfterSending(late)
  const lateLink = lateTarget(late, rows)

  return (
    <section aria-label={t('live.tableLabel')}>
      {lateCount > 0 && (
        <p role="status" className="mb-3 flex items-start justify-between gap-3 rounded-md border border-line bg-surface px-3 py-2 text-ui text-ink">
          <span className="flex items-start gap-2">
            <Icon name="shield" size={15} className="mt-0.5 flex-none text-ink-2" />
            <span>
              {t('reviewer.live.found', { count: lateCount })} <DocsWhyLink target="why.afterSending" />
            </span>
          </span>
          <span className="flex flex-none items-center gap-1.5">
            {lateLink && (
              <Link to={lateLink.href} className="btn btn-sm">
                {lateLink.exchange ? t('reviewer.live.openExchange') : t('reviewer.live.openSession')}
              </Link>
            )}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setLate([])}>
              {t('reviewer.live.dismiss')}
            </button>
          </span>
        </p>
      )}
      <div className="mb-2 flex min-h-6 items-center justify-between gap-3">
        <StreamIndicator state={indicator} paused={paused} />
        {paused && pending > 0 && (
          <button type="button" className="btn btn-sm" onClick={onResume}>
            {t('live.pendingCount', { count: pending })}
          </button>
        )}
      </div>
      <div className="panel overflow-x-auto">
        {rows.length === 0 ? (
          anyFilter ? (
            <EmptyState icon="filter" title={t('live.emptyFiltered.title')}>
              {t('live.emptyFiltered.body')}
            </EmptyState>
          ) : (
            <EmptyState icon="live" title={t('live.empty.title')}>
              <p>{t('live.empty.body')}</p>
              <div className="mx-auto mt-3 grid max-w-[340px] gap-2 text-left">
                <CommandLine command={CMD_CLAUDE} />
                <CommandLine command={CMD_CODEX} />
              </div>
            </EmptyState>
          )
        ) : (
          <ExchangeTable rows={rows} fresh={fresh} />
        )}
      </div>
      {cursor && (
        <div className="mt-3 flex flex-col items-center gap-2">
          <button type="button" className="btn" onClick={() => void loadOlder()} disabled={loadingOlder}>
            <Icon name="arrowDown" size={14} />
            {loadingOlder ? t('common.loading') : t('live.loadOlder')}
          </button>
          {olderError !== null && <p className="text-meta text-crit-ink">{errorText(t, olderError)}</p>}
        </div>
      )}
    </section>
  )
}

export function ExchangeTable({ rows, fresh, showSession = false }: { rows: ExchangeSummary[]; fresh?: Set<string>; showSession?: boolean }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const navigate = useNavigate()
  const classOf = useTypeClass()

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">{t('live.columns.time')}</th>
          <th scope="col">{t('live.columns.agent')}</th>
          <th scope="col">{t('live.columns.mode')}</th>
          <th scope="col" className="num">
            {t('live.columns.latency')}
          </th>
          <th scope="col" className="num">
            {t('live.columns.tokens')}
          </th>
          <th scope="col">{t('live.columns.masked')}</th>
          <th scope="col">{t('live.columns.flags')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((x) => {
          const href = `/exchanges/${encodeURIComponent(x.id)}`
          const totalIn = x.tokens ? x.tokens.input + x.tokens.cache_read + x.tokens.cache_write : 0
          const cached = x.tokens ? cacheReadRatio(x.tokens) : null
          return (
            <tr
              key={x.id}
              data-href={href}
              className={fresh?.has(x.id) ? 'arrived' : undefined}
              onClick={(event) => {
                if ((event.target as HTMLElement).closest('a,button')) return
                if (window.getSelection()?.toString()) return
                navigate(href)
              }}
            >
              <td className="whitespace-nowrap">
                <Link to={href} className="tabular-nums text-ink hover:underline" title={fmt.dateTime(x.ts)}>
                  {fmt.time(x.ts)}
                </Link>
                {showSession && <div className="mono mt-0.5 max-w-[180px] truncate text-micro text-ink-3">{x.session}</div>}
              </td>
              <td className="max-w-[210px]">
                <span title={x.agent_version ?? undefined}>
                  <AgentLabel agent={x.agent} />
                </span>
                {x.project && <span className="ml-1.5 text-meta text-ink-3">{x.project.name}</span>}
                <div className="mt-1 flex min-w-0 items-center gap-1.5 text-meta" title={`${x.upstream} ${x.path}`}>
                  <span className="flex-none text-ink-3">{x.upstream}</span>
                  <span className="mono min-w-0 truncate text-ink-2">{x.model ?? x.path}</span>
                </div>
              </td>
              <td className="whitespace-nowrap">
                <ModeBadge mode={x.mode} />
                <div className="mt-1 text-meta">
                  <TransportStatus summary={x} />
                </div>
              </td>
              <td className="num whitespace-nowrap">
                <div className="text-ink">{x.latency_ms === null ? t('common.none') : fmt.duration(x.latency_ms)}</div>
                {x.overhead_ms !== null && (
                  <div className="text-meta text-ink-3" title={t('live.overheadHelp')}>
                    {t('live.overhead', { value: fmt.duration(x.overhead_ms) })}
                  </div>
                )}
              </td>
              <td className="num whitespace-nowrap">
                {x.tokens ? (
                  <>
                    <div className="text-ink">
                      {t('live.tokensInOut', { input: fmt.compact(totalIn), output: fmt.compact(x.tokens.output) })}
                    </div>
                    {cached !== null && cached > 0 && (
                      <div className="text-meta text-ink-3" title={t('live.cacheHelp', { value: fmt.number(x.tokens.cache_read) })}>
                        {t('live.cached', { value: fmt.percent(cached) })}
                      </div>
                    )}
                  </>
                ) : (
                  <span className="text-ink-3">{t('common.none')}</span>
                )}
              </td>
              <td className="min-w-[180px]">
                {x.masked === 0 ? (
                  <span className="text-ink-3">{t('live.nothingMasked')}</span>
                ) : (
                  <div className="flex flex-col gap-1">
                    <span className="text-ink">
                      <span className="font-semibold tabular-nums">{fmt.number(x.masked)}</span>{' '}
                      <span className="text-meta text-ink-3">
                        {x.new_masked > 0 ? t('live.newMasked', { count: x.new_masked }) : t('live.allResent')}
                      </span>
                    </span>
                    <ClassStrip {...classCounts(x, classOf)} />
                    <TypeChips byType={x.by_type} classOf={typeClassesFor(x, classOf)} max={2} />
                  </div>
                )}
              </td>
              <td className="w-[150px] min-w-[130px]">
                <FlagList flags={x.flags} verified={x.verified} reason={x.block_reason} />
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
