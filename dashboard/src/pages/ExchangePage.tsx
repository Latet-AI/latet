import { useMemo, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router'
import { ApiError, api } from '../api/client'
import type { ExchangeDetail, Leak } from '../api/types'
import { AgentLabel, FlagList, ModeBadge, TransportStatus } from '../components/badges'
import { DocsPageLink, DocsWhyLink } from '../components/DocsLinks'
import { FlowStrip } from '../components/exchange/FlowStrip'
import { ExchangeImages } from '../components/exchange/ExchangeImages'
import { ClassLegend } from '../components/exchange/Legend'
import { HeadersView, RawJsonView } from '../components/exchange/RawViews'
import { ResponseView } from '../components/exchange/ResponseView'
import { SlotView } from '../components/exchange/SlotView'
import { StrictNotice } from '../components/exchange/StrictNotice'
import { OutboundCard, WhyBlocked } from '../components/exchange/WhyBlocked'
import { Icon, StatusGlyph } from '../components/Icon'
import { ReviewPanel } from '../components/ReviewPanel'
import { RuleScope } from '../components/RuleName'
import { rulesHref } from '../components/ScopeBar'
import { knownValues, requestMarks } from '../components/masked/marks'
import { CopyButton, EmptyState, ErrorState, Loading, Meta, Segmented } from '../components/ui'
import { outboundTools } from '../lib/blocks'
import { signedBy } from '../lib/destinations'
import { isBlocked, parseFlags } from '../lib/flags'
import { flagDetail, KIND_LABEL, MODULE_LABEL, moduleCounts, REVIEW_DETAIL_LABEL, REVIEW_DETAIL_TITLE } from '../lib/labels'
import { useDeploymentKey } from '../lib/me'
import { usePref } from '../lib/prefs'
import { ruleLayer } from '../lib/projects'
import { cacheReadRatio } from '../lib/stats'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'

type BlockFilter = 'new' | 'all'

export function ExchangePage() {
  const { t } = useTranslation()
  const { id = '' } = useParams()
  const state = useAsync(`exchange:${id}`, (signal) => api.exchange(id, signal))
  const detail = state.data && state.data.summary.id === id ? state.data : undefined

  if (state.error instanceof ApiError && state.error.status === 404) {
    return (
      <>
        <BackLink />
        <EmptyState icon="question" title={t('exchange.notFound.title')}>
          {t('exchange.notFound.body')}
        </EmptyState>
      </>
    )
  }
  if (state.error && !detail) return <ErrorState error={state.error} onRetry={state.reload} />
  if (!detail) return <Loading />
  return <ExchangeView detail={detail} />
}

function BackLink() {
  const { t } = useTranslation()
  return (
    <Link to="/live" className="mb-3 inline-flex items-center gap-1.5 text-ui text-ink-2 hover:text-ink">
      <Icon name="arrowLeft" size={14} />
      {t('exchange.back')}
    </Link>
  )
}

function Section({ id, title, aside, children }: { id: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="mt-8 scroll-mt-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 id={`${id}-title`} className="text-title font-semibold text-ink">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  )
}

/** What happened to the request, in a sentence: the tutorial's page shows it above the exchange it looks at. */
export function Verdict({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const { summary } = detail
  const uninspected = parseFlags(summary.flags)
    .filter((f) => f.key === 'uninspected')
    .map((f) => flagDetail(f, t))
  if (isBlocked(summary.flags)) return <WhyBlocked detail={detail} />
  if (outboundTools(summary.flags).length > 0) return <OutboundCard detail={detail} blocked={false} />
  if (summary.mode === 'passthrough') {
    return (
      <p className="flex items-start gap-2 text-body text-ink">
        <StatusGlyph status="warn" size={18} />
        <span>
          <strong className="font-semibold">{t('exchange.verdict.passthrough')}</strong> {t('exchange.verdict.passthroughDetail')}
        </span>
      </p>
    )
  }
  const letThrough = detail.leaks.filter((leak) => leak.allowed)
  if (summary.flags.includes('auto-mode-review') && letThrough.length > 0 && letThrough.length === detail.leaks.length) {
    const what = [...new Set(letThrough.flatMap((leak) => (leak.detail ? [leak.detail] : [])))]
    return (
      <p className="flex items-start gap-2 text-body text-ink">
        <StatusGlyph status="ok" size={18} />
        <span>
          <strong className="font-semibold">{t('exchange.verdict.reviewAllowed')}</strong>{' '}
          {t('exchange.verdict.reviewAllowedDetail', { what: fmt.list(what.map((d) => t(REVIEW_DETAIL_LABEL[d]))) })}
        </span>
      </p>
    )
  }
  if (summary.verified) {
    return (
      <p className="flex items-start gap-2 text-body text-ink">
        <StatusGlyph status="ok" size={18} />
        <span>
          <strong className="font-semibold">{t('exchange.verdict.verified')}</strong>{' '}
          {summary.masked > 0 ? t('exchange.verdict.verifiedMasked', { count: summary.masked }) : t('exchange.verdict.verifiedNothing')}
          {uninspected.length > 0 && <> {t('exchange.verdict.uninspected', { what: fmt.list(uninspected) })}</>}
        </span>
      </p>
    )
  }
  return (
    <p className="flex items-start gap-2 text-body text-ink">
      <StatusGlyph status="warn" size={18} />
      <span>
        <strong className="font-semibold">{t('exchange.verdict.notVerified')}</strong> {t('exchange.verdict.notVerifiedDetail')}
      </span>
    </p>
  )
}

/** Rule names in an exchange of a project say which of the project's settings they come from. */
function ProjectScope({ projectId, children }: { projectId: string | null; children: ReactNode }) {
  const project = useAsync(projectId ? `project:${projectId}` : null, (signal) => api.project(projectId ?? '', signal))
  const value = useMemo(
    () => (projectId && project.data ? { projectId, layer: (rule: string | null) => ruleLayer(rule, project.data ?? null) } : null),
    [projectId, project.data],
  )
  return <RuleScope.Provider value={value}>{children}</RuleScope.Provider>
}

function ExchangeView({ detail }: { detail: ExchangeDetail }) {
  return (
    <ProjectScope projectId={detail.summary.project?.id ?? null}>
      <ExchangeBody detail={detail} />
    </ProjectScope>
  )
}

function ExchangeBody({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const { summary, request, response, leaks } = detail
  const allLetThrough = leaks.length > 0 && leaks.every((leak) => leak.allowed) // auto mode review, as the rules allow
  const [blockPref, setBlockPref] = usePref<BlockFilter>('blocks', 'new')
  const [footnotes, setFootnotes] = usePref<boolean>('footnotes', true)
  const newCount = request.slots.filter((s) => s.new).length
  const blockFilter: BlockFilter = newCount === 0 ? 'all' : blockPref

  // The values that stopped the request are marked where they are, to click: Not sensitive lets them through.
  const marks = useMemo(() => requestMarks(detail), [detail])
  const known = useMemo(() => knownValues(marks), [marks])
  const allValues = useMemo(() => marks.flatMap((m) => m.values), [marks])
  // A block with a value that stopped the request is shown whatever the filter: it was never sent, so a retry marks it again as old.
  const stops = marks.map((m) => m.sent.some((mark) => mark.info.stopped))
  const visible = request.slots.map((slot, i) => ({ slot, i })).filter(({ slot, i }) => blockFilter === 'all' || slot.new || stops[i])
  const totalIn = summary.tokens ? summary.tokens.input + summary.tokens.cache_read + summary.tokens.cache_write : 0
  const cached = summary.tokens ? cacheReadRatio(summary.tokens) : null
  const signer = signedBy(summary.signed)

  return (
    <>
      {/* On a phone the row wraps and the id shortens (its copy button copies it whole): never wider than the page. */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <BackLink />
        <span className="flex min-w-0 items-center gap-3">
          <span className="flex min-w-0 items-center gap-1.5 text-meta text-ink-3">
            <code className="mono min-w-0 truncate" title={summary.id}>
              {summary.id}
            </code>
            <CopyButton text={summary.id} label={t('exchange.copyId')} compact />
          </span>
          <DocsPageLink target="page.exchange" />
        </span>
      </div>

      <header className="pb-5">
        <h1 className="font-display text-page font-extrabold tracking-[-0.01em] text-ink">
          {t('exchange.title', { upstream: summary.upstream, time: fmt.time(summary.ts) })}
        </h1>
        <div className="mt-2">
          <Verdict detail={detail} />
        </div>
        {summary.flags.length > 0 && (
          <div className="mt-3">
            <FlagList flags={summary.flags} focusable reason={detail.block_reason ?? summary.block_reason} why />
          </div>
        )}
        <dl className="mt-5 grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-6 gap-y-3 border-t border-line pt-4">
          <Meta label={t('exchange.meta.agent')}>
            <AgentLabel agent={summary.agent} version={summary.agent_version} />
          </Meta>
          <Meta label={t('exchange.meta.model')}>
            <span className="mono">{summary.model ?? t('common.none')}</span>
          </Meta>
          <Meta label={t('exchange.meta.mode')}>
            <ModeBadge mode={summary.mode} focusable />
          </Meta>
          <Meta label={t('exchange.meta.status')}>
            <TransportStatus summary={summary} focusable />
          </Meta>
          <Meta label={t('exchange.meta.latency')} wrap>
            {summary.latency_ms === null ? t('common.none') : fmt.duration(summary.latency_ms)}
            {summary.overhead_ms !== null && (
              <span className="ml-1.5 text-meta text-ink-3">{t('exchange.meta.overhead', { value: fmt.duration(summary.overhead_ms) })}</span>
            )}
          </Meta>
          <Meta label={t('exchange.meta.tokens')} wrap>
            {summary.tokens ? (
              <span title={t('exchange.meta.tokensHelp', { input: fmt.number(summary.tokens.input), read: fmt.number(summary.tokens.cache_read), write: fmt.number(summary.tokens.cache_write), output: fmt.number(summary.tokens.output) })}>
                {t('live.tokensInOut', { input: fmt.compact(totalIn), output: fmt.compact(summary.tokens.output) })}
                {cached !== null && cached > 0 && <span className="ml-1.5 text-meta text-ink-3">{t('live.cached', { value: fmt.percent(cached) })}</span>}
              </span>
            ) : (
              t('common.none')
            )}
          </Meta>
          {summary.project && (
            <Meta label={t('exchange.meta.project')}>
              <Link to={rulesHref(summary.project.id)} className="link" title={t('exchange.meta.projectHelp')}>
                {summary.project.name}
              </Link>
            </Meta>
          )}
          <Meta label={t('exchange.meta.session')}>
            <Link to={`/sessions/${encodeURIComponent(summary.session)}`} className="link mono" title={summary.session}>
              {summary.session}
            </Link>
          </Meta>
          {summary.host && (
            <Meta label={t('exchange.meta.sentTo')}>
              <span className="mono" title={t('exchange.meta.sentToHelp')}>
                {summary.host}
              </span>
            </Meta>
          )}
          {signer && (
            <Meta label={t('exchange.meta.signed')} wrap>
              {signer.by === 'profile' ? t('exchange.meta.signedProfile', { profile: signer.profile }) : t('exchange.meta.signedSession')}
            </Meta>
          )}
          <Meta label={t('exchange.meta.path')}>
            <span className="mono" title={summary.path}>
              {summary.path}
            </span>
          </Meta>
          <Meta label={t('exchange.meta.size')}>{fmt.bytes(request.body_bytes)}</Meta>
          <Meta label={t('exchange.meta.when')} wrap>
            {fmt.dateTime(summary.ts)}
          </Meta>
          {moduleCounts(summary).length > 0 && (
            <Meta label={t('exchange.meta.modules')} wrap>
              <ul className="space-y-0.5">
                {moduleCounts(summary).map(([module, n]) => (
                  <li key={module} className="flex justify-between gap-3">
                    <span className="min-w-0">{t(MODULE_LABEL[module])}</span>
                    <span className="num text-ink-3">{fmt.number(n)}</span>
                  </li>
                ))}
              </ul>
            </Meta>
          )}
        </dl>
      </header>

      <FlowStrip detail={detail} />

      {leaks.length > 0 && isBlocked(summary.flags) && (
        <details className="mb-2 rounded-lg border border-line bg-surface p-4">
          <summary className="cursor-pointer text-ui font-medium text-ink">{t('exchange.leaks.folded', { count: leaks.length })}</summary>
          <LeakTable leaks={leaks} />
        </details>
      )}
      {leaks.length > 0 && !isBlocked(summary.flags) && (
        <section
          aria-labelledby="leaks-title"
          className={`mb-2 rounded-lg border p-4 ${allLetThrough ? 'border-line bg-surface' : 'border-crit/50 bg-crit/6'}`}
        >
          <h2 id="leaks-title" className="flex items-center gap-2 text-title font-semibold text-ink">
            <Icon name={allLetThrough ? 'shield' : 'leak'} className={allLetThrough ? 'text-ink-3' : 'text-crit-ink'} />
            {allLetThrough
              ? t('exchange.leaks.letThroughTitle', { count: leaks.length })
              : t('exchange.leaks.title', { count: leaks.length })}
          </h2>
          <p className="mt-1 max-w-[90ch] text-ui text-ink-2">
            {allLetThrough ? t('exchange.leaks.letThroughBody') : t('exchange.leaks.body')}{' '}
            <DocsWhyLink target={allLetThrough ? 'why.autoModeReviewer' : 'why.blocked'} />
          </p>
          <LeakTable leaks={leaks} />
        </section>
      )}

      <Section
        id="request"
        title={t('exchange.request.title')}
        aside={
          <div className="flex flex-wrap items-center gap-3">
            <Segmented<BlockFilter>
              label={t('exchange.request.blocksLabel')}
              value={blockFilter}
              onChange={setBlockPref}
              options={[
                { value: 'new', label: t('exchange.request.newBlocks', { count: newCount }) },
                { value: 'all', label: t('exchange.request.allBlocks', { count: request.slots.length }) },
              ]}
            />
            <label className="flex items-center gap-1.5 text-meta text-ink-2">
              <input type="checkbox" checked={footnotes} onChange={(e) => setFootnotes(e.target.checked)} className="accent-[var(--ink)]" />
              {t('exchange.request.footnotes')}
            </label>
          </div>
        }
      >
        <StrictNotice exchangeId={summary.id} slots={request.slots} projectId={summary.project?.id ?? null} />
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-[90ch] text-ui text-ink-2">
            {blockFilter === 'new' ? t('exchange.request.newNote', { count: newCount, total: request.slots.length }) : t('exchange.request.allNote')}
          </p>
          <ClassLegend values={allValues} stopped={stops.some(Boolean)} />
        </div>
        {isBlocked(summary.flags) && request.slots.length > 0 && (
          <p className="mb-3 flex max-w-[100ch] items-start gap-2 rounded-md border border-crit/50 bg-crit/6 px-3 py-2 text-ui text-ink">
            <Icon name="ban" size={15} className="mt-0.5 flex-none text-crit-ink" />
            <span>
              <strong className="font-semibold">{t('exchange.request.notSent')}</strong>{' '}
              {leaks.length > 0 ? t(key('exchange.request.notSentDetail')) : t(key('exchange.request.notSentText'))}
            </span>
          </p>
        )}
        <ExchangeImages images={request.images ?? []} slots={request.slots} />
        {request.slots.length === 0 ? (
          <div className="panel px-4 py-5 text-ui text-ink-3">{t('exchange.request.noBlocks')}</div>
        ) : (
          <div className="space-y-3">
            {visible.map(({ slot, i }) => (
              <SlotView
                key={`${slot.path}:${i}`}
                slot={slot}
                index={i}
                marks={marks[i]!}
                footnotes={footnotes}
                blocked={isBlocked(summary.flags)}
                exchangeId={summary.id}
                agent={summary.agent}
              />
            ))}
          </div>
        )}
      </Section>

      <ReviewPanel session={summary.session} slots={request.slots} />

      <Section id="response" title={t('exchange.response.title')}>
        {response ? (
          <ResponseView response={response} known={known} footnotes={footnotes} mode={summary.mode} />
        ) : (
          <div className="panel px-4 py-5 text-ui text-ink-2">
            {summary.transport === 'plugin'
              ? t('exchange.response.plugin')
              : isBlocked(summary.flags)
                ? t('exchange.response.blocked')
                : t('exchange.response.none')}
          </div>
        )}
      </Section>

      <details className="mt-8 group">
        <summary className="cursor-pointer text-title font-semibold text-ink">{t('exchange.headers.title')}</summary>
        <div className="mt-3">
          <HeadersView detail={detail} />
        </div>
      </details>

      <details className="mt-6">
        <summary className="cursor-pointer text-title font-semibold text-ink">{t('exchange.raw.title')}</summary>
        <div className="mt-3">
          <RawJsonView detail={detail} />
        </div>
      </details>
    </>
  )
}

function LeakTable({ leaks }: { leaks: Leak[] }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  return (
    <table className="data-table mt-3 rounded-md bg-surface">
      <thead>
        <tr>
          <th scope="col">{t('exchange.leaks.type')}</th>
          <th scope="col">{t('exchange.leaks.kind')}</th>
          <th scope="col">{t('exchange.leaks.preview')}</th>
          <th scope="col">{t('exchange.leaks.status')}</th>
        </tr>
      </thead>
      <tbody>
        {leaks.map((leak, i) => (
          <tr key={i}>
            <td className="mono text-ink">
              {leak.type}
              {leak.detail && (
                <span className="block font-sans text-meta text-ink-3">{t(REVIEW_DETAIL_TITLE[leak.detail])}</span>
              )}
            </td>
            <td className="text-ink-2">{KIND_LABEL[leak.kind as keyof typeof KIND_LABEL] ? t(key(KIND_LABEL[leak.kind as keyof typeof KIND_LABEL])) : leak.kind}</td>
            <td className="mono text-ink-2">{leak.preview}</td>
            <td className="text-ink-2">
              {leak.allowed ? t('exchange.leaks.allowed') : leak.new ? t('exchange.leaks.new') : t('exchange.leaks.old')}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
