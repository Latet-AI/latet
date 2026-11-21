import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { NOTICE_TEXT, signInNotice } from '../lib/signIn'

/**
 * The sign-in page of a server with single sign-on: the button to the company's identity provider, and why the last
 * sign-in did not happen (`?sign-in=`). Nothing where single sign-on is off. The terminal's way stays under it, and
 * alone where the server answers 404 (a trial that ended, a license without single sign-on): nobody is kept out of
 * their own dashboard.
 */
export function CompanySignIn() {
  const { t } = useTranslation()
  const sso = useAsync('company-sign-in', (signal) => api.companySignIn(signal))
  // The reason is read from the address the page was opened with, once: the router moves the address on, and the reason
  // would go with it.
  const notice = useMemo(() => signInNotice(window.location.search), [])
  if (!sso.data && !notice) return null
  return (
    <div className="mt-5">
      {notice && (
        <p role="alert" className="mb-3 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
          {t(NOTICE_TEXT[notice])}
        </p>
      )}
      {sso.data && (
        <>
          <a className="btn btn-primary" href={sso.data.login}>
            {t('auth.company.button')}
          </a>
          <p className="mt-5 text-ui text-ink-3">{t('auth.company.or')}</p>
        </>
      )}
    </div>
  )
}
