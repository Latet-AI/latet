// A notice about the Latet Enterprise license at the top of every page, for whoever can act on it: it ends soon, it
// ended, a trial ends in a week or tomorrow or has ended, more people and API keys use Latet than it covers, or its
// file cannot be used. It leads to the License page.

import { useTranslation } from 'react-i18next'
import { Link, useLocation } from 'react-router'
import { Icon } from '../../components/Icon'
import { useMe } from '../../lib/me'
import { useFormat } from '../../lib/useFormat'
import { formatLicenseDay, licenseBanner } from '../lib/license'

export function LicenseBar() {
  const { t } = useTranslation()
  const { me } = useMe()
  const { pathname } = useLocation()
  const fmt = useFormat()
  const notice = licenseBanner(me, (iso) => formatLicenseDay(fmt.locale, iso))
  if (notice === null || pathname === '/license') return null
  const crit = notice.tone === 'crit'
  return (
    <div
      role="region"
      aria-label={t('ee.license.bar.label')}
      className={`mb-5 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border px-3 py-2 text-ui text-ink ${
        crit ? 'border-crit/50 bg-crit/10' : 'border-warn/50 bg-warn/10'
      }`}
    >
      <p className="flex items-center gap-2">
        <Icon name="key" size={15} className={`flex-none ${crit ? 'text-crit-ink' : 'text-warn-ink'}`} />
        {t(notice.key, notice.values)}
      </p>
      <Link to="/license" className="btn btn-sm">
        {t('ee.license.bar.open')}
      </Link>
    </div>
  )
}
