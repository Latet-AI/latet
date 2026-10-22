import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, Navigate, NavLink, useParams } from 'react-router'
import { api } from '../api/client'
import type { Agent, ExchangeSummary, Session } from '../api/types'
import { AgentIcon, agentName, FlagList, ModeBadge, TransportStatus, TypeChips } from '../components/badges'
import { SlotView } from '../components/exchange/SlotView'
import { Icon } from '../components/Icon'
import { blockMarks } from '../components/masked/marks'
import { EmptyState, ErrorState, errorText, Loading, PageHeader, Segmented } from '../components/ui'
import { AGENTS, typeClassesFor } from '../lib/labels'
import { useDeploymentKey, useTypeClass } from '../lib/me'
import { usePref } from '../lib/prefs'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'
import { useInView } from '../lib/useInView'

type Payload = 'new' | 'all'

export function SessionsPage() {
  const { t } = useTranslation()
  const { key } = useParams()
  const sessions = useAsync('sessions', (signal) => api.sessions(100, signal))

  const header = <PageHeader title={t('sessions.title')} description={t('sessions.description')} docs="page.sessions" />
  if (sessions.loading) return <>{header}<Loading /></>
  if (sessions.error && !sessions.data) return <>{header}<ErrorState error={sessions.error} onRetry={sessions.reload} /></>
  const items = sessions.data?.items ?? []
  if (items.length === 0) {
    return (
      <>
        {header}
        <div className="panel">
          <EmptyState icon="sessions" title={t('sessions.empty.title')}>
            {t('sessions.empty.body')}
          </EmptyState>
        </div>
      </>
    )
  }
  if (!key) return <Navigate to={`/sessions/${encodeURIComponent(items[0]!.key)}`} replace />
  const selected = items.find((s) => s.key === key)

  return (
    <>
      {header}
      <div className="grid grid-cols-[300px_minmax(0,1fr)] items-start gap-6 max-xl:grid-cols-[260px_minmax(0,1fr)] max-md:grid-cols-1">
        <SessionList sessions={items} selected={key} />
        <div className="min-w-0">
          <SessionTimeline key={key} sessionKey={key} session={selected} />
        </div>
      </div>
    </>
  )
}

function SessionList({ sessions, selected }: { sessions: Session[]; selected: string }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const groups = useMemo(() => {
    const order: Agent[] = [...AGENTS]
    const map = new Map<string, Session[]>()
    for (const s of sessions) {
      const agent = order.includes(s.agent) ? s.agent : 'other'
      if (!map.has(agent)) map.set(agent, [])
      map.get(agent)!.push(s)
    }
    return order.filter((a) => map.has(a)).map((agent) => ({ agent, sessions: map.get(agent)! }))
  }, [sessions])

  return (
    <nav aria-label={t('sessions.listLabel')} className="panel sticky top-4 max-h-[calc(100vh-32px)] overflow-y-auto">
      {groups.map((group, gi) => (
        <div key={group.agent} className={gi > 0 ? 'border-t border-line' : undefined}>
          <h2 className="flex items-center gap-2 px-3 pt-3 pb-1.5 text-meta font-medium text-ink-2">
            <AgentIcon agent={group.agent} />
            {agentName(t, group.agent)}
            <span className="text-ink-3 tabular-nums">{group.sessions.length}</span>
          </h2>
          <ul className="px-1.5 pb-2">
            {group.sessions.map((s) => (
              <li key={s.key}>
                <NavLink
                  to={`/sessions/${encodeURIComponent(s.key)}`}
                  aria-current={s.key === selected ? 'page' : undefined}
                  className={`relative block rounded-md px-2.5 py-2 ${s.key === selected ? 'bg-wash-2' : 'hover:bg-wash'}`}
                >
                  {s.key === selected && (
                    <span className="absolute top-1/2 -left-1.5 h-1 w-1.5 -translate-y-1/2 rounded-r-[1px] bg-brand" aria-hidden="true" />
                  )}
                  <span className="mono block truncate text-meta text-ink" title={s.key}>
                    {s.key}
                  </span>
                  <span className="mt-0.5 flex flex-wrap gap-x-3 text-micro text-ink-3">
                    <span title={fmt.dateTime(s.last_ts)}>{fmt.relative(s.last_ts)}</span>
                    <span>{t('sessions.exchanges', { count: s.exchanges })}</span>
                    {s.masked > 0 && <span>{t('sessions.masked', { count: s.masked })}</span>}
                    {s.blocked > 0 && <span className="text-crit-ink">{t('sessions.blocked', { count: s.blocked })}</span>}
                  </span>
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  )
}

function SessionTimeline({ sessionKey, session }: { sessionKey: string; session: Session | undefined }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const [payload, setPayload] = usePref<Payload>('sessionPayload', 'new')
  const page = useAsync(`session:${sessionKey}`, (signal) => api.exchanges({ session: sessionKey, limit: 200 }, signal))
  const [earlier, setEarlier] = useState<ExchangeSummary[]>([])
  const [next, setNext] = useState<string | null | undefined>(undefined)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [earlierError, setEarlierError] = useState<unknown>(null)

  if (page.loading) return <Loading />
  if (page.error && !page.data) return <ErrorState error={page.error} onRetry={page.reload} />
  const newestFirst = [...(page.data?.items ?? []), ...earlier]
  const chronological = [...newestFirst].reverse()
  const cursor = next === undefined ? (page.data?.next ?? null) : next

  const loadEarlier = async () => {
    if (!cursor) return
    setLoadingEarlier(true)
    setEarlierError(null)
    try {
      const more = await api.exchanges({ session: sessionKey, before: cursor, limit: 200 })
      setEarlier((list) => [...list, ...more.items])
      setNext(more.next)
    } catch (error) {
      setEarlierError(error)
    } finally {
      setLoadingEarlier(false)
    }
  }

  return (
    <section aria-labelledby="session-title">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="session-title" className="mono truncate text-title font-semibold text-ink" title={sessionKey}>
            {sessionKey}
          </h2>
          {session && (
            <p className="mt-0.5 text-ui text-ink-2">
              {t('sessions.summary', {
                agent: agentName(t, session.agent),
                first: fmt.dateTime(session.first_ts),
                last: fmt.time(session.last_ts),
                count: session.exchanges,
              })}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<Payload>
            label={t('sessions.payloadLabel')}
            value={payload}
            onChange={setPayload}
            options={[
              { value: 'new', label: t('sessions.payloadNew') },
              { value: 'all', label: t('sessions.payloadAll') },
            ]}
          />
          <Link to={`/live?session=${encodeURIComponent(sessionKey)}`} className="btn btn-sm">
            <Icon name="live" size={13} />
            {t('sessions.openInLive')}
          </Link>
        </div>
      </div>
      <p className="mb-4 max-w-[90ch] text-ui text-ink-2">{payload === 'new' ? t(key('sessions.newNote')) : t('sessions.allNote')}</p>

      {cursor && (
        <div className="mb-3">
          <button type="button" className="btn btn-sm" onClick={() => void loadEarlier()} disabled={loadingEarlier}>
            {loadingEarlier ? t('common.loading') : t('sessions.loadEarlier')}
          </button>
          {earlierError !== null && <p className="mt-1 text-meta text-crit-ink">{errorText(t, earlierError)}</p>}
        </div>
      )}

      {chronological.length === 0 ? (
        <div className="panel">
          <EmptyState icon="sessions" title={t('sessions.noExchanges.title')}>
            {t('sessions.noExchanges.body')}
          </EmptyState>
        </div>
      ) : (
        <ol className="relative space-y-5 border-l border-line-2 pl-5" aria-label={t('sessions.timelineLabel')}>
          {chronological.map((exchange) => (
            <TimelineItem key={exchange.id} exchange={exchange} payload={payload} />
          ))}
        </ol>
      )}
    </section>
  )
}

function TimelineItem({ exchange, payload }: { exchange: ExchangeSummary; payload: Payload }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const classOf = useTypeClass()
  const [element, setElement] = useState<HTMLLIElement | null>(null)
  const visible = useInView(element)
  const detail = useAsync(visible ? `timeline:${exchange.id}` : null, (signal) => api.exchange(exchange.id, signal))
  const blocked = exchange.flags.some((f) => f === 'blocked')
  const slots = detail.data?.request.slots ?? []
  const shown = slots.map((slot, i) => ({ slot, i })).filter(({ slot }) => payload === 'all' || slot.new)
  const marks = useMemo(
    () => (detail.data ? detail.data.request.slots.map((s, i) => blockMarks(s.original, s.masked, s.replacements, `tl:${exchange.id}:${i}`)) : []),
    [detail.data, exchange.id],
  )

  return (
    <li ref={setElement} className="relative">
      <span
        className={`absolute top-1.5 -left-[25px] size-[9px] rounded-full border-2 border-canvas ${blocked ? 'bg-crit' : 'bg-ink-3'}`}
        aria-hidden="true"
      />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Link to={`/exchanges/${encodeURIComponent(exchange.id)}`} className="text-ui font-medium text-ink tabular-nums hover:underline" title={fmt.dateTime(exchange.ts)}>
          {fmt.time(exchange.ts)}
        </Link>
        <span className="mono text-meta text-ink-2">{exchange.model ?? exchange.path}</span>
        <ModeBadge mode={exchange.mode} />
        <TransportStatus summary={exchange} />
        <span className="text-meta text-ink-2">
          {exchange.new_masked > 0
            ? t('sessions.item.newMasked', { count: exchange.new_masked })
            : exchange.masked > 0
              ? t('sessions.item.onlyResent', { count: exchange.masked })
              : t('sessions.item.nothing')}
        </span>
        {exchange.new_masked > 0 && (
          <TypeChips byType={exchange.new_by_type ?? exchange.by_type} classOf={typeClassesFor(exchange, classOf)} max={4} />
        )}
        <FlagList flags={exchange.flags} verified={exchange.verified} reason={exchange.block_reason} />
      </div>
      <div className="mt-2 space-y-2">
        {!visible || detail.loading ? (
          <div className="h-10 rounded-md border border-dashed border-line" aria-hidden="true" />
        ) : detail.error && !detail.data ? (
          <p className="text-meta text-crit-ink">{errorText(t, detail.error)}</p>
        ) : shown.length === 0 ? (
          <p className="text-meta text-ink-3">{payload === 'new' ? t('sessions.item.noNewBlocks', { count: slots.length }) : t('sessions.item.noBlocks')}</p>
        ) : (
          shown.map(({ slot, i }) => <SlotView key={i} slot={slot} index={i} marks={marks[i]!} footnotes={false} sentOnly compact />)
        )}
      </div>
    </li>
  )
}
