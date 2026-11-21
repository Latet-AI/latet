import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../../api/client'
import { useToast } from '../../components/Toast'
import { hasPage } from '../../lib/edition'
import { useMe } from '../../lib/me'
import { download } from '../../lib/transfer'
import { auditApi } from '../api'
import type { AuditQuery } from '../types'
import { AuditSession } from './auditSession'

/** Every new /me response invalidates even an equal payload: hidden authority may have changed. */
export function useAudit(query: AuditQuery, readOnly: boolean) {
  const { me, unreachable } = useMe(), { t } = useTranslation(), toast = useToast()
  const allowed = !!me?.server && me.server.identity?.kind === 'person' && !unreachable && hasPage(me, 'audit')
    && !!me.edition?.actions.includes('audit.read') && me.edition.features.includes('reports')
  const canExport = allowed && !readOnly && me?.edition?.license?.state !== 'trial-ended'
  const session = useMemo(() => new AuditSession(auditApi, file => {
    if (!canExport || !me || unreachable || readOnly) return
    download(file.name, file.file, file.type)
    toast.show(t('ee.audit.exported', { name: file.name }))
  }), [me, unreachable, readOnly, canExport, t, toast])
  const key = JSON.stringify(query)
  // Revoke the old query at commit, before a pending export can settle ahead of the passive read.
  useLayoutEffect(() => { session.activate(); return () => session.dispose() }, [session, key])
  useEffect(() => { if (allowed) session.setQuery(JSON.parse(key) as AuditQuery) }, [session, key, allowed])
  const state = useSyncExternalStore(session.subscribe, session.snapshot, session.snapshot)
  const current = !allowed ? { key, busy: false, company: false, error: new ApiError(403, 'audit unavailable') }
    : state.key === key ? state : { key, busy: true, company: false }
  return { ...current, canExport, addresses: allowed && !!me?.edition?.roles.includes('admin'),
    reload: () => { if (allowed) session.setQuery(query) },
    exportAs: (format: 'csv' | 'json') => canExport && state.key === key ? session.exportAs(format) : Promise.resolve() }
}
