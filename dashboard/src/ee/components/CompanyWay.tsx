import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { Banner } from './Banner'

/**
 * What a change that applies to everyone says when this session was not opened with the company sign-in (403,
 * `company-sign-in`): why, and the button that begins that sign-in. The terminal's way stays the way in for everything
 * that is the person's own. No button where the server does not serve single sign-on. An alert for a change that was
 * just refused, brought to the person when it appears (it is the only feedback: no toast repeats it); `quiet`, a
 * status, for a page that says it from the start.
 */
export function CompanySignInWay({ text, quiet = false }: { text?: string; quiet?: boolean }) {
  const { t } = useTranslation()
  const sso = useAsync('company-sign-in', (signal) => api.companySignIn(signal))
  return (
    <Banner
      quiet={quiet}
      className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border border-warn/60 bg-warn/10 px-4 py-3 text-ui text-ink"
    >
      <p className="min-w-0 max-w-[72ch]">{text ?? t('ee.company.needed')}</p>
      {sso.data && (
        <a className="btn btn-primary" href={sso.data.login}>
          {t('auth.company.button')}
        </a>
      )}
    </Banner>
  )
}

/**
 * Said under the page's header after a change was refused because someone else changed what it was made from (the
 * sign-in settings): the page has read them again and shows them as they are now. Brought to the person like the
 * company sign-in's banner.
 */
export function ChangedMeanwhile({ text }: { text: string }) {
  return (
    <Banner className="mb-4 rounded-md border border-warn/60 bg-warn/10 px-4 py-3 text-ui text-ink">
      <p className="max-w-[80ch]">{text}</p>
    </Banner>
  )
}
