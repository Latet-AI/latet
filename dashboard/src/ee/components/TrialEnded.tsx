// Once a Latet Enterprise trial ended, every Enterprise page but License is read-only: what it holds can be read, not
// changed. EnterpriseOnly (App.tsx) shows this notice above the page and sets EnterpriseReadOnly; each Enterprise page
// calls useEnterpriseReadOnly() and, while it is true, shows no editing control and no action. License is never
// read-only: there a server's license is shown, and its administrators install or remove it while it can manage, else from the command line.

import { createContext, useContext, useId } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'

/** Whether the Enterprise page inside is read-only because a trial ended (false everywhere else). */
export const EnterpriseReadOnly = createContext(false)

/** True on an Enterprise page once a trial ended: the page hides its editing controls and its actions. */
export function useEnterpriseReadOnly(): boolean {
  return useContext(EnterpriseReadOnly)
}

/** The notice above a read-only Enterprise page: the trial ended, what it means, and the way to the License page. */
export function TrialEnded() {
  const { t } = useTranslation()
  const titleId = useId()
  return (
    <section className="panel mb-5 flex flex-wrap items-center justify-between gap-x-4 gap-y-3 p-5" aria-labelledby={titleId}>
      <div className="min-w-0 max-w-[72ch] space-y-1">
        <h2 id={titleId} className="text-title font-semibold text-ink">
          {t('ee.trialEnded.title')}
        </h2>
        <p className="text-body text-ink-2">{t('ee.trialEnded.body')}</p>
      </div>
      <Link to="/license" className="btn btn-sm">
        {t('ee.trialEnded.open')}
      </Link>
    </section>
  )
}
