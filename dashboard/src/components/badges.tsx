import { useTranslation } from 'react-i18next'
import type { Agent, ExchangeSummary, Mode } from '../api/types'
import { parseFlags, type ParsedFlag } from '../lib/flags'
import {
  AGENT_LABEL,
  CLASS_LABEL,
  CLASS_ORDER,
  CLASS_TONE,
  flagDetail,
  FLAG_HELP,
  FLAG_LABEL,
  isJudgement,
  MODE_HELP,
  MODE_LABEL,
  type ClassCounts,
  type ViewClass,
} from '../lib/labels'
import { useFormat } from '../lib/useFormat'
import { FLAG_DOCS } from '../lib/docsPlaces'
import { useDeploymentKey } from '../lib/me'
import { rankEntries } from '../lib/stats'
import { DocsWhyLink } from './DocsLinks'
import { Tooltip } from './Floating'
import { Icon, type IconName } from './Icon'

const MODE_STYLE: Record<Mode, string> = {
  rewrite: 'border-line-2 text-ink',
  verify: 'border-line-2 text-ink',
  passthrough: 'border-dashed border-line-2 text-ink-3',
}

// A shield: values were replaced. A check: checked, not changed. None: not inspected.
const MODE_ICON: Partial<Record<Mode, IconName>> = {
  rewrite: 'shield',
  verify: 'check',
}

export function ModeBadge({ mode, focusable = false }: { mode: Mode; focusable?: boolean }) {
  const { t } = useTranslation()
  const label = MODE_LABEL[mode] ? t(MODE_LABEL[mode]) : mode
  return (
    <Tooltip content={MODE_HELP[mode] ? t(MODE_HELP[mode]) : mode} focusable={focusable}>
      <span className={`tag ${MODE_STYLE[mode] ?? ''}`}>
        {MODE_ICON[mode] && <Icon name={MODE_ICON[mode]} size={12} className="text-ink-2" />}
        {label}
      </span>
    </Tooltip>
  )
}

const FLAG_ICON: Record<ParsedFlag['key'], IconName> = {
  blocked: 'ban',
  leak: 'leak',
  error: 'x',
  uninspected: 'eyeOff',
  degraded: 'gauge',
  'unknown-route': 'question',
  'auto-mode-review': 'search',
  'project-off': 'eyeOff',
  withheld: 'ban',
  history: 'sessions',
  outbound: 'external',
  'route-bypass': 'external',
  image: 'image',
  other: 'info',
}

const SEVERITY_STYLE: Record<ParsedFlag['severity'], string> = {
  critical: 'border-crit/50 bg-crit/10 text-ink [&_svg]:text-crit-ink',
  warning: 'border-warn/60 bg-warn/12 text-ink [&_svg]:text-ink',
  neutral: 'border-line-2 text-ink-2',
}

export function FlagBadge({
  flag,
  focusable = false,
  reason,
}: {
  flag: ParsedFlag
  focusable?: boolean
  /** The block reason the daemon gave, shown as it is under the help of "Blocked". */
  reason?: string | null
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const base = flag.key === 'other' ? flag.raw : t(FLAG_LABEL[flag.key])
  const detail = flag.detail ? flagDetail(flag, t) : null
  const label = detail && flag.key !== 'other' ? t('flags.withDetail', { label: base, detail }) : base
  const help = t(key(FLAG_HELP[flag.key]), { detail: detail ?? '', flag: flag.raw })
  const content =
    flag.key === 'blocked' && reason ? (
      <>
        <span className="block">{help}</span>
        <span className="mt-1.5 block text-ink">{reason}</span>
      </>
    ) : (
      help
    )
  return (
    <Tooltip content={content} focusable={focusable}>
      <span className={`tag h-auto min-h-5 py-0.5 leading-tight whitespace-normal ${SEVERITY_STYLE[flag.severity]}`}>
        <Icon name={FLAG_ICON[flag.key]} size={12} />
        {label}
      </span>
    </Tooltip>
  )
}

export function FlagList({
  flags,
  verified,
  focusable = false,
  reason,
  why = false,
}: {
  flags: string[]
  verified?: boolean
  focusable?: boolean
  reason?: string | null
  /** On a request's own page: a "Why" after each flag that tells what happened, to where that is explained. */
  why?: boolean
}) {
  const { t } = useTranslation()
  const parsed = parseFlags(flags)
  if (parsed.length === 0) return verified ? <VerifiedMark focusable={focusable} /> : null
  return (
    <span className={`flex flex-wrap ${why ? 'gap-x-3 gap-y-1' : 'gap-1'}`}>
      {parsed.map((flag) => {
        const badge = <FlagBadge key={flag.raw} flag={flag} focusable={focusable} reason={reason} />
        const docs = why ? FLAG_DOCS[flag.key] : undefined
        if (!docs) return badge
        return (
          <span key={flag.raw} className="inline-flex items-center gap-1.5 text-meta">
            {badge}
            <DocsWhyLink target={docs} about={t(FLAG_LABEL[flag.key])} />
          </span>
        )
      })}
    </span>
  )
}

/** The egress check ran and found nothing in clear. */
export function VerifiedMark({ focusable = false }: { focusable?: boolean }) {
  const { t } = useTranslation()
  return (
    <Tooltip content={t('flags.verified.help')} focusable={focusable}>
      <span className="inline-flex items-center gap-1 text-meta text-good-ink">
        <Icon name="shield" size={14} />
        {t('flags.verified.label')}
      </span>
    </Tooltip>
  )
}

export function HttpStatus({ status }: { status: number | null }) {
  const { t } = useTranslation()
  if (status === null) return <span className="text-ink-3">{t('common.none')}</span>
  const bad = status >= 400
  return (
    <span className={`inline-flex items-center gap-1 tabular-nums ${bad ? 'font-medium text-crit-ink' : 'text-ink'}`}>
      {bad && <Icon name="ban" size={12} />}
      {status}
    </span>
  )
}

/** The HTTP status, or what happened instead: a WebSocket turn, a stop inside Claude Code. */
export function TransportStatus({ summary, focusable = false }: { summary: ExchangeSummary; focusable?: boolean }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  if (summary.transport === 'plugin') {
    return (
      <Tooltip content={t(key('transport.pluginHelp'))} focusable={focusable}>
        <span className="tag border-crit/50 bg-crit/10 text-ink [&_svg]:text-crit-ink">
          <Icon name="ban" size={12} />
          {t('transport.plugin')}
        </span>
      </Tooltip>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <HttpStatus status={summary.status} />
      {summary.transport === 'ws' && (
        <span className="tag h-4 flex-none px-1 text-[10px]" title={t('transport.websocket')}>
          {t('transport.ws')}
        </span>
      )}
    </span>
  )
}

const VIEW_ORDER: ViewClass[] = [...CLASS_ORDER, 'UNKNOWN']

/**
 * The classes of a request's masked values as one thin segmented bar (the redaction bar of the
 * logo), with the counts in its accessible name and tooltip.
 */
export function ClassStrip({ counts, guessed = false, width = 96 }: { counts: ClassCounts; guessed?: boolean; width?: number }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const entries = VIEW_ORDER.filter((cls) => (counts[cls] ?? 0) > 0).map((cls) => ({ cls, n: counts[cls] ?? 0 }))
  const total = entries.reduce((sum, e) => sum + e.n, 0)
  if (total === 0) return null
  const parts = entries.map((e) => t('classes.count', { label: t(CLASS_LABEL[e.cls]), count: e.n }))
  const label = guessed ? t('classes.guessed', { list: fmt.list(parts) }) : fmt.list(parts)
  return (
    <span className="flex gap-[2px]" style={{ width }} role="img" aria-label={label} title={label}>
      {entries.map((e) => (
        <span
          key={e.cls}
          data-tone={CLASS_TONE[e.cls]}
          className="h-1 min-w-[3px] rounded-[1px]"
          style={{
            flexGrow: e.n,
            flexBasis: 0,
            background: isJudgement(e.cls) ? 'repeating-linear-gradient(90deg, var(--tone) 0 3px, transparent 3px 5px)' : 'var(--tone)',
          }}
        />
      ))}
    </span>
  )
}

export function ClassSwatch({ cls, dashed }: { cls: ViewClass; dashed?: boolean }) {
  return (
    <span
      className="swatch"
      data-tone={CLASS_TONE[cls] ?? 'neutral'}
      style={dashed || isJudgement(cls) ? { width: 10, background: 'repeating-linear-gradient(90deg, var(--tone) 0 3px, transparent 3px 5px)' } : undefined}
      aria-hidden="true"
    />
  )
}

export function ClassLabel({ cls }: { cls: ViewClass }) {
  const { t } = useTranslation()
  return (
    <span className="inline-flex items-center gap-1.5 text-ui text-ink-2">
      <ClassSwatch cls={cls} />
      {CLASS_LABEL[cls] ? t(CLASS_LABEL[cls]) : cls}
    </span>
  )
}

/**
 * Per-type chips: "EMAIL 2". Classes come from `classOf` when known; the swatch then carries the
 * class color (the type name always says what it is).
 */
export function TypeChips({
  byType,
  classOf,
  max = 3,
}: {
  byType: Record<string, number>
  classOf?: (type: string) => ViewClass | undefined
  max?: number
}) {
  const { t } = useTranslation()
  const entries = rankEntries(byType)
  const shown = entries.slice(0, max)
  const rest = entries.slice(max)
  return (
    <span className="flex flex-wrap gap-1">
      {shown.map(({ key, value }) => {
        const cls = classOf?.(key)
        return (
          <span key={key} className="tag mono" title={cls ? t(CLASS_LABEL[cls]) : undefined}>
            {cls && <ClassSwatch cls={cls} />}
            <span className="text-ink">{key}</span>
            {value > 1 && <span className="text-ink-3 tabular-nums">{value}</span>}
          </span>
        )
      })}
      {rest.length > 0 && (
        <span className="tag" title={rest.map((r) => `${r.key} ${r.value}`).join(', ')}>
          {t('common.more', { count: rest.length })}
        </span>
      )}
    </span>
  )
}

const MONOGRAM: Record<Agent, string> = {
  'claude-code': 'CC',
  codex: 'CX',
  opencode: 'OC',
  'gemini-cli': 'GC',
  'tutorial-agent': 'TA',
  other: '··',
}

export function AgentIcon({ agent }: { agent: Agent }) {
  return (
    <span
      className="inline-grid h-[18px] w-[22px] flex-none place-items-center rounded-sm border border-line-2 bg-well text-[9px] leading-none font-bold tracking-[0.02em] text-ink-2"
      aria-hidden="true"
    >
      {MONOGRAM[agent] ?? MONOGRAM.other}
    </span>
  )
}

export function agentName(t: (key: string) => string, agent: string): string {
  const key = AGENT_LABEL[agent as Agent]
  return key ? t(key) : agent
}

export function AgentLabel({ agent, version }: { agent: Agent; version?: string | null }) {
  const { t } = useTranslation()
  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      <AgentIcon agent={agent} />
      <span className="truncate text-ink">{agentName(t, agent)}</span>
      {version && <span className="truncate text-meta text-ink-3 tabular-nums">{version}</span>}
    </span>
  )
}
