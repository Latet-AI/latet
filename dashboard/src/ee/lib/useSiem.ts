import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { tk } from '../../i18n'
import { hasPage } from '../../lib/edition'
import { useMe } from '../../lib/me'
import { reportsApi } from '../api'
import { SiemSession } from './siemSession'

const NOTICE_TEXT = { saved: tk('ee.reports.siem.saved'), testOk: tk('ee.reports.siem.testOk'), testFail: tk('ee.reports.siem.testFail') }

/** Every newly received /me invalidates the old view and intent, including equal payloads with hidden scope changes. */
export function useSiem(readOnly: boolean) {
  const { me, unreachable } = useMe(), { t } = useTranslation(), toast = useToast()
  const allowed = !!me?.server && me.server.identity?.kind === 'person' && hasPage(me, 'siem') && !unreachable
    && !!me.edition?.actions.includes('reports.all')
  const editing = allowed && !readOnly && me?.edition?.license?.state !== 'trial-ended' && !!me?.edition?.actions.includes('siem.edit')
  const session = useMemo(() => new SiemSession(reportsApi, editing, kind => {
    if (!me || unreachable || readOnly) return
    toast.show(t(NOTICE_TEXT[kind]), kind === 'testFail' ? 'fail' : undefined)
  }), [editing, me, unreachable, readOnly, t, toast])
  useLayoutEffect(() => { session.activate(); return () => session.dispose() }, [session])
  useEffect(() => { if (allowed) session.reload() }, [allowed, session])
  const state = useSyncExternalStore(session.subscribe, session.snapshot, session.snapshot)
  return { ...state, error: allowed ? state.error : 'read' as const, editable: session.editable(), reload: session.reload,
    edit: session.edit, change: session.change, cancel: session.cancel, remove: session.remove, save: session.save, confirmRemove: session.confirmRemove, test: session.test }
}
