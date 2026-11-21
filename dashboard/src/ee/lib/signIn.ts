import { ApiError } from '../../api/client'
import { tk } from '../../i18n'
import type { Group, SignInView, SsoSettings } from '../types'

/** A row of the groups' mapping: a group of the ID token (its name or ID) and the Latet group it gives. */
export interface GroupRow {
  claim: string
  group: string
}

/** The Sign-in page's form: the sso section as text fields, its groups as rows. */
export interface SignInDraft {
  issuer: string
  clientId: string
  scopes: string
  userNameClaim: string
  groupsClaim: string
  accounts: SsoSettings['accounts']
  accessGroup: string
  /** Google's Workspace domains, separated by spaces, commas or new lines. */
  allowedDomains: string
  rows: GroupRow[]
}

export type DraftProblem =
  | 'issuer'
  | 'clientId'
  | 'openid'
  | 'claimEmpty'
  | 'claimTwice'
  | 'groupName'
  | 'accessGroup'
  | 'accessGroupOpen'
  | 'domains'
  | 'domainsNotGoogle'
  | 'domain'
export type SignInNotice = 'expired' | 'failed' | 'notAllowed' | 'notProvisioned' | 'disabled' | 'unavailable' | 'trialEnded'

const NOTICES: Record<string, SignInNotice> = {
  expired: 'expired',
  failed: 'failed',
  'not-allowed': 'notAllowed',
  'not-provisioned': 'notProvisioned',
  disabled: 'disabled',
  unavailable: 'unavailable',
  'trial-ended': 'trialEnded',
}

/** What each reason says, in the reader's language: on the sign-in page, and on the dashboard of a person already signed in. */
export const NOTICE_TEXT: Record<SignInNotice, string> = {
  expired: tk('auth.company.expired'),
  failed: tk('auth.company.failed'),
  notAllowed: tk('auth.company.notAllowed'),
  notProvisioned: tk('auth.company.notProvisioned'),
  disabled: tk('auth.company.disabled'),
  unavailable: tk('auth.company.unavailable'),
  trialEnded: tk('auth.company.trialEnded'),
}

/** Where **Link my company account** begins: a page of the server's own, which sends the browser to the provider. */
export const LINK_START = '/_latet/sso/link'

/** Why the last sign-in did not happen: the server sends the browser back with `?sign-in=<reason>`. */
export function signInNotice(search: string): SignInNotice | null {
  const reason = new URLSearchParams(search).get('sign-in')
  return reason ? (NOTICES[reason] ?? 'failed') : null
}

/** https, or http on this computer (Keycloak in Docker, for a test), as the server takes it. */
export function secureIssuer(issuer: string): boolean {
  try {
    const url = new URL(issuer.trim())
    return url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))
  } catch {
    return false
  }
}

/** A group of the ID token as the server compares them: case aside, without Keycloak's leading `/`. */
export function claimKey(claim: string): string {
  return claim.trim().replace(/^\/+/, '').toLowerCase()
}

// A domain name as the settings take one (a name with a dot and a top level of letters), once lower-cased.
const DOMAIN = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/

/** The domains a text names, lower-cased and each once: they are separated by spaces, commas or new lines. */
export function domainList(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).filter(Boolean).map((domain) => domain.toLowerCase()))]
}

/** Whether the issuer is Google's: its ID tokens say the person's Workspace domain, which is how it lets people in. */
export function googleIssuer(issuer: string): boolean {
  return issuer.trim().replace(/\/+$/, '') === 'https://accounts.google.com'
}

export function toDraft(sso: SsoSettings | null): SignInDraft {
  if (!sso) {
    return {
      issuer: '',
      clientId: '',
      scopes: 'openid profile email',
      userNameClaim: 'preferred_username',
      groupsClaim: 'groups',
      accounts: 'scim',
      accessGroup: '',
      allowedDomains: '',
      rows: [],
    }
  }
  return {
    issuer: sso.issuer,
    clientId: sso.client_id,
    scopes: sso.scopes.join(' '),
    userNameClaim: sso.user_name_claim,
    groupsClaim: sso.groups_claim,
    accounts: sso.accounts,
    accessGroup: sso.access_group ?? '',
    allowedDomains: sso.allowed_domains.join(' '),
    rows: Object.entries(sso.groups).map(([claim, group]) => ({ claim, group })),
  }
}

/** The draft as the sso section; the client secret's name is kept from `base`. */
export function fromDraft(draft: SignInDraft, base: SsoSettings | null): SsoSettings {
  const groups: SsoSettings['groups'] = {}
  for (const row of draft.rows) groups[row.claim.trim()] = row.group.trim()
  return {
    issuer: draft.issuer.trim(),
    client_id: draft.clientId.trim(),
    client_secret: base ? base.client_secret : 'sso-client-secret',
    scopes: draft.scopes.split(/\s+/).filter(Boolean),
    user_name_claim: draft.userNameClaim.trim(),
    groups_claim: draft.groupsClaim.trim(),
    accounts: draft.accounts,
    access_group: draft.accessGroup.trim() || null,
    allowed_domains: domainList(draft.allowedDomains),
    groups,
  }
}

/** What the server would refuse, said before saving. Groups compare as the server compares them. */
export function draftProblems(draft: SignInDraft): DraftProblem[] {
  const problems = new Set<DraftProblem>()
  if (!secureIssuer(draft.issuer)) problems.add('issuer')
  if (!draft.clientId.trim()) problems.add('clientId')
  if (!draft.scopes.split(/\s+/).includes('openid')) problems.add('openid')
  const seen = new Set<string>()
  for (const row of draft.rows) {
    const key = claimKey(row.claim)
    if (!key) problems.add('claimEmpty')
    else if (seen.has(key)) problems.add('claimTwice')
    seen.add(key)
    const name = row.group.trim()
    if (!name || name.length > 80) problems.add('groupName')
  }
  const group = draft.accessGroup.trim()
  if (draft.accounts === 'group' && !group) problems.add('accessGroup')
  if (draft.accounts === 'open' && group) problems.add('accessGroupOpen')
  const domains = domainList(draft.allowedDomains)
  if (domains.some((domain) => !DOMAIN.test(domain))) problems.add('domain')
  if (googleIssuer(draft.issuer)) {
    if (domains.length === 0) problems.add('domains')
  } else if (domains.length > 0) {
    problems.add('domainsNotGoogle')
  }
  return [...problems]
}

// A group of the ID token written as its provider's immutable ID: a UUID (Microsoft Entra, Keycloak), an Okta group ID,
// a number. Any other is a name, which whoever may make groups at the provider can write too.
const GROUP_ID = [/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i, /^00g[0-9A-Za-z]{17}$/, /^\d{5,}$/]

/** Whether a group of the ID token is written by a name (as the server compares them: a leading `/` aside), not an ID. */
export function byName(claim: string): boolean {
  const key = claim.trim().replace(/^\/+/, '')
  return key !== '' && !GROUP_ID.some((pattern) => pattern.test(key))
}

/**
 * The rows that give the Admin role by a name: a group of the ID token written by its name, mapped to a Latet group that
 * holds Admin. On a provider where people make groups, anyone who makes one of that name becomes an administrator.
 */
export function adminByName(rows: GroupRow[], groups: Group[]): GroupRow[] {
  const admins = new Set(groups.filter((group) => group.roles.includes('admin')).map((group) => group.name.trim().toLowerCase()))
  return rows.filter((row) => byName(row.claim) && admins.has(row.group.trim().toLowerCase()))
}

interface EditionAccess {
  features: string[]
  actions?: string[]
}

/** Who sees the sign-in settings: everyone while the license has no roles (read-only), then those who edit them. */
export function seesSettings(edition: EditionAccess): boolean {
  return !edition.features.includes('roles') || editsSettings(edition)
}

export function editsSettings(edition: EditionAccess): boolean {
  return (edition.actions ?? []).includes('sign-in.edit')
}

/**
 * Whether a save was refused because the sign-in settings changed since the page read them (409 `sign-in-changed`):
 * someone else saved them, from the page or with `latet server`, and nothing was changed. The page reads them again.
 */
export function signInChanged(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false
  const body = error.body as { code?: unknown } | null | undefined
  return body?.code === 'sign-in-changed'
}

/**
 * What **Make a code** says when the server refused it for one of its limits, with the server's own number: the person
 * has the most computers connected to their account (409 `computers-most`), or made the most codes of the last hour
 * (429 `codes-too-many`). Null for any other refusal, which says the server's reason as every change does.
 */
export function connectRefused(t: (key: string, options?: Record<string, unknown>) => string, error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const body = error.body as { code?: unknown; most?: unknown } | null | undefined
  const most = typeof body?.most === 'number' ? body.most : undefined
  if (most === undefined) return null
  if (error.status === 409 && body?.code === 'computers-most') return t('ee.signIn.connect.most', { most })
  if (error.status === 429 && body?.code === 'codes-too-many') return t('ee.signIn.connect.tooMany', { most })
  return null
}

/**
 * A save of the settings made from the view the form was made from: it names that view's etag, which is null while
 * single sign-on was off ("none was set up"), so that a change made meanwhile refuses it (`signInChanged`).
 */
export function saveFrom(
  view: Pick<SignInView, 'etag'>,
  sso: SsoSettings | null,
  save: (sso: SsoSettings | null, etag: string | null) => Promise<unknown>,
): Promise<unknown> {
  return save(sso, view.etag)
}
