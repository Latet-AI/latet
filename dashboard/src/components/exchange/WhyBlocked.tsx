import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { AutoModeReview, ExchangeDetail } from '../../api/types'
import { tk } from '../../i18n'
import { explainBlock, outboundTools, outboundValues, type BlockExplanation, type BlockKind, type OutboundValue } from '../../lib/blocks'
import { BLOCK_DOCS, LIVE_DOCS } from '../../lib/docsPlaces'
import { DocsWhyLink } from '../DocsLinks'
import { flagDetail, REVIEW_DETAIL_LABEL } from '../../lib/labels'
import { useFormat } from '../../lib/useFormat'
import { useDeploymentKey, useMe } from '../../lib/me'
import { savedChangePending } from '../../lib/refused'
import { Icon } from '../Icon'
import { ANCHORS } from '../rules'
import { useToast } from '../Toast'
import { errorText } from '../ui'
import { chooseValue } from '../ValueScope'

// Where in Rules each kind of block can be changed.
const RULES_AT: Partial<Record<BlockKind, string>> = {
  review: ANCHORS.identity,
  uninspected: ANCHORS.profile,
  imagesSetting: ANCHORS.profile,
  imagesMask: ANCHORS.profile,
  localReviewer: ANCHORS.reviewer,
  live: ANCHORS.live,
}

// `unmarked`: what to do when the page cannot mark the values in the request (the history keeps no texts).
const COPY: Record<
  Exclude<BlockKind, 'live' | 'outbound'>,
  { title: string; happened: string; meant: string; todo: string; unmarked?: string }
> = {
  review: {
    title: tk('exchange.why.review.title'),
    happened: tk('exchange.why.review.happened'),
    meant: tk('exchange.why.review.meant'),
    todo: tk('exchange.why.review.todo'),
  },
  history: {
    title: tk('exchange.why.history.title'),
    happened: tk('exchange.why.history.happened'),
    meant: tk('exchange.why.history.meant'),
    todo: tk('exchange.why.history.todo'),
  },
  plugin: {
    title: tk('exchange.why.plugin.title'),
    happened: tk('exchange.why.plugin.happened'),
    meant: tk('exchange.why.plugin.meant'),
    todo: tk('exchange.why.plugin.todo'),
    unmarked: tk('exchange.why.plugin.todoUnmarked'),
  },
  uninspected: {
    title: tk('exchange.why.uninspected.title'),
    happened: tk('exchange.why.uninspected.happened'),
    meant: tk('exchange.why.uninspected.meant'),
    todo: tk('exchange.why.uninspected.todo'),
  },
  imagesSetting: {
    title: tk('exchange.why.imagesSetting.title'),
    happened: tk('exchange.why.imagesSetting.happened'),
    meant: tk('exchange.why.imagesSetting.meant'),
    todo: tk('exchange.why.imagesSetting.todo'),
  },
  imagesMask: {
    title: tk('exchange.why.imagesMask.title'),
    happened: tk('exchange.why.imagesMask.happened'),
    meant: tk('exchange.why.imagesMask.meant'),
    todo: tk('exchange.why.imagesMask.todo'),
  },
  localReviewer: {
    title: tk('exchange.why.localReviewer.title'),
    happened: tk('exchange.why.localReviewer.happened'),
    meant: tk('exchange.why.localReviewer.meant'),
    todo: tk('exchange.why.localReviewer.todo'),
  },
  leak: {
    title: tk('exchange.why.leak.title'),
    happened: tk('exchange.why.leak.happened'),
    meant: tk('exchange.why.leak.meant'),
    todo: tk('exchange.why.leak.todo'),
    unmarked: tk('exchange.why.leak.todoUnmarked'),
  },
  other: {
    title: tk('exchange.why.other.title'),
    happened: tk('exchange.why.other.happened'),
    meant: tk('exchange.why.other.meant'),
    todo: tk('exchange.why.other.todo'),
  },
}

const LIVE_COPY = {
  rejected: {
    title: tk('exchange.why.live.rejected.title'),
    happened: tk('exchange.why.live.rejected.happened'),
    meant: tk('exchange.why.live.meant'),
    todo: tk('exchange.why.live.rejected.todo'),
  },
  expired: {
    title: tk('exchange.why.live.expired.title'),
    happened: tk('exchange.why.live.expired.happened'),
    meant: tk('exchange.why.live.meant'),
    todo: tk('exchange.why.live.expired.todo'),
  },
  abandoned: {
    title: tk('exchange.why.live.abandoned.title'),
    happened: tk('exchange.why.live.abandoned.happened'),
    meant: tk('exchange.why.live.meant'),
    todo: tk('exchange.why.live.abandoned.todo'),
  },
}

/** Why a request was stopped, in plain words: what happened, what it meant for you, what you can do. */
export function WhyBlocked({ detail }: { detail: ExchangeDetail }) {
  const why = explainBlock(detail)
  if (!why) return null
  if (why.kind === 'outbound') return <OutboundCard detail={detail} blocked />
  return <WhyCard why={why} leakTypes={[...new Set(detail.leaks.map((leak) => leak.type))]} />
}

function WhyCard({ why, leakTypes }: { why: BlockExplanation; leakTypes: string[] }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const copy = why.kind === 'live' ? LIVE_COPY[why.live ?? 'rejected'] : COPY[why.kind as Exclude<BlockKind, 'live' | 'outbound'>]
  const values = {
    details: fmt.list(why.details.map((d) => t(REVIEW_DETAIL_LABEL[d]))),
    others: fmt.list(why.others),
    types: fmt.list(leakTypes),
    what: fmt.list(why.uninspected.map((d) => flagDetail({ key: 'uninspected', detail: d, raw: d }, t))),
    reason: why.given ?? t('common.none'),
  }
  // Values that stopped the request are marked in it, to click: Not sensitive is there. Without the texts, in Rules.
  const unmarked = !why.marked && 'unmarked' in copy ? copy.unmarked : undefined
  const anchor = unmarked ? ANCHORS.allow : RULES_AT[why.kind]
  return (
    <section aria-labelledby="why-title" className="max-w-[110ch] rounded-lg border border-crit/50 bg-crit/6 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="why-title" className="flex items-center gap-2 text-title font-semibold text-ink">
          <Icon name="ban" className="text-crit-ink" />
          {t(copy.title, values)}
        </h2>
        <span className="text-ui">
          <DocsWhyLink target={why.kind === 'live' ? LIVE_DOCS[why.live ?? 'rejected'] : BLOCK_DOCS[why.kind as Exclude<BlockKind, 'live'>]} />
        </span>
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-3 text-ui sm:grid-cols-[170px_minmax(0,1fr)]">
        <Row label={t('exchange.why.happened')}>
          {why.kind === 'review' && why.details.length === 0 ? t('exchange.why.review.happenedOthers', values) : t(key(copy.happened), values)}
          {why.kind === 'review' && why.details.length > 0 && why.others.length > 0 && <> {t('exchange.why.review.alsoOthers', values)}</>}
        </Row>
        <Row label={t('exchange.why.meant')}>{t(key(copy.meant), values)}</Row>
        <Row label={t('exchange.why.todo')}>
          <p>{why.kind === 'review' && why.details.length === 0 ? t('exchange.why.review.todoOthers') : t(key(unmarked ?? copy.todo), values)}</p>
          {why.kind === 'review' && why.details.length > 0 && (
            <ChangeReview
              change={allowed(why)}
              label={t('exchange.why.review.allow', { details: values.details })}
              done={t('exchange.why.review.done', { details: values.details })}
            />
          )}
          {why.kind === 'review' && fakeNameHelps(why) && (
            <>
              <p className="mt-3">{t('exchange.why.review.surrogate')}</p>
              <ChangeReview
                change={FAKE_NAME}
                label={t('exchange.why.review.surrogateAllow')}
                done={t('exchange.why.review.surrogateDone')}
                note={t('exchange.why.review.surrogateNote')}
              />
            </>
          )}
          {anchor && (
            <Link to={`/rules#${anchor}`} className="link mt-1.5 inline-block text-meta">
              {t('exchange.why.inRules')}
            </Link>
          )}
        </Row>
      </dl>
      {why.given && (
        <details className="mt-3">
          <summary className="cursor-pointer text-meta text-ink-3">{t('exchange.why.given')}</summary>
          <blockquote className="mt-1 border-l-2 border-crit/60 pl-3 text-meta break-words text-ink-2">{why.given}</blockquote>
        </details>
      )}
    </section>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-ink">{label}</dt>
      <dd className="text-ink-2">{children}</dd>
    </>
  )
}

/** Every identity detail the review carried, let through. */
function allowed(why: BlockExplanation): Partial<AutoModeReview> {
  const out: Partial<AutoModeReview> = {}
  for (const detail of why.details) out[detail] = 'allow'
  return out
}

/** The reviewer is told the user name's surrogate, and your user folder in paths goes through. */
const FAKE_NAME: Partial<AutoModeReview> = { user_name: 'surrogate', paths: 'allow' }

/** A fake user name helps when the review carried your user name, and nothing else but your user folder. */
function fakeNameHelps(why: BlockExplanation): boolean {
  return why.details.includes('user_name') && why.details.every((d) => d === 'user_name' || d === 'paths')
}

/** Auto mode's reviewer: change what of your identity reaches it, from now on. */
function ChangeReview({ change, label, done, note }: { change: Partial<AutoModeReview>; label: string; done: string; note?: string }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'pending'>('idle')
  if (state === 'done') return <p className="mt-2 text-ink">{done}</p>
  if (state === 'pending') return <p className="mt-2 text-warn-ink">{t('errors.settingsSavedChangePending')}</p>
  const apply = async () => {
    setState('busy')
    try {
      await api.changeConfig((config) => ({ auto_mode_review: { ...config.auto_mode_review, ...change } }))
      setState('done')
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
      const pending = savedChangePending(error)
      setState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
    }
  }
  return (
    <>
      <button type="button" className="btn mt-2" disabled={state === 'busy'} onClick={() => void apply()}>
        <Icon name="check" size={14} />
        {label}
      </button>
      {note && <p className="mt-1 text-meta text-ink-3">{note}</p>}
    </>
  )
}

/** A connector's tool as people know it: "Gmail send_message", not its full name. */
function toolName(tool: string): string {
  return tool.replace(/^mcp__claude_ai_/, '').replaceAll('__', ' ')
}

/**
 * A tool that runs outside this computer and got (or would have got) a stand-in: stopped by the
 * Claude Code plugin before it ran (`blocked`), or run by the provider already.
 */
export function OutboundCard({ detail, blocked }: { detail: ExchangeDetail; blocked: boolean }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const values = outboundValues(detail.request.slots)
  const tools = fmt.list(outboundTools(detail.summary.flags).map(toolName))
  const types = fmt.list([...new Set(values.map((v) => v.type))])
  const vars = { tools, types: types || t('exchange.why.outbound.aValue') }
  const tone = blocked ? 'border-crit/50 bg-crit/6' : 'border-warn/60 bg-warn/10'
  return (
    <section aria-labelledby="why-title" className={`max-w-[110ch] rounded-lg border p-4 ${tone}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="why-title" className="flex items-center gap-2 text-title font-semibold text-ink">
          <Icon name={blocked ? 'ban' : 'external'} className={blocked ? 'text-crit-ink' : 'text-warn'} />
          {blocked ? t('exchange.why.outbound.title', vars) : t(key('exchange.why.outbound.ranTitle'), vars)}
        </h2>
        <span className="text-ui">
          <DocsWhyLink target={BLOCK_DOCS.outbound} />
        </span>
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-3 text-ui sm:grid-cols-[170px_minmax(0,1fr)]">
        <Row label={t('exchange.why.happened')}>
          {blocked ? t(key('exchange.why.outbound.happened'), vars) : t('exchange.why.outbound.ranHappened', vars)}
        </Row>
        <Row label={t('exchange.why.meant')}>{blocked ? t('exchange.why.outbound.meant') : t(key('exchange.why.outbound.ranMeant'))}</Row>
        <Row label={t('exchange.why.todo')}>
          <p>{blocked ? t(key('exchange.why.outbound.todo')) : t('exchange.why.outbound.ranTodo')}</p>
          <ul className="mt-2 space-y-1.5">
            {values.map((value) => (
              <NotSensitive key={value.value} value={value} projectId={detail.summary.project?.id ?? null} />
            ))}
          </ul>
        </Row>
      </dl>
    </section>
  )
}

function NotSensitive({ value, projectId }: { value: OutboundValue; projectId: string | null }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'pending'>('idle')
  if (state === 'done') return <li className="text-ink">{t('exchange.why.outbound.allowed', { value: value.value })}</li>
  if (state === 'pending') return <li className="text-warn-ink">{t('errors.settingsSavedChangePending')}</li>
  const allow = async () => {
    setState('busy')
    try {
      await chooseValue({ kind: 'allow', value: value.value }, projectId)
      setState('done')
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
      const pending = savedChangePending(error)
      setState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
    }
  }
  return (
    <li className="flex flex-wrap items-center gap-2">
      <button type="button" className="btn" disabled={state === 'busy'} onClick={() => void allow()}>
        <Icon name="check" size={14} />
        {t('exchange.why.outbound.allowValue', { value: value.value })}
      </button>
      <span className="text-meta text-ink-3">{t('exchange.why.outbound.sentAs', { sent: value.sent })}</span>
    </li>
  )
}
