// The License page of the Enterprise Edition: a server's license as the server weighs it now (its state, the
// licensee, the users bought, the days, the features, the notices). Whoever manages the license (the administrators)
// installs a file in its place or removes it here while it can manage; in any other standing, and for everyone else,
// the server's command line does, and the page says so (LicenseChange).

import { useTranslation } from 'react-i18next'
import { Icon } from '../../components/Icon'
import { ErrorState, Loading, Meta, PageHeader } from '../../components/ui'
import { useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { eeApi } from '../api'
import { LicenseChange } from '../components/LicenseChange'
import {
  daysText,
  featureLabel,
  formatLicenseDay,
  noticeTexts,
  reasonKey,
  STATE_LABEL,
  stateTextKey,
  STATE_TONE,
  type Tone,
  weighedOf,
} from '../lib/license'

const TONE: Record<Tone, string> = {
  good: 'border-good/50 bg-good/10 text-good-ink',
  warn: 'border-warn/50 bg-warn/10 text-warn-ink',
  crit: 'border-crit/50 bg-crit/10 text-crit-ink',
  neutral: 'text-ink-2',
}

export function LicensePage() {
  const { t } = useTranslation()
  const fmt = useFormat()
  const data = useAsync('ee-license', (signal) => eeApi.license(signal))
  const day = (iso: string) => formatLicenseDay(fmt.locale, iso)

  const header = <PageHeader title={t('ee.license.title')} description={t('ee.license.descriptionServer')} />
  if (data.loading) return <>{header}<Loading /></>
  if (!data.data) return <>{header}<ErrorState error={data.error} onRetry={data.reload} /></>
  const standing = data.data
  const license = standing.license
  // A file that cannot be used, and a trial that ended, are said by the state's sentence: not again as a notice.
  const shown = standing.notices.filter((n) => n !== 'invalid' && n !== 'trial-ended')
  const notices = noticeTexts({ ...weighedOf(standing), notices: shown }, day)
  const days = standing.days_left === null ? null : daysText(standing.days_left)

  return (
    <div className={data.stale ? 'opacity-60' : undefined}>
      {header}
      <section className="panel space-y-4 p-5" aria-labelledby="license-state">
        <p className="flex flex-wrap items-center gap-2">
          <span id="license-state" data-license-state={standing.state} className={`tag ${TONE[STATE_TONE[standing.state]]}`}>
            {t(STATE_LABEL[standing.state])}
          </span>
          {license?.trial && <span className="tag">{t('ee.license.trial')}</span>}
        </p>
        <p className="max-w-[72ch] text-body text-ink-2">
          {t(stateTextKey(standing), {
            date: standing.pauses_on ? day(standing.pauses_on) : '',
            reason: t(reasonKey(standing.reason_code)),
          })}
        </p>
        {notices.length > 0 && (
          <ul className="space-y-1.5">
            {notices.map((notice) => (
              <li key={notice.key} className={`flex items-start gap-2 text-ui ${notice.tone === 'crit' ? 'text-crit-ink' : 'text-warn-ink'}`}>
                <Icon name="info" size={14} className="mt-0.5 flex-none" />
                <span>{t(notice.key, notice.values)}</span>
              </li>
            ))}
          </ul>
        )}
        {license && (
          <dl className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-x-6 gap-y-3 border-t border-line pt-4">
            <Meta label={t('ee.license.fields.licensee')}>{license.licensee}</Meta>
            <Meta label={t('ee.license.fields.id')}>
              <span className="mono">{license.id}</span>
            </Meta>
            <Meta label={t('ee.license.fields.users')}>{fmt.number(license.users)}</Meta>
            {standing.users_in_use !== null && (
              <Meta label={t('ee.license.fields.inUse')} wrap>
                {fmt.number(standing.users_in_use)}
                <span className="block text-meta text-ink-3">{t('ee.license.inUseHint')}</span>
              </Meta>
            )}
            <Meta label={t('ee.license.fields.issued')}>{day(license.issued)}</Meta>
            <Meta label={t('ee.license.fields.expires')} wrap>
              {day(license.expires)}
              {days && <span className="text-ink-3"> · {t(days.key, { count: days.count })}</span>}
            </Meta>
          </dl>
        )}
        {license && (
          <div>
            <h2 className="text-meta text-ink-3">{t('ee.license.fields.features')}</h2>
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {license.features.map((feature) => (
                <li key={feature} className="tag">
                  {featureLabel(t, feature)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <LicenseChange standing={standing} reload={data.reload} />
    </div>
  )
}
