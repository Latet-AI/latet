import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { Icon } from '../../components/Icon'
import { lineText, type LockLine, type PolicyView } from '../lib/policy'

/** "Set by your company" at the top of a section, with what the company's policy sets there. */
export function Locked({ lines }: { lines: LockLine[] | undefined }) {
  const { t } = useTranslation()
  if (!lines?.length) return null
  return (
    <div className="mb-3 flex max-w-[720px] items-start gap-2 rounded-lg border border-line bg-well p-2.5 text-meta text-ink-2">
      <Icon name="lock" size={14} className="mt-0.5 flex-none text-ink" />
      <div>
        <p className="font-medium text-ink">{t('ee.policy.setBy')}</p>
        <ul>
          {lines.map((line) => (
            <li key={`${line.key}:${JSON.stringify(line.vars ?? {})}`}>{lineText(t, line)}</li>
          ))}
        </ul>
      </div>
    </div>
  )
}

/** At the top of Rules: the policy is in force, where your own settings are below it, and the way to the Policy page
 * when this person may open it. */
export function PolicyNotice({ view, linked }: { view: PolicyView | null; linked: boolean }) {
  const { t } = useTranslation()
  if (!view || (view.floors.length === 0 && view.problems.length === 0)) return null
  const inForce = view.floors.length > 0
  return (
    <div className="mb-4 flex max-w-[860px] items-start gap-2.5 rounded-lg border border-line bg-surface p-3 text-ui">
      <Icon name="lock" size={15} className="mt-0.5 flex-none text-ink" />
      <div className="min-w-0 space-y-1">
        <p className="flex flex-wrap items-baseline gap-x-1.5 text-ink">
          {inForce && <span>{t('ee.policy.inForce')}</span>}
          {linked && (
            <Link className="link" to="/policy">
              {t('ee.policy.open')}
            </Link>
          )}
        </p>
        {view.problems.map((problem, i) => (
          <p key={`${i}:${problem}`} className="text-meta text-warn-ink">
            {problem}
          </p>
        ))}
        {view.below.length > 0 && (
          <>
            <p className="text-meta text-ink-2">{t('ee.policy.below')}</p>
            <ul className="mono list-disc pl-5 text-meta text-ink-2">
              {view.below.map((reason, i) => (
                <li key={`${i}:${reason}`}>{reason}</li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}

/** A save the company's policy refused: the settings it names, as the daemon says them. */
export function FloorRefused({ reasons }: { reasons: string[] }) {
  const { t } = useTranslation()
  return (
    <div className="text-crit-ink">
      <p className="flex items-center gap-1.5 font-medium">
        <Icon name="lock" size={13} />
        {t('ee.policy.refused')}
      </p>
      <ul className="mono mt-1 list-disc pl-5 text-meta">
        {reasons.map((reason, i) => (
          <li key={`${i}:${reason}`}>{reason}</li>
        ))}
      </ul>
      <p className="mt-1 text-meta">{t('ee.policy.refusedHint')}</p>
    </div>
  )
}
