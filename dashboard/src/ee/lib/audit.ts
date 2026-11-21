import type { AuditActor, AuditEntryView, AuditLogView, AuditQuery } from '../types'
import { rangeProblem } from './reports'

type Translate = (key: string, options?: Record<string, unknown>) => string
export function actorLabel(t: Translate, actor: AuditActor): string {
  if (actor.kind === 'token') return t('ee.audit.token', { id: actor.id.replace(/^admin:/, '') })
  if (actor.kind === 'scim') return t('ee.audit.scim', { id: actor.id.replace(/^scim:/, '') })
  if (actor.kind === 'server') return t('ee.audit.server')
  if (actor.kind === 'unknown') return t('ee.audit.unknown')
  return actor.kind === 'person' || actor.kind === 'key' ? actor.name ?? actor.id : actor.id
}
export function detailsText(details: Readonly<Record<string, unknown>>): string {
  return Object.entries(details).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ')
}
export function newestFirst(entries: readonly AuditEntryView[]): AuditEntryView[] { return [...entries].sort((a, b) => b.id - a.id) }
export function auditQuery(range: { from: string; to: string }, action: string | null, actor: string | null): AuditQuery {
  return { ...range, ...(action ? { action } : {}), ...(actor ? { actor } : {}) }
}
/** Python's filter limit counts Unicode code points, not UTF-16 code units. */
export function filterProblem(query: AuditQuery): boolean { return [query.action, query.actor].some(value => value !== undefined && [...value].length > 200) }
export function auditTime(value: string | null, format: (seconds: number) => string, t: Translate): string {
  const time = value === null ? NaN : Date.parse(value)
  return Number.isFinite(time) ? format(time / 1000) : t('ee.audit.timeUnavailable')
}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const nullableText = (value: unknown) => value === null || typeof value === 'string'
const actor = (value: unknown): value is AuditActor => object(value) && typeof value.id === 'string' && nullableText(value.name) && nullableText(value.kind)
/** Validate every field consumed by the page before keeping a successful read or enabling an export. */
export function validAuditLog(value: unknown): value is AuditLogView {
  if (!object(value) || !object(value.period) || typeof value.period.from !== 'string' || typeof value.period.to !== 'string'
    || rangeProblem(value.period.from, value.period.to) || typeof value.addresses !== 'boolean' || typeof value.truncated !== 'boolean'
    || !Array.isArray(value.entries) || value.entries.length > 5000 || !Array.isArray(value.actions) || !value.actions.every(a => typeof a === 'string')
    || !Array.isArray(value.actors) || !value.actors.every(actor)) return false
  const ids = new Set<number>()
  return value.entries.every(row => {
    if (!object(row) || !Number.isSafeInteger(row.id) || typeof row.id !== 'number' || row.id < 1 || ids.has(row.id)
      || !nullableText(row.ts) || typeof row.action !== 'string' || !actor(row.actor) || !object(row.details) || !nullableText(row.address)
      || !(row.target === null || (object(row.target) && typeof row.target.id === 'string' && nullableText(row.target.name)))) return false
    ids.add(row.id); return true
  })
}
