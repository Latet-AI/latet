// An exchange's blocks as the local reviewer read them: what it masked, what it only suggested.

import { useContext, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { Slot } from '../api/types'
import { KIND_LABEL } from '../lib/labels'
import { useDeploymentKey, useMe } from '../lib/me'
import { savedChangePending } from '../lib/refused'
import { CATEGORY_TYPE, reviewsOf, type ShownFinding } from '../lib/reviewer'
import { useAsync } from '../lib/useAsync'
import { tk } from '../i18n'
import { DocsSectionLink } from './DocsLinks'
import { Icon } from './Icon'
import { RuleScope } from './RuleName'
import { CATEGORY_LABEL } from './ReviewerEditor'
import { useToast } from './Toast'
import { errorText } from './ui'
import { chooseValue, useWhere, WhereChoice } from './ValueScope'

const READ_WHEN = { inline: tk('reviewer.panel.inline'), background: tk('reviewer.panel.background') }
const FAILED_OTHER = tk('reviewer.failed.other')

const FAILED_LABEL: Record<string, string> = {
  timeout: tk('reviewer.failed.timeout'),
  unreachable: tk('reviewer.failed.unreachable'),
  unreadable: tk('reviewer.failed.unreadable'),
  suspect: tk('reviewer.failed.suspect'),
  busy: tk('reviewer.failed.busy'),
}

/**
 * `only`: one of `slots`, the block whose reviews to list (the tutorial's pane shows one block of a request); the
 * blocks are numbered by their place in `slots` all the same, as the request numbers them.
 */
export function ReviewPanel({ session, slots, only }: { session: string; slots: Slot[]; only?: Slot }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const reviews = useAsync(`reviews:${session}`, (signal) => api.reviews(session, 500, signal))
  // Older records do not say whether a value was left in clear as Not sensitive: your lists do.
  const config = useAsync('config', (signal) => api.config(signal))
  const scope = useContext(RuleScope)
  const project = useAsync(scope ? `project:${scope.projectId}` : null, (signal) => api.project(scope?.projectId ?? '', signal))
  const notSensitive = useMemo(
    () => new Set([...(config.data?.allow_values ?? []), ...(project.data?.personal?.allow_values ?? [])]),
    [config.data, project.data],
  )
  const shown = useMemo(
    () =>
      reviewsOf(slots, reviews.data?.items ?? [])
        .filter(({ review }) => only === undefined || review.block === only.block)
        .map((review) => ({
          ...review,
          findings: review.findings.map((f) => ({ ...f, allowed: f.allowed ?? (!f.masked && f.text !== null && notSensitive.has(f.text)) })),
        })),
    [slots, only, reviews.data, notSensitive],
  )
  const { hasProject, where, setWhere, projectId } = useWhere()
  if (shown.length === 0) return null
  const suggested = shown.some(({ findings }) => findings.some((f) => !f.masked && !f.allowed && f.text))
  return (
    <section aria-labelledby="reviewer-panel" className="mt-6">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <h2 id="reviewer-panel" className="text-title font-semibold text-ink">
            {t('reviewer.panel.title')}
          </h2>
          <DocsSectionLink target="section.reviewerPanel" section={t('reviewer.panel.title')} />
        </div>
        {hasProject && suggested && <WhereChoice where={where} onChange={setWhere} />}
      </div>
      <p className="mb-2 max-w-[90ch] text-ui text-ink-2">{t('reviewer.panel.description')}</p>
      <ul className="space-y-2">
        {shown.map(({ review, slot, findings }) => (
          <li key={review.id} className="rounded-lg border border-line bg-surface p-3">
            <p className="flex flex-wrap items-center gap-2 text-meta text-ink-3">
              <span className="font-medium text-ink-2">{t(review.inline ? READ_WHEN.inline : READ_WHEN.background)}</span>
              <span>{t('reviewer.panel.kind', { kind: t(key(KIND_LABEL[review.kind])) })}</span>
              <span>{t('reviewer.panel.seconds', { seconds: review.seconds })}</span>
              {slot && slot.new && (
                <a href={`#block-${slots.indexOf(slot)}`} className="link">
                  {t('reviewer.panel.block', { number: slots.indexOf(slot) + 1 })}
                </a>
              )}
              {/* History is folded under All: named, not linked. */}
              {slot && !slot.new && <span>{t('reviewer.panel.blockHistory', { number: slots.indexOf(slot) + 1 })}</span>}
            </p>
            {review.failed && (
              <p className="mt-1.5 flex items-center gap-1.5 text-meta text-crit-ink">
                <Icon name="x" size={13} />
                {t(FAILED_LABEL[review.failed] ?? FAILED_OTHER)}
              </p>
            )}
            {review.block_asked && <p className="mt-1.5 text-meta text-warn-ink">{t('reviewer.panel.blockAsked')}</p>}
            {findings.length > 0 && (
              <ul className="mt-2 divide-y divide-line">
                {findings.map((finding, i) => (
                  <FindingRow key={i} finding={finding} projectId={projectId} />
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

type Action = 'idle' | 'busy' | 'done' | 'pending'

function FindingRow({ finding, projectId }: { finding: ShownFinding; projectId: string | null }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const [state, setState] = useState<Action>('idle')
  const act = async (what: 'mask' | 'allow') => {
    if (!finding.text) return
    setState('busy')
    try {
      const value = finding.text
      await chooseValue(what === 'mask' ? { kind: 'mask', value, type: CATEGORY_TYPE[finding.category] } : { kind: 'allow', value }, projectId)
      setState('done')
      if (what === 'mask') toast.show(projectId ? t('reviewer.panel.termAddedInProject') : t('reviewer.panel.termAdded'))
      else toast.show(projectId ? t('reviewer.panel.allowedInProject') : t('reviewer.panel.allowed'))
    } catch (error) {
      const pending = savedChangePending(error)
      setState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
      toast.show(errorText(t, error), 'fail')
    }
  }
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-2">
      <span className="flex min-w-0 flex-wrap items-center gap-2 text-ui">
        <code className="mono break-all text-ink">{finding.text ?? t('common.none')}</code>
        <span className="text-meta text-ink-3">{t(CATEGORY_LABEL[finding.category])}</span>
        <span className={`text-meta ${finding.masked || finding.allowed ? 'text-ink-2' : 'text-warn-ink'}`}>
          {finding.masked ? t('reviewer.panel.masked') : finding.allowed ? t('reviewer.panel.allowedByYou') : t('reviewer.panel.suggested')}
        </span>
      </span>
      {!finding.masked && !finding.allowed && finding.text && (
        <span className="flex gap-1.5">
          <button type="button" className="btn btn-sm" disabled={state !== 'idle'} onClick={() => void act('mask')}>
            <Icon name="eyeOff" size={13} />
            {t('reviewer.panel.alwaysMask')}
          </button>
          <button type="button" className="btn btn-sm" disabled={state !== 'idle'} onClick={() => void act('allow')}>
            <Icon name="eye" size={13} />
            {t('reviewer.panel.notSensitive')}
          </button>
        </span>
      )}
      {state === 'pending' && <p className="w-full text-meta text-warn-ink">{t('errors.settingsSavedChangePending')}</p>}
    </li>
  )
}
