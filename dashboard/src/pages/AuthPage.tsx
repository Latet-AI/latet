import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { DocsPageLink } from '../components/DocsLinks'
import { LogoMark } from '../components/Icon'
import { CommandLine } from '../components/ui'
import { CMD_DASHBOARD, CMD_DASHBOARD_PRINT, CMD_TUTORIAL } from '../lib/commands'
import { signInOnServer, type LoginRefusal } from '../lib/signin'
import { useAsync } from '../lib/useAsync'

/**
 * Shown whenever the daemon answers 401: the dashboard opens only through a one-time link. In the
 * tutorial's sandbox (the health answer says so) the way back is to start the tutorial again, not
 * `latet dashboard`, which would open the user's own dashboard. On a Latet server (the health answer says so, and until
 * it comes, the page's host: a computer's dashboard is never anywhere but on 127.0.0.1) the link comes from
 * `latet dashboard` on a computer connected to the server.
 */
export function AuthPage({ refusal }: { refusal: LoginRefusal }) {
  const { t } = useTranslation()
  const health = useAsync('health', (signal) => api.health(signal))
  const sandbox = health.data?.tutorial === true
  const server = !sandbox && signInOnServer(health.data, window.location.hostname)
  const said =
    refusal === 'limited'
      ? t('auth.limited')
      : refusal === 'busy'
        ? t('auth.busy')
        : refusal === 'expired'
          ? server
            ? t('auth.serverExpired')
            : t('auth.expired')
          : null
  return (
    <main className="grid min-h-screen place-items-center bg-canvas px-4 py-10">
      <div className="w-full max-w-[480px]">
        <div className="mb-6 flex items-center gap-2.5">
          <LogoMark size={26} animate />
          <span className="text-title font-semibold tracking-[-0.01em] text-ink">{t('app.name')}</span>
          {/* How to open the dashboard: a page that is not signed in cannot ask /me, so its link opens this install's
              own docs when HEAD /_latet/docs/ (public) finds them, docs.latet.ai otherwise. */}
          <span className="ml-auto">
            <DocsPageLink target={sandbox ? 'page.tutorial' : server ? 'page.signInServer' : 'page.signIn'} />
          </span>
        </div>
        <h1 className="font-display text-page font-extrabold tracking-[-0.01em] text-ink">{sandbox ? t('auth.tutorialTitle') : t('auth.title')}</h1>
        {said !== null && (
          <p role="alert" className="mt-3 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
            {said}
          </p>
        )}
        <p className="mt-3 text-body text-ink-2">{sandbox ? t('auth.tutorialBody') : server ? t('auth.serverBody') : t('auth.body')}</p>
        <div className="mt-5">
          <CommandLine command={sandbox ? CMD_TUTORIAL : CMD_DASHBOARD} />
        </div>
        {!sandbox && (
          <>
            <p className="mt-5 text-ui text-ink-3">{t('auth.otherBrowser')}</p>
            <div className="mt-2">
              <CommandLine command={CMD_DASHBOARD_PRINT} />
            </div>
          </>
        )}
        <p className="mt-6 border-t border-line pt-4 text-meta text-ink-3">
          {sandbox ? t('auth.tutorialWhy') : server ? t('auth.serverWhy') : t('auth.why')}
        </p>
      </div>
    </main>
  )
}
