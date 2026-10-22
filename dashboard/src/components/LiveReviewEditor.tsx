// The Live Reviewer's settings (Rules): whether new texts wait for your approval, and which.

import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { LiveReviewSettings } from '../api/types'
import { useApprovals } from '../lib/approvals-context'
import { REVIEW_KINDS } from '../lib/reviewer'
import { TitleWithDocs } from './DocsLinks'
import { KIND_LABEL } from './ReviewerEditor'
import { useToast } from './Toast'
import { errorText, Switch } from './ui'

function toggle<T>(list: readonly T[], item: T, on: boolean): T[] {
  return on ? [...list.filter((x) => x !== item), item] : list.filter((x) => x !== item)
}

export function LiveReviewEditor({ value, onChange }: { value: LiveReviewSettings; onChange: (next: LiveReviewSettings) => void }) {
  const { t } = useTranslation()
  const set = <K extends keyof LiveReviewSettings>(key: K, v: LiveReviewSettings[K]) => onChange({ ...value, [key]: v })
  return (
    <div className="space-y-5">
      <Switch
        checked={value.enabled}
        onChange={(on) => set('enabled', on)}
        label={t('liveReview.enabled')}
        description={
          <>
            {t('liveReview.enabledHelp')}{' '}
            <Link to="/approvals" className="link">
              {t('liveReview.open')}
            </Link>
          </>
        }
      />
      <fieldset disabled={!value.enabled} className="space-y-5 disabled:opacity-60">
        <legend className="sr-only">{t('liveReview.title')}</legend>
        <div>
          <TitleWithDocs title={t('liveReview.kinds')} docs="section.liveKinds" />
          <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('liveReview.kindsHelp')}</p>
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {REVIEW_KINDS.map((kind) => (
              <li key={kind}>
                <label className="flex cursor-pointer items-center gap-2 text-ui text-ink">
                  <input
                    type="checkbox"
                    className="accent-[var(--ink)]"
                    checked={value.kinds.includes(kind)}
                    onChange={(e) => set('kinds', toggle(value.kinds, kind, e.target.checked))}
                  />
                  {t(KIND_LABEL[kind])}
                </label>
              </li>
            ))}
          </ul>
        </div>
        <Switch
          checked={value.auto_approve_clean}
          onChange={(on) => set('auto_approve_clean', on)}
          label={t('liveReview.autoApprove')}
          description={t('liveReview.autoApproveHelp')}
        />
        <div>
          <TitleWithDocs title={t('liveReview.timeout')} docs="section.liveTimeout" />
          <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('liveReview.timeoutHelp')}</p>
          <label className="flex items-center gap-2 text-ui text-ink-2">
            <input
              type="number"
              min={10}
              step={10}
              className="field mono w-[170px]"
              value={value.timeout_s ?? ''}
              placeholder={t('liveReview.noLimit')}
              onChange={(e) => {
                const n = Number.parseInt(e.target.value, 10)
                set('timeout_s', e.target.value.trim() === '' || Number.isNaN(n) ? null : Math.max(1, n))
              }}
              aria-label={t('liveReview.timeout')}
            />
            {t('liveReview.timeoutUnit')}
          </label>
          {value.timeout_s !== null && value.timeout_s < 10 && <p className="mt-1 text-meta text-crit-ink">{t('liveReview.timeoutMin')}</p>}
        </div>
      </fieldset>
    </div>
  )
}

/** The Live Reviewer on or off, saved at once in the global settings (Live and Approvals pages). */
export function LiveReviewSwitch({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { live, setLiveEnabled } = useApprovals()
  const enabled = live?.enabled ?? false
  const toggle = async (on: boolean) => {
    try {
      await setLiveEnabled(on)
      toast.show(on ? t('liveReview.turnedOn') : t('liveReview.turnedOff'))
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    }
  }
  return (
    <Switch
      checked={enabled}
      disabled={!live}
      onChange={(on) => void toggle(on)}
      label={t('liveReview.switch')}
      description={compact ? undefined : enabled ? t('liveReview.switchOn') : t('liveReview.switchOff')}
    />
  )
}
