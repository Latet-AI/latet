import { useState, type ChangeEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, useLocation } from 'react-router'
import { api, isMock, notifyUnauthorized } from '../api/client'
import { LANGUAGE_NAMES, LANGUAGES, setLanguage, tk, type Language } from '../i18n'
import { useApprovals } from '../lib/approvals-context'
import { PROFILE_LABEL } from '../lib/labels'
import { useDeploymentKey, useMe } from '../lib/me'
import { pageOnServer, serverHost } from '../lib/serverPage'
import { useTheme, type ThemePref } from '../lib/theme'
import { Icon, LogoMark, type IconName } from './Icon'
import { SectionsProvider, useActiveSection, usePageSections } from './sections'
import { useToast } from './Toast'
import { TutorialBar } from './tutorial/TutorialBar'
import { errorText, Segmented } from './ui'

interface NavItem {
  to: string
  label: string
  icon: IconName
  /** Shows how many texts, and changes to your settings, wait for your approval. */
  count?: 'approvals'
}

// The tutorial's sandbox has its chapters first, as a group of their own.
const TUTORIAL_NAV: NavItem[] = [{ to: '/tutorial', label: tk('nav.tutorial'), icon: 'book' }]

// Groups: what happened, what Latet knows and does, how it is wired up.
const NAV: NavItem[][] = [
  [
    { to: '/live', label: tk('nav.live'), icon: 'live' },
    { to: '/approvals', label: tk('nav.approvals'), icon: 'approvals', count: 'approvals' },
    { to: '/sessions', label: tk('nav.sessions'), icon: 'sessions' },
    { to: '/stats', label: tk('nav.stats'), icon: 'stats' },
  ],
  [
    { to: '/vault', label: tk('nav.vault'), icon: 'vault' },
    { to: '/rules', label: tk('nav.rules'), icon: 'rules' },
    { to: '/playground', label: tk('nav.playground'), icon: 'playground' },
  ],
  [{ to: '/integrations', label: tk('nav.integrations'), icon: 'integrations' }],
]

/** The sections of the page on screen, under its entry: a click scrolls to one. */
function SectionLinks() {
  const { t } = useTranslation()
  const sections = usePageSections()
  const active = useActiveSection(sections)
  if (sections.length < 2) return null
  return (
    <ul aria-label={t('nav.sections')} className="mt-px mb-1 ml-[19px] space-y-px border-l border-line pl-2 max-lg:hidden">
      {sections.map((section) => (
        <li key={section.id}>
          <a
            href={`#${section.id}`}
            aria-current={active === section.id ? 'location' : undefined}
            className={`block rounded-sm px-2 py-[3px] text-meta text-pretty ${
              active === section.id ? 'bg-wash-2 font-medium text-ink' : 'text-ink-3 hover:bg-wash hover:text-ink'
            }`}
            onClick={(event) => {
              event.preventDefault()
              document.getElementById(section.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              window.history.replaceState(window.history.state, '', `#${section.id}`)
            }}
          >
            {section.title}
          </a>
        </li>
      ))}
    </ul>
  )
}

function ThemeToggle() {
  const { t } = useTranslation()
  const [pref, setPref] = useTheme()
  return (
    <Segmented<ThemePref>
      label={t('theme.label')}
      value={pref}
      onChange={setPref}
      size="sm"
      options={[
        { value: 'system', label: t('theme.system'), icon: 'monitor', iconOnly: true },
        { value: 'light', label: t('theme.light'), icon: 'sun', iconOnly: true },
        { value: 'dark', label: t('theme.dark'), icon: 'moon', iconOnly: true },
      ]}
    />
  )
}

function LanguageMenu() {
  const { t, i18n } = useTranslation()
  const current = (i18n.resolvedLanguage ?? 'en') as Language
  const choose = (event: ChangeEvent<HTMLSelectElement>) => setLanguage(event.target.value as Language)
  return (
    <>
      <select className="field w-full max-lg:hidden" aria-label={t('language.label')} value={current} onChange={choose}>
        {LANGUAGES.map((lng) => (
          <option key={lng} value={lng} lang={lng}>
            {LANGUAGE_NAMES[lng]}
          </option>
        ))}
      </select>
      <select className="field w-full pr-4 pl-1 text-center lg:hidden" aria-label={t('language.label')} value={current} onChange={choose}>
        {LANGUAGES.map((lng) => (
          <option key={lng} value={lng} lang={lng} title={LANGUAGE_NAMES[lng]}>
            {lng.toUpperCase()}
          </option>
        ))}
      </select>
    </>
  )
}

/** On a Latet server: ends this browser's session, on every instance, and shows how to open the dashboard again. */
function SignOut() {
  const { t } = useTranslation()
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const signOut = async () => {
    setBusy(true)
    try {
      await api.logout()
      notifyUnauthorized()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
      setBusy(false)
    }
  }
  return (
    <button type="button" className="link text-meta" disabled={busy} onClick={() => void signOut()}>
      {t('shell.signOut')}
    </button>
  )
}

function DaemonStatus() {
  const { t } = useTranslation()
  const { me, unreachable, reload } = useMe()
  if (unreachable && !me) {
    return (
      <div className="space-y-1.5 text-meta text-ink-3 max-lg:hidden">
        <p className="flex items-center gap-1.5 text-ink-2">
          <span className="size-1.5 rounded-full bg-crit" aria-hidden="true" />
          {pageOnServer() ? t('shell.unreachableServer') : t('shell.unreachable')}
        </p>
        <button type="button" className="link text-meta" onClick={reload}>
          {t('common.retry')}
        </button>
      </div>
    )
  }
  if (!me) return null
  return (
    <div className="space-y-1 text-meta text-ink-3 max-lg:hidden">
      <p className="flex items-center gap-1.5 text-ink-2">
        <span className={`size-1.5 rounded-full ${me.recording ? 'bg-good' : 'bg-line-2'}`} aria-hidden="true" />
        {me.recording ? t('shell.recording') : t('shell.notRecording')}
      </p>
      <p>
        {t('shell.profile', { profile: PROFILE_LABEL[me.profile] ? t(PROFILE_LABEL[me.profile]) : me.profile })}
      </p>
      {me.server ? (
        <>
          <p className="truncate" title={me.server.url}>
            {t('shell.server', { host: serverHost(me.server.url) })}
          </p>
          {me.server.identity && (
            <p className="truncate" title={me.server.identity.name}>
              {t('shell.signedIn', { name: me.server.identity.name })}
            </p>
          )}
        </>
      ) : (
        <p className="mono">{t('shell.address', { port: me.port })}</p>
      )}
      <p>{t('shell.version', { version: me.version })}</p>
      {me.server && <SignOut />}
    </div>
  )
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <SectionsProvider>
      <Layout>{children}</Layout>
    </SectionsProvider>
  )
}

function Layout({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { pathname } = useLocation()
  const { waiting } = useApprovals()
  const { me } = useMe()
  const nav = me?.tutorial ? [TUTORIAL_NAV, ...NAV] : NAV
  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-ink px-3 py-2 text-surface focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        {t('app.skipToContent')}
      </a>
      <aside className="sticky top-0 flex h-screen w-[208px] flex-none flex-col border-r border-line bg-canvas max-lg:w-[60px]">
        <div className="flex min-h-14 items-center gap-2.5 px-4 py-[8px] max-lg:justify-center max-lg:px-0">
          <LogoMark size={22} animate />
          <div className="leading-tight max-lg:hidden">
            <p className="font-display text-title font-bold tracking-[-0.01em] text-ink">{t('app.name')}</p>
            <p className="text-micro text-ink-3">{t(key('app.tagline'))}</p>
          </div>
        </div>
        <nav aria-label={t('nav.label')} className="flex-1 overflow-y-auto px-2.5 pt-2">
          {nav.map((group, i) => (
            <ul key={i} className={`space-y-px ${i > 0 ? 'mt-2 border-t border-line pt-2' : ''}`}>
              {group.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    title={t(item.label)}
                    // The current page: a green wash and a heavier label. In the icon rail (below lg) the label is
                    // for screen readers only, so a ring says it as well: not by colour alone.
                    className={({ isActive }) =>
                      `group relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-ui max-lg:justify-center max-lg:px-0 ${
                        isActive
                          ? 'bg-nav font-medium text-nav-ink max-lg:shadow-[inset_0_0_0_1.5px_var(--nav-ink)]'
                          : 'text-ink-2 hover:bg-wash hover:text-ink'
                      }`
                    }
                  >
                    <Icon name={item.icon} />
                    <span className="max-lg:sr-only">{t(item.label)}</span>
                    {item.count === 'approvals' && waiting > 0 && (
                      <span
                        className="ml-auto inline-grid h-[18px] min-w-[18px] place-items-center rounded-full bg-real px-1 text-micro font-semibold text-surface tabular-nums max-lg:absolute max-lg:top-0.5 max-lg:right-1 max-lg:ml-0 max-lg:h-[14px] max-lg:min-w-[14px] max-lg:text-[9px]"
                        title={t('nav.waiting', { count: waiting })}
                      >
                        <span aria-hidden="true">{waiting}</span>
                        <span className="sr-only">{t('nav.waiting', { count: waiting })}</span>
                      </span>
                    )}
                  </NavLink>
                  {pathname.startsWith(item.to) && <SectionLinks />}
                </li>
              ))}
            </ul>
          ))}
        </nav>
        <div className="space-y-3 border-t border-line px-4 py-4 max-lg:px-2">
          {isMock() && (
            <p className="tag border-brand/60 bg-brand/10 text-ink max-lg:hidden" title={t('shell.mockHint')} data-mock-tag>
              {t('shell.mock')}
            </p>
          )}
          <DaemonStatus />
          <LanguageMenu />
          <div className="max-lg:flex max-lg:justify-center max-lg:[&_[role=radiogroup]]:flex-col">
            <ThemeToggle />
          </div>
        </div>
      </aside>
      <main id="main" className="min-w-0 flex-1 px-7 pt-6 pb-16 max-lg:px-5" tabIndex={-1}>
        <div className="mx-auto max-w-[1560px]">
          <TutorialBar />
          {children}
        </div>
      </main>
    </div>
  )
}
