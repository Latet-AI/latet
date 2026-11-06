// What Latet read of a request and what let it through, on the exchange's page (the request-coverage plan's P2-7):
// stopped before it was sent or after an attempt, every part read, the built-in rules and the exceptions it passed
// under, how its launch and its project were decided, and what its profile removed. From the history's safe summary
// alone: categories and counts, never a value; a refusal said in the reader's language from its safe reason code.

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { ExchangeSummary, LaunchCategory, PassCategory } from '../../api/types'
import { tk } from '../../i18n'
import {
  coverageOutcome,
  launchOf,
  projectBinding,
  refusalCategory,
  remedyOf,
  removedNote,
  type RefusalCategory,
  type Remedy,
} from '../../lib/coverage'
import { useDocsHref } from '../DocsLinks'
import { Tooltip } from '../Floating'
import { Icon, StatusGlyph } from '../Icon'

export const PASS_LABEL: Record<PassCategory, string> = {
  'checked-only': tk('coverage.pass.checked-only'),
  'image-remainder': tk('coverage.pass.image-remainder'),
  'sent-before': tk('coverage.pass.sent-before'),
  'model-written': tk('coverage.pass.model-written'),
  'provider-echo': tk('coverage.pass.provider-echo'),
  'provider-encrypted': tk('coverage.pass.provider-encrypted'),
  exception: tk('coverage.pass.exception'),
}

export const LAUNCH_LABEL: Record<LaunchCategory, string> = {
  launched: tk('coverage.launch.launched'),
  'launched-key-missing': tk('coverage.launch.launched-key-missing'),
  trusted: tk('coverage.launch.trusted'),
  claimed: tk('coverage.launch.claimed'),
}

export const LAUNCH_HELP: Record<LaunchCategory, string> = {
  launched: tk('coverage.launch.launchedHelp'),
  'launched-key-missing': tk('coverage.launch.keyMissingHelp'),
  trusted: tk('coverage.launch.trustedHelp'),
  claimed: tk('coverage.launch.claimedHelp'),
}

const REFUSAL: Record<RefusalCategory, string> = {
  header: tk('coverage.refusal.header'),
  query: tk('coverage.refusal.query'),
  'body-member': tk('coverage.refusal.body-member'),
  'body-member-closed': tk('coverage.refusal.body-member-closed'),
  route: tk('coverage.refusal.route'),
  expired: tk('coverage.refusal.expired'),
  resource: tk('coverage.refusal.resource'),
  sensitive: tk('coverage.refusal.sensitive'),
  auth: tk('coverage.refusal.auth'),
  egress: tk('coverage.refusal.egress'),
  parse: tk('coverage.refusal.parse'),
  limit: tk('coverage.refusal.limit'),
  state: tk('coverage.refusal.state'),
  settings: tk('coverage.refusal.settings'),
  masking: tk('coverage.refusal.masking'),
  other: tk('coverage.refusal.other'),
}

const REMEDY: Record<Remedy, string> = {
  exception: tk('coverage.remedy.exception'),
  'exception-docs': tk('coverage.remedy.exception-docs'),
  renew: tk('coverage.remedy.renew'),
  resource: tk('coverage.remedy.resource'),
  again: tk('coverage.remedy.again'),
  websocket: tk('coverage.remedy.websocket'),
  none: tk('coverage.remedy.none'),
}

function Line({ status, children }: { status: 'ok' | 'warn' | 'fail'; children: ReactNode }) {
  return (
    <li className="flex items-start gap-2">
      <span className="mt-0.5">
        <StatusGlyph status={status} size={14} />
      </span>
      <span className="min-w-0">{children}</span>
    </li>
  )
}

/** A launch status as a tag, its meaning in a tooltip: on a list's row, a session's timeline. */
export function LaunchBadge({ launch, focusable = false }: { launch: LaunchCategory; focusable?: boolean }) {
  const { t } = useTranslation()
  const warn = launch === 'launched-key-missing'
  return (
    <Tooltip content={t(LAUNCH_HELP[launch])} focusable={focusable}>
      <span className={`tag h-auto min-h-5 py-0.5 leading-tight whitespace-normal ${warn ? 'border-warn/60 bg-warn/12 text-ink' : 'border-line-2 text-ink-2'}`} data-launch={launch}>
        <Icon name={launch === 'launched' || launch === 'trusted' ? 'key' : 'question'} size={12} />
        {t(LAUNCH_LABEL[launch])}
      </span>
    </Tooltip>
  )
}

/** The request's coverage, one line each: never a green line for what left unread. */
export function CoverageList({ summary }: { summary: ExchangeSummary }) {
  const { t } = useTranslation()
  const outcome = coverageOutcome(summary)
  const launch = launchOf(summary)
  const binding = projectBinding(summary)
  const removed = removedNote(summary)
  const refusal = outcome.kind === 'blocked' ? refusalCategory(summary.block_reason) : null
  const approving = useDocsHref('section.approveException')
  return (
    <section aria-label={t('coverage.title')} className="mt-3">
      <ul className="space-y-1 text-ui text-ink" data-coverage>
        {outcome.kind === 'blocked' && (
          <Line status="fail">
            {outcome.sent === 'before' ? t('coverage.blocked.before') : t('coverage.blocked.attempted')}
          </Line>
        )}
        {refusal !== null && (
          <Line status="fail">
            {t(REFUSAL[refusal])} <span className="text-ink-2">{t(REMEDY[remedyOf(summary, refusal)])}</span>
            {remedyOf(summary, refusal) === 'exception-docs' && (
              <>
                {' '}
                <a href={approving} target="_blank" rel="noreferrer" className="link">
                  {t('coverage.remedy.exceptionDocsLink')}
                </a>
              </>
            )}
          </Line>
        )}
        {outcome.kind === 'read' && <Line status="ok">{t('coverage.outcome.read')}</Line>}
        {outcome.kind === 'passed' && (
          <Line status={outcome.unread ? 'warn' : 'ok'}>
            {t('coverage.outcome.passed')}{' '}
            {outcome.passes.map((pass, i) => (
              <span key={pass}>
                {i > 0 && ', '}
                <span data-pass={pass}>{t(PASS_LABEL[pass])}</span>
              </span>
            ))}
            {outcome.unread && <span className="block text-meta text-ink-2">{t('coverage.outcome.unread')}</span>}
          </Line>
        )}
        {outcome.kind === 'unknown' && <Line status="warn">{t('coverage.outcome.unknown')}</Line>}
        {launch !== null && (
          <Line status={launch === 'launched-key-missing' ? 'warn' : launch === 'claimed' ? 'warn' : 'ok'}>
            <span data-launch={launch}>{t(LAUNCH_LABEL[launch])}</span>
            <span className="block text-meta text-ink-2">{t(LAUNCH_HELP[launch])}</span>
          </Line>
        )}
        {binding !== null && summary.project && (
          <Line status={binding === 'trusted' ? 'ok' : 'warn'}>
            <span data-binding={binding}>
              {binding === 'trusted'
                ? t('coverage.project.trusted', { name: summary.project.name })
                : t('coverage.project.claimed', { name: summary.project.name })}
            </span>
          </Line>
        )}
        {removed !== null && (
          <Line status={removed.authAfter ? 'fail' : 'warn'}>
            {removed.headers > 0 && t('coverage.removed.headers', { count: removed.headers })}
            {removed.headers > 0 && removed.members > 0 && ' '}
            {removed.members > 0 && t('coverage.removed.members', { count: removed.members })}
            {removed.authAfter && (
              <span className="block text-meta text-crit-ink" data-diagnostic>
                {t('coverage.removed.authAfter', { status: summary.status })}
              </span>
            )}
          </Line>
        )}
        {summary.profile && (
          <li className="text-meta text-ink-3">
            {t('coverage.profile')} <code className="mono text-ink-2">{summary.profile}</code>
          </li>
        )}
      </ul>
    </section>
  )
}
