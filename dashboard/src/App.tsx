import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { createBrowserRouter, Navigate, Outlet, RouterProvider, useRouteError } from 'react-router'
import { api, onUnauthorized } from './api/client'
import { InspectorProvider } from './components/masked/Inspector'
import { Shell } from './components/Shell'
import { ToastProvider } from './components/Toast'
import { TutorialElsewhere } from './components/tutorial/EndTutorial'
import { ErrorState, Loading } from './components/ui'
import { ApprovalsProvider } from './lib/approvals-context'
import { MeContext, useMe } from './lib/me'
import { loginRefusal } from './lib/signin'
import { homePath, tutorialElsewhere } from './lib/tutorial'
import { useAsync } from './lib/useAsync'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { AuthPage } from './pages/AuthPage'
import { ExchangePage } from './pages/ExchangePage'
import { IntegrationsPage } from './pages/IntegrationsPage'
import { LivePage } from './pages/LivePage'
import { NotFoundPage } from './pages/NotFoundPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { RulesPage } from './pages/RulesPage'
import { SessionsPage } from './pages/SessionsPage'
import { StatsPage } from './pages/StatsPage'
import { TutorialMenuPage } from './pages/TutorialMenuPage'
import { TutorialPage } from './pages/TutorialPage'
import { VaultPage } from './pages/VaultPage'

function Root() {
  const [unauthorized, setUnauthorized] = useState(false)
  const me = useAsync('me', (signal) => api.me(signal))
  const config = useAsync('config', (signal) => api.config(signal))
  const refusal = useMemo(() => loginRefusal(window.location.search), [])

  useEffect(() => onUnauthorized(() => setUnauthorized(true)), [])

  const customTypes = useMemo(() => new Set((config.data?.terms ?? []).map((term) => term.type.toUpperCase())), [config.data])
  const meState = useMemo(
    () => ({ me: me.data, unreachable: Boolean(me.error), reload: me.reload, customTypes, reloadConfig: config.reload }),
    [me.data, me.error, me.reload, customTypes, config.reload],
  )

  if (unauthorized) return <AuthPage refusal={refusal} />
  return (
    <MeContext.Provider value={meState}>
      {/* One approvals stream for the app: the menu's count, the page title, Live and Approvals. */}
      <ApprovalsProvider live={config.data?.live_review}>
        <Shell>
          <InspectorProvider>
            <Outlet />
          </InspectorProvider>
        </Shell>
      </ApprovalsProvider>
    </MeContext.Provider>
  )
}

/** Where the dashboard opens: the tutorial in its sandbox (`/me` says so), Live on a real daemon. */
function Home() {
  const { me, unreachable } = useMe()
  if (!me && !unreachable) return <Loading />
  return <Navigate to={homePath(me)} replace />
}

/** The tutorial's pages: its routes exist in its sandbox only. On your own daemon, how to start it. */
function SandboxOnly({ children }: { children: ReactNode }) {
  const { me, unreachable } = useMe()
  if (!me && !unreachable) return <Loading />
  return tutorialElsewhere(me) ? <TutorialElsewhere /> : children
}

function RouteError() {
  const error = useRouteError()
  return (
    <Shell>
      <ErrorState error={error} onRetry={() => window.location.reload()} />
    </Shell>
  )
}

const basename = import.meta.env.BASE_URL.replace(/\/$/, '')

const router = createBrowserRouter(
  [
    {
      path: '/',
      element: <Root />,
      errorElement: <RouteError />,
      children: [
        { index: true, element: <Home /> },
        { path: 'login', element: <Home /> },
        { path: 'tutorial', element: <SandboxOnly><TutorialMenuPage /></SandboxOnly> },
        { path: 'tutorial/:chapter', element: <SandboxOnly><TutorialPage /></SandboxOnly> },
        { path: 'live', element: <LivePage /> },
        { path: 'approvals', element: <ApprovalsPage /> },
        { path: 'sessions', element: <SessionsPage /> },
        { path: 'sessions/:key', element: <SessionsPage /> },
        { path: 'exchanges/:id', element: <ExchangePage /> },
        { path: 'vault', element: <VaultPage /> },
        { path: 'rules', element: <RulesPage /> },
        { path: 'playground', element: <PlaygroundPage /> },
        { path: 'integrations', element: <IntegrationsPage /> },
        { path: 'stats', element: <StatsPage /> },
        { path: '*', element: <NotFoundPage /> },
      ],
    },
  ],
  { basename },
)

export function App() {
  return (
    <ToastProvider>
      <RouterProvider router={router} />
    </ToastProvider>
  )
}
