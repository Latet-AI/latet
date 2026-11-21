import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { download } from '../../lib/transfer'
import { useMe } from '../../lib/me'
import { reportsApi } from '../api'
import type { ReportQuery } from '../types'
import { ReportSession } from './reportSession'

/** Keeps requests alive until settlement; disposal suppresses all later display/download effects. */
export function useReports(query: ReportQuery, readOnly = false) {
  const { t } = useTranslation()
  const toast = useToast()
  const { me, unreachable } = useMe()
  // /me has no led-group list or authority revision. Even an equal new response can accompany a
  // narrower hidden scope, so discard the previous session on every response (and failed refresh).
  // Construction is inert: requests start after commit, through the shared settlement gate.
  const session = useMemo(() => new ReportSession(reportsApi, (file) => {
    if (readOnly || !me || unreachable) return
    download(file.name, file.file, file.type)
    toast.show(t('ee.reports.exported', { name: file.name }))
  }), [t, toast, readOnly, me, unreachable])
  const key = JSON.stringify(query)
  // Revoke the old query at commit, before a pending export can settle ahead of the passive read.
  useLayoutEffect(() => { session.activate(); return () => session.dispose() }, [session, key])
  useEffect(() => { if (me && !unreachable) session.setQuery(JSON.parse(key) as ReportQuery) }, [session, key, me, unreachable])
  const state = useSyncExternalStore(session.subscribe, session.snapshot, session.snapshot)
  // A changed URL is already a different query before the effect runs. Never flash the previous choices.
  const current = state.key === key ? state : { key, busy: true, company: false }
  return { ...current, reload: () => session.setQuery(query), exportAs: (format: 'csv' | 'json') => readOnly ? Promise.resolve() : session.exportAs(format) }
}
