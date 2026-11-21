import type { Batch, ChangeState, LicenseNotice, LicenseState, SettingsChange } from '../api/types'

/** A license as its file says it. */
export interface LicenseDetails {
  id: string
  licensee: string
  users: number
  features: string[]
  issued: string
  expires: string
  trial: boolean
}

/** Why a file is no license: the codes a server's standing, or its administration's refusal, gives. */
export type LicenseErrorCode =
  | 'unreadable'
  | 'format'
  | 'no-keys'
  | 'key'
  | 'signature'
  | 'version'
  | 'content'
  | 'ended'

/** `GET /ee/license`: the license in use, weighed now. */
export interface LicenseStanding {
  state: LicenseState
  license: LicenseDetails | null
  /** The enabled people and API keys a server counts; null for a person on a server (the administrators' count). */
  users_in_use: number | null
  days_left: number | null
  /** Since when more people and API keys are in use than bought (epoch seconds); null where users_in_use is. */
  over_since: number | null
  pauses_on: string | null
  /** Why the file cannot be used: in English, and as a code the page says in the reader's language. */
  reason: string | null
  reason_code: LicenseErrorCode | null
  notices: LicenseNotice[]
  source: 'server' | null
}

// E3: people, groups, roles, API keys and their control; single sign-on and SCIM (enterprise-interfaces.md, E3). The
// shapes are those the server answers (ee/people_api.py, ee/keys.py, ee/sign_in_api.py).
export type Role = 'user' | 'group-lead' | 'security' | 'auditor' | 'admin'
/** The roles given by hand, to a person or a group: everyone has the user role. */
export type GrantedRole = Exclude<Role, 'user'>
/** Made by hand, pushed by SCIM, or given by the groups of a sign-in. */
export type GroupSource = 'local' | 'scim' | 'oidc'
/** Who disabled an account: an administrator's disable is never lifted by SCIM. */
export type DisabledBy = 'local' | 'scim'

export interface GroupRef {
  id: string
  name: string
}

export interface Person {
  id: string
  name: string
  email: string | null
  /** What they sign in as at the company's provider, when known. */
  user_name: string | null
  disabled: boolean
  /** How the account was made: by hand, by SCIM, or at their first sign-in. */
  source: 'local' | 'scim' | 'oidc'
  last_seen: number | null
  /** Whether the company's sign-in is linked to their account (never what links it). */
  linked: boolean
  /** Whether their first sign-in finds their account by its user name: an administrator or SCIM linked it, nobody signed in to it since. */
  claimable: boolean
  /** Whether SCIM manages them: it sees and changes only the people it manages. */
  scim_managed: boolean
  /** Who disabled them, while they are disabled. */
  disabled_by: DisabledBy | null
  groups: (GroupRef & { source: GroupSource })[]
  /** The roles given to them. */
  roles: GrantedRole[]
  /** The roles given to their groups. */
  through: { role: GrantedRole; group: GroupRef }[]
}

export interface PeopleList {
  people: Person[]
  /** Enabled people and API keys: what the license counts. */
  enabled: number
}

/** What a group is without its members: the answer to making or renaming one. */
export interface GroupHead extends GroupRef {
  source: GroupSource
  external_id: string | null
}

export interface Group extends GroupHead {
  members: { id: string; name: string; source: GroupSource }[]
  roles: GrantedRole[]
}

/** A person or a group, as a role or the control of a key is given to it. */
export interface Subject {
  kind: 'person' | 'group'
  id: string
  name: string
}

export type Controller = Subject

export interface ApiKey {
  id: string
  name: string
  disabled: boolean
  created: number
  last_seen: number | null
  /** Listed to administrators only (those who manage keys). */
  controllers: Controller[]
}

export interface KeysList {
  keys: ApiKey[]
  /** Whether the person makes keys and gives their control (keys.manage). */
  manage: boolean
}

export interface NewKey {
  key: ApiKey
  /** Shown once: the server keeps its hash. */
  secret: string
  /** The address a program uses in place of the provider's. */
  address: string
}

export interface ConnectCode {
  code: string
  /** `latet connect` with the server's address (and the certificate's fingerprint when it is self-signed): the code is shown apart. */
  command: string
}

export interface NewPerson extends ConnectCode {
  person: GroupRef
}

/**
 * The answer to a change of a person or of what they hold: whom the name was taken for, and, when the change took
 * the administrator role from them or disabled them, the SCIM tokens they made that still serve (not ended by a demotion).
 */
export interface PersonChange {
  ok: true
  person: GroupRef
  scim_tokens?: string[]
}

/** The answer to a change of a role: the roles the subject has now, whom the name was taken for, and the SCIM tokens as above. */
export interface RoleChange {
  roles: GrantedRole[]
  subject: Subject
  scim_tokens?: string[]
}

/** The answer to a change of who controls a key: who does now, and whom the name was taken for. */
export interface ControlChange {
  controllers: Controller[]
  subject: Subject
}

/** The sso section of the Enterprise settings, as the Sign-in page sends and reads it (`company_changes` is `latet server`'s). */
export interface SsoSettings {
  issuer: string
  client_id: string
  /** The name of the saved client secret, never the secret. */
  client_secret: string | null
  scopes: string[]
  user_name_claim: string
  groups_claim: string
  /** Who gets an account at a first sign-in: only people SCIM provisioned or an administrator linked, the members of `access_group`, or anyone (written out). */
  accounts: 'scim' | 'group' | 'open'
  access_group: string | null
  /** Google's Workspace domains, checked against its ID tokens' `hd`. */
  allowed_domains: string[]
  /** A group of the ID token (name or ID) → the Latet group it gives. */
  groups: Record<string, string>
}

/** Who made a SCIM token: the administration (`latet server`), or a person and whether they still administer. */
export type TokenMaker =
  | { kind: 'administration'; id: string }
  | { kind: 'person'; id: string; name: string | null; admin: boolean; disabled: boolean }

export interface ScimTokenInfo {
  id: string
  created: number
  /** When it ends (epoch seconds), as an administration token does; null: never. */
  expires: number | null
  last_used: number | null
  /** Told to whoever edits the sign-in settings only: who made it (a person's id or `admin:<id>`). */
  made_by?: string
  maker?: TokenMaker
}

export interface SignInView {
  sso: SsoSettings | null
  /** The settings' etag as the server holds them now (null: single sign-on off): a change made from this view names it. */
  etag: string | null
  /** Whether the dashboard's changes that apply to everyone take a session the company sign-in opened; null: no single sign-on, any administrator's session. Set with `latet server` alone. */
  company_changes: 'sso' | 'any-session' | null
  redirect_uri: string
  /** Whether the client secret is saved for the issuer; it is never sent. */
  secret: boolean
  /** Whether a secret is saved for another issuer: it is saved again before anyone signs in. */
  secret_issuer_changed: boolean
  scim: { url: string; tokens: ScimTokenInfo[] }
}

export interface SignInTest {
  ok: boolean
  error: string | null
  issuer: string | null
  authorization_endpoint: string | null
  token_endpoint: string | null
  keys: number
}

export interface NewScimToken {
  id: string
  /** Shown once: the server keeps its keyed hash. */
  token: string
  url: string
  expires: number | null
}

// --- Reports, the administration log and the SIEM (E4) ---

export type ReportPeople = 'group-only' | 'pseudonymous' | 'named'

export interface ReportCounts {
  exchanges: number
  stopped: number
  masked: number
}

export interface ReportRow extends ReportCounts {
  key: string
  /** A group's, an API key's or a person's name, a project; null: none (no group, no project). */
  label: string | null
}

export interface ReportResidual extends ReportCounts { count: number }
export interface ApproverResidual { count: number; approvals: number; changes: number }

export interface Report {
  period: { from: string; to: string; days: number; step: 'day' | 'week' }
  previous: { from: string; to: string }
  filters: { group: string | null; project: string | null; agent: string | null; category: string | null }
  people: ReportPeople
  totals: ReportCounts & {
    approvals: Record<string, number>
    changes: Record<string, number>
    /** The applications API's calls by operation (scan, mask): an API key's requests among the others. */
    apps: Record<string, number>
    uncounted: number
  }
  before: ReportCounts
  series: (ReportCounts & { key: string })[]
  by_group: ReportRow[]
  by_project: ReportRow[]
  by_agent: ReportRow[]
  /** API keys by name, whatever the company chose for people. */
  by_key: ReportRow[]
  /** Null when the company reports by group only. */
  by_person: ReportRow[] | null
  /** The decisions E5 records, by who made them; null when the company reports by group only. */
  by_approver: { key: string; approvals: number; changes: number }[] | null
  /** Counts left outside each top-200 list, including activity without a surviving name. */
  others: {
    by_group: ReportResidual
    by_project: ReportResidual
    by_agent: ReportResidual
    by_key: ReportResidual
    by_person: ReportResidual | null
    by_approver: ApproverResidual | null
  }
  by_category: { key: string; masked: number }[]
  stopped_by_reason: { key: string; count: number }[]
  options: { groups: { id: string; name: string }[]; projects: string[]; agents: string[]; categories: string[] }
  viewer: { groups: string[] | null; people: ReportPeople; siem: 'edit' | 'read' | null }
}

export interface ReportQuery {
  from: string
  to: string
  group?: string
  project?: string
  agent?: string
  category?: string
}

/** A file to save: its name, media type and text. */
export interface ReportFile {
  name: string
  type: string
  file: string
}

/** Why the license lets nothing new be produced now (a 403 `not-producing`'s `paused`). */
export interface Paused {
  why: 'expired' | 'users' | 'trial-ended' | 'unusable'
  expires: string | null
}

export type SiemKind = 'webhook' | 'splunk' | 'syslog'

export interface SiemOutputSettings {
  name: string
  kind: SiemKind
  url: string
  headers: Record<string, string>
  /** The name of its secret: the secret itself never comes back. */
  secret: string | null
  format: 'json' | 'ndjson'
}

export interface SiemOutputView extends SiemOutputSettings {
  has_secret: boolean
  /** A secret kept that was set for another address: it is set again for this one, and never sent (I1). */
  secret_destination_changed: boolean
  queued: number
  dropped: number
  last_ok: number | null
  last_error: string | null
  failing_since: number | null
  /** Its queue waits at an entry of the administration log the receiver refuses (I5). */
  blocked: boolean
  /** Also true for a run of non-administration events refused one by one. */
  held: boolean
  /** Entries of the administration log the bound collapsed into a gap marker, until it delivers again (round 3). */
  collapsed: number
}

export interface SiemView {
  outputs: SiemOutputView[]
  /** What a change made from these outputs names: another made meanwhile answers 409 `siem-changed`. */
  etag: string | null
  editable: boolean
  paused: Paused | null
}

export interface SiemTest {
  ok: boolean
  detail: string
}
/** Administration metadata: actor kinds unknown to this client retain their literal ID. */
export interface AuditActor { id: string; name: string | null; kind: string | null }
export interface AuditEntryView {
  id: number
  ts: string | null
  action: string
  actor: AuditActor
  target: { id: string; name: string | null } | null
  details: Record<string, unknown>
  address: string | null
}
export interface AuditLogView {
  period: { from: string; to: string }
  entries: AuditEntryView[]
  actions: string[]
  actors: AuditActor[]
  truncated: boolean
  addresses: boolean
}
export interface AuditQuery { from: string; to: string; action?: string; actor?: string }

// --- Central approvals: the Company approvals page (E5) ---

/** Someone on the server who decided, by id and name. */
export interface Approver {
  id: string
  name: string
}

/** Whoever asked: a person, or an API key's program (whose controllers changed its settings). */
export interface Asker extends Approver {
  kind: 'person' | 'key'
}

/** Who decides on an item: the security team, or an API key's controllers (or the admins when it has none). */
export type ApprovalRoute = 'security' | 'controllers'

/** Who asked, as the Company approvals page shows it. */
export interface Asked {
  id: string
  identity: Asker
  /** Their first group, or null. */
  group: string | null
  route: ApprovalRoute
  /** Epoch seconds. */
  created: number
  /** You asked for it: someone else decides (you may withdraw it, never approve it). */
  mine: boolean
}

/** A change that lowers protection, waiting for you to decide: what it lowers, never a value. */
export interface CompanyChange extends Asked {
  source: SettingsChange['source']
  agent: string | null
  /** What it lowers, one English sentence each (100 at most, then one line "N more"). */
  loosened: string[]
  keys: string[]
  /** Made in an API key's space by one of the people working for it, who never approves it. */
  asked_by?: { id: string; name: string | null }
}

/** A text of the Live Reviewer waiting, as the queue lists it: its agent, kinds and number, never the text. */
export interface CompanyBatchRow extends Asked {
  agent: Batch['agent']
  kinds: string[]
  items: number
}

/**
 * A text opened: as its author sees it (values included), with who asked (GET /ee/approvals/batches/{id}), and its
 * version: the server's digest of how it would leave (never of its values), which an approval names; one masked again
 * since answers 409 `changed`, nothing decided.
 */
export type CompanyText = Batch & Asked & { version: string }

/** GET /ee/approvals: oldest first, 200 at most. */
export interface CompanyQueue {
  changes: CompanyChange[]
  batches: CompanyBatchRow[]
  /** What else waits for you to decide past the 200 listed (your own requests left out): listed as these are decided. */
  more: number
}

/**
 * How something that waited for someone else ended. `approved-changed`: a text approved, then withheld where it was
 * sent, since masking it then gave otherwise than the decider approved (the rules changed meanwhile), or since its
 * request was refused before it left.
 */
export type DecidedState = ChangeState | 'withdrawn' | 'expired' | 'abandoned' | 'gone' | 'approved-unapplied' | 'approved-changed'

/** GET /ee/approvals/history: one decision, never a text. */
export interface Decided {
  id: string
  kind: 'change' | 'batch'
  route: ApprovalRoute
  identity: Asker
  created: number
  decided: number
  state: DecidedState
  /**
   * Who decided; null when nobody did (withdrawn by whoever asked, expired, given up, gone, replaced), and for an end
   * found in a queue after the fact, whose decider the record did not hear.
   */
  by: Approver | null
  /** A change: where it came from, what it lowered, and who made it in an API key's space. */
  source?: SettingsChange['source']
  loosened?: string[]
  keys?: string[]
  asked_by?: { id: string; name: string | null }
  /** A text: its agent, its kinds, how many texts. */
  agent?: string | null
  kinds?: string[]
  items?: number
}

/** Term sync (E6): a source's last sync. Never a term: `reason` is in Latet's words and codes, never a record's. */
export interface TermSyncRun {
  source: string
  kind: 'file' | 'url' | 'graph' | 'ldap'
  started: number
  finished: number
  outcome: 'ok' | 'failed'
  code: string | null
  reason: string | null
  found: number
  added: number
  gone: number
  skipped: Record<string, number>
}

/** A source of term sync; `kind` null once it is no longer set up (its terms then all no longer in the source). */
export interface TermSyncSource {
  name: string
  kind: 'file' | 'url' | 'graph' | 'ldap' | null
  every_minutes: number | null
  configured: boolean
  /** Its terms still in it, by type in `by_type`; `gone`: those no longer in it, masked until removed. */
  terms: number
  by_type: Record<string, number>
  gone: number
  /** When a sync of it was asked for by hand (seconds), until its next run takes the ask; null when none stands. */
  asked: number | null
  last: TermSyncRun | null
  next: number | null
}

/** GET /ee/term-sync: for whoever terms.manage allows. Never a term. `problems`: instances that mask no synced term. */
export interface TermSyncState {
  licensed: boolean
  paused: boolean
  masked: number
  kept: number
  most: number
  gone: number
  unreadable: number
  /** This instance's terms trail the server's (B-I1, A-R8); the page may leave it unshown. */
  behind: { loaded: number; store: number | null; seconds: number } | null
  /** In the server's words, shown as they are: the sources the instance that answered cannot read (LDAP without the
   * `ldap` extra), and instances that mask no synced term. */
  problems: string[]
  sources: TermSyncSource[]
}

/** A term no longer in its source: masked until someone with the security role removes it. */
export interface GoneTerm {
  id: string
  source: string
  type: string
  value: string
  gone: number
}

/**
 * A source as the section `term_sync` sets it (GET /ee/term-sync/sources [D4]): where it reads, what, and how its
 * fields map to types. Never a secret: a source that reads with one names where it goes (`{secret}` in a url source's
 * address or headers). The other fields are its kind's (`path`, `tenant`, `base`, `filter`...).
 */
export interface TermSourceSettings {
  name: string
  kind: 'file' | 'url' | 'graph' | 'ldap'
  every_minutes?: number
  mapping?: Record<string, string>
  url?: string
  headers?: Record<string, string>
  bind?: string | null
  [field: string]: unknown
}

/** Where a source connects, as the server says it: each address as `scheme://host:port` (none for a file), and the DN
 * a directory signs in as. */
export interface TermSourcePlace {
  addresses: string[]
  bind: string | null
}

/** GET /ee/term-sync/sources [D4]: the sources as JSON and as YAML (the editor's text), the etag a change names, and
 * where each connects, which the secret dialog says before it asks for a secret (the final review's M1). */
export interface TermSyncSources {
  sources: TermSourceSettings[]
  text: string
  etag: string
  where: Record<string, TermSourcePlace>
}

/** PUT /ee/term-sync/sources: what the save changed, by the sources' names, and the etag of the sources now. */
export interface TermSourcesSaved {
  added: string[]
  removed: string[]
  changed: string[]
  /** Each changed source's changed fields, by name. */
  fields: Record<string, string[]>
  /** In the server's words: the secrets to set again for a source moved or made to read other data, the sources the
   * instance that answered cannot read (LDAP without the `ldap` extra), and instances that mask no synced term. */
  problems: string[]
  etag: string
}
