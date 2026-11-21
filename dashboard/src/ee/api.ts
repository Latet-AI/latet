import { asset, get, query, send } from '../api/client'
import type { ApprovalDecision, ChangeState } from '../api/types'
import { installBody } from './lib/license'
import type { PolicySaved, PolicyText } from './lib/policy'
import type {
  CompanyQueue,
  CompanyText,
  ConnectCode,
  Decided,
  ControlChange,
  GrantedRole,
  GroupHead,
  Group,
  KeysList,
  LicenseStanding,
  NewKey,
  NewPerson,
  NewScimToken,
  PeopleList,
  PersonChange,
  RoleChange,
  SignInTest,
  SignInView,
  SsoSettings,
  Report, ReportQuery, ReportFile, SiemView, SiemOutputSettings, SiemTest, AuditLogView, AuditQuery,
  GoneTerm, TermSourcesSaved, TermSyncSources, TermSyncState,
} from './types'

const at = (path: string) => `/ee${path}`
const part = encodeURIComponent

/** Audit reads do not hold the report slot; AuditSession coordinates exports with Reports. */
export const auditApi = {
  log: (q: AuditQuery, signal?: AbortSignal) => get<AuditLogView>(`/ee/audit${query({ ...q })}`, signal),
  exportLog: (q: AuditQuery, format: 'csv' | 'json') => get<ReportFile>(`/ee/audit/export${query({ ...q, format })}`),
}

/** The report/export slot is coordinated by ReportSession, including across page mounts. */
export const reportsApi = {
  report: (q: ReportQuery) => get<Report>(`/ee/reports${query({ ...q })}`),
  exportReport: (q: ReportQuery, format: 'csv' | 'json') => get<ReportFile>(`/ee/reports/export${query({ ...q, format })}`),
  siem: (signal?: AbortSignal) => get<SiemView>('/ee/siem', signal),
  saveSiem: (outputs: SiemOutputSettings[], secrets: Record<string, string | null>, etag: string) =>
    send<SiemView>('PUT', '/ee/siem', { outputs, secrets, etag }),
  siemSecret: (name: string, secret: string | null) => send<{ ok: boolean }>('PUT', '/ee/siem/secret', { name, secret }),
  testSiem: (name: string) => send<SiemTest>('POST', '/ee/siem/test', { name }),
}

/** Whether the person saw that the server's policy stops being in force with a change of the license, and goes on. */
export interface PolicyStops {
  policyStops?: boolean
}

/** The Enterprise Edition's routes, under /_latet/api/v1/ee/. */
export const eeApi = {
  license: (signal?: AbortSignal) => get<LicenseStanding>('/ee/license', signal),
  /**
   * Installs a license file (its text) on the server, for `license.manage` while the license can manage. 400
   * `{error, code}` for a file that is no license; 409 `{error, policy: "stops"}` (`policyStops`, ee/lib/license.ts)
   * when the server's policy stops being in force with it, nothing changed, unless `policyStops` says it was seen.
   */
  installLicense: (text: string, { policyStops }: PolicyStops = {}) =>
    send<LicenseStanding>('PUT', at('/license'), installBody(text, policyStops)),
  /** Removes the license (`removed`: whether there was one), under the same conditions and the same 409. */
  removeLicense: ({ policyStops }: PolicyStops = {}) =>
    send<LicenseStanding & { removed: boolean }>('DELETE', at('/license'), policyStops ? { policy_stops: true } : undefined),
}

/** The People and groups and Sign-in pages' routes (E3), on a server. */
export const peopleApi = {
  people: (signal?: AbortSignal) => get<PeopleList>(at('/people'), signal),
  addPerson: (name: string, email: string | null) => send<NewPerson>('POST', at('/people'), { name, email }),
  /** A code for a person who never connected a computer (409 for anyone else: `latet server people code` makes it). */
  code: (person: string) => send<NewPerson>('POST', at(`/people/${part(person)}/code`)),
  disable: (person: string) => send<PersonChange>('POST', at(`/people/${part(person)}/disable`)),
  enable: (person: string) => send<PersonChange>('POST', at(`/people/${part(person)}/enable`)),
  groups: (signal?: AbortSignal) => get<{ groups: Group[] }>(at('/groups'), signal),
  addGroup: (name: string) => send<{ group: GroupHead }>('POST', at('/groups'), { name }),
  /** A group made here only (409 for one from the provider). */
  renameGroup: (group: string, name: string) => send<{ group: GroupHead }>('PUT', at(`/groups/${part(group)}`), { name }),
  removeGroup: (group: string) => send<{ ok: true; scim_tokens?: string[] }>('DELETE', at(`/groups/${part(group)}`)),
  join: (group: string, person: string) => send<PersonChange>('POST', at(`/groups/${part(group)}/members`), { person }),
  leave: (group: string, person: string) => send<PersonChange>('DELETE', at(`/groups/${part(group)}/members/${part(person)}`)),
  grant: (subject: string, role: GrantedRole) => send<RoleChange>('POST', at('/roles'), { subject, role }),
  revoke: (subject: string, role: GrantedRole) => send<RoleChange>('DELETE', at(`/roles/${part(subject)}/${role}`)),
  /** Always the person's own keys, in a key's space too (`spaceUrl`). */
  keys: (signal?: AbortSignal) => get<KeysList>(at('/keys'), signal),
  makeKey: (name: string) => send<NewKey>('POST', at('/keys'), { name }),
  /** The key's token rotated (409 when it has several: `latet server keys rotate --token`); `address` is null for a
   * token that serves no agents (one added with `latet server keys add-token`). */
  rotateKey: (key: string) => send<{ secret: string; address: string | null }>('POST', at(`/keys/${part(key)}/rotate`)),
  disableKey: (key: string) => send<{ ok: true }>('POST', at(`/keys/${part(key)}/disable`)),
  enableKey: (key: string) => send<{ ok: true }>('POST', at(`/keys/${part(key)}/enable`)),
  control: (key: string, subject: string) => send<ControlChange>('POST', at(`/keys/${part(key)}/controllers`), { subject }),
  release: (key: string, subject: string) => send<ControlChange>('DELETE', at(`/keys/${part(key)}/controllers/${part(subject)}`)),
  signIn: (signal?: AbortSignal) => get<SignInView>(at('/sign-in'), signal),
  /**
   * The sso section (null: single sign-on off); `company_changes` is `latet server`'s, and a body that names it is
   * refused. `etag`: the settings' as `signIn` read them (`SignInView.etag`, null when single sign-on was off); 409
   * `sign-in-changed` when someone else changed them since, nothing changed.
   */
  saveSignIn: (sso: SsoSettings | null, etag: string | null) => send<SignInView>('PUT', at('/sign-in'), { sso, etag }),
  saveSecret: (secret: string) => send<{ ok: true }>('PUT', at('/sign-in/secret'), { secret }),
  removeSecret: () => send<{ ok: true }>('DELETE', at('/sign-in/secret')),
  testSignIn: (sso: SsoSettings) => send<SignInTest>('POST', at('/sign-in/test'), { sso }),
  /** A token that ends at the server's default day (the dashboard makes none that never does). */
  makeScimToken: () => send<NewScimToken>('POST', at('/sign-in/scim-tokens')),
  removeScimToken: (id: string) => send<{ ok: true }>('DELETE', at(`/sign-in/scim-tokens/${part(id)}`)),
  connectCode: () => send<ConnectCode>('POST', at('/sign-in/code')),
  /** The person saw what their dashboard told them up to `until` (`/me.edition.connected.until`): only from a session the company sign-in opened. */
  told: (until: number) => send<{ ok: true; told: boolean }>('POST', at('/notices/told'), { until }),
}

/**
 * Central approvals (E5): what waits for you to decide. 403 `not-yours` for someone who decides nothing. Each refusal,
 * and each decision not taken, carries a `code` the page words (ee/lib/company-approvals.ts).
 */
export const approvalsApi = {
  queue: (signal?: AbortSignal) => get<CompanyQueue>(at('/approvals'), signal),
  /** How many wait for you to decide (the menu's count): one small read, while the page is closed. */
  count: (signal?: AbortSignal) => get<{ waiting: number }>(at('/approvals/count'), signal),
  /** What was decided lately, newest first. */
  history: (signal?: AbortSignal) => get<{ items: Decided[] }>(at('/approvals/history'), signal),
  /** A text, as its author sees it: read when you open it. 409 `gone` when it no longer waits; 403 `company-sign-in`
   *  from a session the company's sign-in did not open [D6]; 403 `trial-ended` once a trial ended. */
  batch: (id: string, signal?: AbortSignal) => get<CompanyText>(at(`/approvals/batches/${part(id)}`), signal),
  /** Approve a change (it applies to that person's or that API key's settings) or reject it. */
  decideChange: (id: string, action: 'approve' | 'reject') =>
    send<{ ok: true; state: ChangeState }>('POST', at(`/approvals/changes/${part(id)}`), { action }),
  /**
   * Approve a text (with the values masked here) or stop it. An approval names the `version` of the text read (400
   * without it); 409 `changed` when it was masked again since (nothing decided: read it again), `gone` when it no
   * longer waits; 422 for a mask refused.
   */
  decideBatch: (id: string, decision: ApprovalDecision & { version?: string }) =>
    send<{ ok: true; state: string }>('POST', at(`/approvals/batches/${part(id)}`), decision),
  /** An image of a text waiting: as it came, or as it would leave now, with `mark` (`sentMark` of its text as masked)
   * so that it is asked again once it is painted again (the final security review's approvals L4). */
  imageUrl: (id: string, item: number, view: 'original' | 'sent' = 'original', mark = '') =>
    asset(
      at(
        `/approvals/batches/${part(id)}/images/${item}` +
          (view === 'sent' ? `?view=sent${mark ? `&at=${encodeURIComponent(mark)}` : ''}` : ''),
      ),
    ),
}

/**
 * Term sync (E6), for whoever terms.manage allows (the security role). The gone list and the sources are read from a
 * session the company's sign-in opened (403 `company-sign-in` otherwise, while the server asks for it), and so is
 * every change made. A change of the sources, and a secret's, names the etag of the sources it was made from (400
 * without it; 409 `sources-changed` when they changed since); a source at the server's own computer, and its secret,
 * are `latet server terms`' alone (403 `by-command`).
 */
export const termSyncApi = {
  state: (signal?: AbortSignal) => get<TermSyncState>(at('/term-sync'), signal),
  gone: (signal?: AbortSignal) => get<{ items: GoneTerm[]; total: number }>(at('/term-sync/gone'), signal),
  /** 202 `{asked}`: the sources whose sync was asked for, run at once by the instance that holds the lease. */
  run: (source?: string) => send<{ asked: string[] }>('POST', at('/term-sync/run'), source ? { source } : {}),
  remove: (ids: string[]) => send<{ removed: number; by_type: Record<string, number> }>('POST', at('/term-sync/remove'), { ids }),
  // [D4]: the sources and their secrets, the security role's
  sources: (signal?: AbortSignal) => get<TermSyncSources>(at('/term-sync/sources'), signal),
  saveSources: (text: string, etag: string) => send<TermSourcesSaved>('PUT', at('/term-sync/sources'), { text, etag }),
  /** Kept sealed for the source as it is set up now, and a sync of it asked for in the same change. */
  saveSecret: (source: string, value: string, etag: string) =>
    send<{ saved: string }>('PUT', at(`/term-sync/secrets/${part(source)}`), { value, etag }),
  removeSecret: (source: string, etag: string) => send<{ removed: string }>('DELETE', at(`/term-sync/secrets/${part(source)}`), { etag }),
}

/** The Policy page's editor (E3, a server, for policy.edit): the stored text, and the policy saved or removed. */
export const policyApi = {
  text: (signal?: AbortSignal) => get<PolicyText>(at('/policy/text'), signal),
  /**
   * `etag`: the stored policy's as `text` read it (`PolicyText.etag`, null when none was stored); 409 `policy-changed`
   * when someone else changed it since. Never with `accept_notices`: a text with keys left out whole is refused with
   * its reason, and corrected (R27).
   */
  save: (text: string, etag: string | null) => send<PolicySaved>('PUT', at('/policy'), { policy: text, etag }),
  /**
   * `etag`: the stored policy's as `text` read it, as `save` sends it (null when none was stored); the server answers
   * 400 without one and 409 `policy-changed` when someone else changed the policy since.
   */
  remove: (etag: string | null) => send<{ removed: boolean }>('DELETE', at('/policy'), { etag }),
}
