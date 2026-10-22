// The dashboard API contract (v1), as documented in docs/dashboard-api.md.

/** STRUCT: a value of a table, JSON or key=value in a tool's output (strict data mode, field rules). */
export type EntityClass = 'SECRET' | 'CUSTOM' | 'IDENTITY' | 'PII' | 'NER' | 'LLM' | 'STRUCT'
export type Mode = 'rewrite' | 'verify' | 'passthrough'
/** `tutorial-agent` is the tutorial's pretend agent (shown as "Tutorial Agent"): it exists in the tutorial's sandbox only. */
export type Agent = 'claude-code' | 'codex' | 'opencode' | 'gemini-cli' | 'tutorial-agent' | 'other'
/** The part of Latet that found a value (src/latet/engine/modules.py). */
export type Module =
  | 'secrets'
  | 'secret-structure'
  | 'personal'
  | 'ids'
  | 'identity'
  | 'terms'
  | 'recognizers'
  | 'names'
  | 'reviewer'
  | 'live'
  | 'fields'
  | 'data'
  | 'other'
/**
 * How a value was found in this text: by a detector, as a value already in the vault, as another
 * occurrence in the same text, as a surrogate the client sent back, masked by the Claude Code plugin
 * before the daemon saw the request, or restored in an answer.
 */
export type Via = 'detector' | 'known' | 'repeat' | 'echo' | 'plugin' | 'restore'

export interface Tokens {
  input: number
  output: number
  cache_read: number
  cache_write: number
}

export interface ExchangeSummary {
  id: string
  ts: number // unix seconds (float)
  session: string
  agent: Agent
  agent_version: string | null
  upstream: string
  /** Where it went (llm.acme.com, bedrock-runtime.eu-west-1.amazonaws.com); absent in older records. */
  host?: string | null
  /** Latet signed it for Amazon: `aws-profile:<name>` or `aws-session`. */
  signed?: string | null
  path: string
  protocol: string | null
  mode: Mode
  /** "plugin": the Claude Code plugin's own final check stopped the request inside Claude Code. */
  transport: 'http' | 'ws' | 'plugin'
  model: string | null
  status: number | null
  latency_ms: number | null
  overhead_ms: number | null
  tokens: Tokens | null
  masked: number
  new_masked: number
  by_type: Record<string, number>
  /** Like by_type, counting only blocks new to the session. */
  new_by_type: Record<string, number>
  /** Values masked in this request, by class ("?" when a value's class was lost). Missing in older records. */
  by_class?: Record<string, number>
  /** Values masked in this request, by the module that found them. Missing in older records. */
  by_module?: Partial<Record<Module, number>>
  restored: number
  flags: string[]
  verified: boolean
  /** Why Latet stopped the request (blocked ones), as it told the agent. Not in the contract yet. */
  block_reason?: string | null
  /** The project of the session, when known (flag "project-off": Latet is off there). */
  project?: { id: string; name: string } | null
}

export interface Replacement {
  start: number
  end: number
  masked_start: number
  masked_end: number
  type: string
  class: EntityClass
  /** The detector rule that found the value (for one found again: the rule that found it first); null when unknown. */
  rule: string | null
  module?: Module
  via?: Via
  entity: string | null
  /**
   * False: no vault entry is linked to the value (forgotten, or one-way redacted); `original` then shows the masked
   * text for this span. A one-way redaction links none: the vault holds none for a value the detectors found, and
   * keeps one the local reviewer or the Live Reviewer found under `redact` sealed, with a fake that never leaves.
   */
  known: boolean
  /** Found in its JSON-escaped form (inside a JSON document). */
  json?: true
}

export type SlotKind =
  | 'system'
  | 'user_text'
  | 'assistant_text'
  | 'tool_input'
  | 'tool_result'
  | 'attachment'
  | 'tool_description'
  /** The input of a tool that runs outside this computer, as the model wrote it (stand-ins included). */
  | 'outbound_tool'
  /** The text read in an image of the request. */
  | 'image_text'
  | 'other'

export interface Slot {
  /** The block's id in the history (what the local reviewer's records point to). Missing in older daemons. */
  block?: string
  path: string
  kind: SlotKind
  from_model: boolean
  new: boolean
  /** Rebuilt from the vault when shown. Older daemons sent null when the original was unknown. */
  original: string | null
  masked: string
  replacements: Replacement[]
  /** The tool whose output a tool result is, when the request said. Null or missing in older records. */
  tool?: string | null
}

export type Header = [string, string]

export interface ToolCall {
  name: string
  args_masked: string
  args_restored: string
  /** masked_* offsets in args_masked, start/end in args_restored. Missing in older records. */
  spans?: Replacement[]
}

export interface ExchangeResponse {
  headers: Header[]
  text_masked: string
  text_restored: string
  /** masked_* offsets in text_masked, start/end in text_restored. Missing in older records. */
  text_spans?: Replacement[]
  tool_calls: ToolCall[]
}

export interface Leak {
  type: string
  kind: string
  preview: string
  new: boolean
  /** Auto mode review: the identity detail the value is. */
  detail?: ReviewDetail
  /** Auto mode review: let through to the reviewer, as the rules allow. */
  allowed?: boolean
  // Where the value is, when the history keeps texts: the block it is in (`Slot.block`) and its place in it
  // (code points). Never the value: it is in the block's text, which the page reads. Missing in older records.
  block?: string
  class?: EntityClass
  rule?: string | null
  /** Offsets in the block as it was sent (`Slot.masked`). */
  masked_start?: number
  masked_end?: number
  /** Offsets in the block as it is on this computer (`Slot.original`). */
  start?: number
  end?: number
}

/** An image of the request, as it was sent. */
export interface ExchangeImage {
  /** Where its bytes are kept (`GET /images/{id}`); null when it was not sent or is too large to keep. */
  id: string | null
  path: string
  /** masked: painted over where values were read; clean: nothing to mask; withheld: not sent. */
  state: 'masked' | 'clean' | 'withheld'
  /** The block of the text read in it. */
  block: string | null
  /** Its bytes are still kept (pruned with the history). */
  kept?: boolean
}

export interface ExchangeDetail {
  summary: ExchangeSummary
  request: { headers: Header[]; slots: Slot[]; images?: ExchangeImage[]; body_bytes: number }
  response: ExchangeResponse | null
  leaks: Leak[]
  /** See ExchangeSummary.block_reason. */
  block_reason?: string | null
}

export interface Session {
  key: string
  agent: Agent
  first_ts: number
  last_ts: number
  exchanges: number
  masked: number
  blocked: number
}

export interface VaultEntity {
  id: string
  type: string
  class: EntityClass
  surrogate: string
  created: number
  sessions: number
}

export type Profile = 'balanced' | 'max' | 'secrets'
export type Policy = 'mask' | 'redact' | 'allow'
/** What the dashboard keeps of each exchange: everything, counts/timings/flags only, or nothing. */
export type HistoryMode = 'full' | 'metadata' | 'off'
/** Local name and address detection: used once installed, or never. */
export type NerMode = 'auto' | 'off'
/** What Claude Code's auto mode reviewer is sent of this machine's identity. */
export type ReviewDetail = 'user_name' | 'paths' | 'host_name' | 'git_author'
/** A review carrying a blocked detail is stopped (Claude Code asks the user instead). */
export type ReviewChoice = 'block' | 'allow'
/** Per detail. `surrogate`: Claude Code is given the user name's surrogate, and the real one blocks. */
export type AutoModeReview = Record<Exclude<ReviewDetail, 'user_name'>, ReviewChoice> & {
  user_name: ReviewChoice | 'surrogate'
}

export interface NerStatus {
  /** The `ner` extra (the model runtime) is installed. */
  runtime: boolean
  /** The model is downloaded (`latet ner install`). */
  model: boolean
  /** The detector is in use now. */
  active: boolean
}

export interface Term {
  value: string
  type: string
  case_sensitive: boolean
  whole_word: boolean
}

export interface IdentityView {
  enabled: boolean
  usernames: string[]
  hostname: string | null
  /** Other host names, masked as `hostname` is: on a Latet server, those of the person's other computers. */
  hostnames?: string[]
  full_name: string | null
  emails: string[]
}

/** A recognizer as written in config.yaml (a broken entry can hold anything). */
export interface RecognizerEntry {
  name?: string
  type?: string
  description?: string
  pattern?: string
  checksum?: string | WeightedChecksum
  context?: { words?: string[]; acronyms?: string[]; window?: number; header?: boolean }
  examples?: { match?: string[]; no_match?: string[] }
  class?: 'custom' | 'secret'
  policy?: Policy
  surrogate?: 'shape' | { keep: number }
  min_length?: number
  enabled?: boolean
  [key: string]: unknown
}

export interface WeightedChecksum {
  algorithm: 'weighted'
  weights: number[]
  modulus: number
  remainder?: number
}

/** How a recognizer stands after loading. */
export interface RecognizerStatus {
  name: string
  state: 'active' | 'off' | 'error'
  errors: string[]
  warnings: string[]
}

export interface RecognizerExample {
  text: string
  expect: 'match' | 'no_match'
  ok: boolean
  found: string[]
  /** Why the pattern's matches are not values (or "the pattern does not match"). */
  reason: string | null
}

/** POST /recognizers/test: the same report as `latet rules test`, and the Try it text. */
export interface RecognizerReport extends RecognizerStatus {
  type?: string
  class?: string
  examples?: RecognizerExample[]
  stdlib?: { matches: number; samples: string[] }
  try?: { found: string[]; reason: string | null; spans: { start: number; end: number }[] }
}

export interface ChecksumInfo {
  name: string
  description: string
}

export interface PiiCatalogEntry {
  type: string
  /** ISO 3166-1 alpha-2; null for the types of no particular country. */
  country: string | null
}

/** Strict data mode masks every value of the tables, JSON and key=value pairs of tool outputs. */
export type DataMode = 'normal' | 'strict'
/** block: a request carrying an image or a PDF is not sent. */
export type ImagesMode = 'allow' | 'mask' | 'block'
export type OcrMode = 'auto' | 'off'

/** A rule for the values of one field: a column, a JSON key or path (`items[].sku`), the key of key=value. */
export interface FieldRule {
  /** `*` matches any characters; case does not matter. */
  name: string
  /** The type its values are masked as (FIELD when not set). */
  type?: string | null
  /** False: its values are kept in clear in strict data mode (your own settings only). Default true. */
  mask?: boolean
  /** Its values are secrets: one in clear blocks the request. */
  secret?: boolean
}

/** Strict data mode, guided: what Latet proposes for a column of a tool's output (`ask`: your call). */
export type ColumnProposal = 'keep' | 'mask' | 'ask'
/**
 * Why: keep when its name says ids, dates of events, states, flags or counts; mask for secrets, people,
 * sensitive topics and free text; ask otherwise (dates, yes/no or repeating values whose name says nothing).
 */
export type ColumnReason =
  | 'dates'
  | 'ids'
  | 'states'
  | 'flags'
  | 'counts'
  | 'secret-name'
  | 'secret'
  | 'people'
  | 'person'
  | 'free-text'
  | 'birth-dates'
  | 'sensitive'
  | 'amounts'
  | 'dates-unclear'
  | 'flags-unclear'
  | 'repeats'
  | 'unclear'

/** One column of a tool's output (a table's, a JSON path's, a key's), as `GET /exchanges/{id}/columns` gives it. */
export interface OutputColumn {
  name: string
  /** How many of its values strict data mode masked in this output. */
  masked: number
  proposal: ColumnProposal
  reason: ColumnReason
  /** What was found in it (EMAIL), or the states seen. */
  detail: string | null
  /** A rule in effect there keeps it in clear already. */
  kept_now: boolean
  /** A rule for everywhere would change it there: false when a rule of the project's own matches it first. */
  everywhere: boolean
  /** Up to three of its values, in clear. */
  examples: string[]
}

export interface OutputColumns {
  columns: OutputColumn[]
  /** Strict data mode is on where the exchange was (its project, or the global rules). */
  strict: boolean
  /**
   * Strict data mode covers this tool's output, by the rule its masking follows: false for a file read that no rule of
   * `data_files` names, a web tool's result that neither `data_hosts` (its address) nor `data_tools` names, or a tool
   * `data_tools` leaves out.
   */
  applies: boolean
  /** `loaded: false`: Latet restarted since, and the project's settings load at its next session. */
  project: { id: string; name: string; loaded: boolean } | null
  /** The local reviewer is on: its opinion can be asked. */
  reviewer: boolean
}

/**
 * The light answer of `GET /exchanges/{id}/columns?slot=&shape=1`, what the exchange's notice needs: no proposal is
 * worked out for it (no detector runs), and the output is read for columns only when strict data mode covers the tool.
 */
export interface OutputShape {
  /** The names of the output's columns; none when strict data mode does not cover the tool (`applies` false). */
  columns: string[]
  /** Strict data mode is on where the exchange was (its project, or the global rules). */
  strict: boolean
  /** Strict data mode covers this tool's output (as `OutputColumns.applies`). */
  applies: boolean
}

/** The local reviewer's opinion on each column: shown beside the proposals, never applied. */
export interface ColumnOpinions {
  opinions: Record<string, { keep: boolean; why: string }>
}

/** What the local reviewer may read before a text leaves. */
export type ReviewKind = 'user_text' | 'attachment' | 'system' | 'tool_result' | 'tool_input' | 'tool_description' | 'image_text'
export type ReviewCategory =
  | 'person'
  | 'address'
  | 'phone'
  | 'email'
  | 'id'
  | 'health'
  | 'sensitive'
  | 'financial'
  | 'secret'
  | 'org'
  | 'confidential'
/** How much the reviewer reads in a project: nothing, after a text left, or before. */
export type ReviewMode = 'off' | 'background' | 'inline'

/** A server certificate, as shown to compare it: never trusted until you say so. */
export interface ServerCertificate {
  fingerprint: string
  /** AB:CD:..., as openssl and servers print it. */
  readable: string
}

/** The local reviewer: a language model on your own server (config.yaml `llm_review`). */
export interface LlmReviewSettings {
  enabled: boolean
  /** Its OpenAI-compatible server: on this machine, or on another one of yours with `remote`. */
  url: string
  /** The server is yours, on another machine: it is sent the original texts (https outside the local network). */
  remote: boolean
  /** null: the first model the server lists. */
  model: string | null
  /** The variable holding the server's API key (the key itself is never in the settings). */
  api_key_env: string
  api_key_file: string | null
  inline: ReviewKind[]
  /** null: a request waits as long as its review takes. */
  budget_ms: number | null
  on_failure: 'stop' | 'send'
  background: boolean
  /** null: what the server says. */
  slots: number | null
  /** sha256:<hex> of the server's own certificate you trust (self-signed, on a machine of yours); null: none. */
  certificate?: string | null
  block: boolean
  auto_mask: ReviewCategory[]
  chunk_chars: number
  /** It is shown the images too, with the text read in them (a model that sees images). */
  images?: boolean
  /** Your guidance, after Latet's own instructions (at most `ReviewerPrompt.max_guidance` characters); null: none. */
  guidance?: string | null
}

/** GET /reviewer/prompt: the reviewer's own instructions, the very text the daemon sends it (read-only). */
export interface ReviewerPrompt {
  system: string
  /** The sentence before your guidance, when there is some. */
  intro: string
  version: string
  /** The most characters of guidance: the global one, and a project's. */
  max_guidance: number
}

/** Whose space a dashboard on a Latet server is: a person's, or an API key's. */
export interface ServerIdentity {
  id: string
  name: string
  kind: 'person' | 'key'
}

/** On a Latet server: its address, and whose space this is. */
export interface ServerMe {
  url: string
  identity: ServerIdentity | null
}

export interface ConfigView {
  /** The settings in use as read (`latet.runtime.digest`): a save sends it back, and is refused (409, `settings_stale`)
   * when they changed since. On a Latet server a save without it is refused alike. */
  etag: string
  profile: Profile
  pii: string[] | null
  pii_available: string[]
  /** Read-only: every type, with its country, in display order. */
  pii_catalog: PiiCatalogEntry[]
  /** Read-only: the types of no particular country used by default. */
  pii_base: string[]
  /** Read-only: the types in use with the saved rules. */
  pii_effective: string[]
  /** Countries whose ID and tax numbers are recognised (the Max profile: all). */
  id_regions: string[]
  /** Read-only. */
  id_regions_available: string[]
  phone_regions: string[]
  include_private_ips: boolean | null
  terms: Term[]
  recognizers: RecognizerEntry[]
  /** Read-only: the saved recognizers, as loaded. */
  recognizer_status: RecognizerStatus[]
  /** Read-only: the check-digit algorithms a recognizer may name. */
  checksums: ChecksumInfo[]
  allow_values: string[]
  allow_patterns: string[]
  policies: Record<string, Policy>
  identity: IdentityView
  history: HistoryMode
  /** 1 to 365. */
  retention_days: number
  ner: NerMode
  /** Read-only. */
  ner_status: NerStatus
  auto_mode_review: AutoModeReview
  /** null: not set, the profile decides (Max turns strict data mode on). */
  data: DataMode | null
  /** Read-only: strict data mode as it applies, the profile's when `data` is not set. */
  data_effective: DataMode
  /** Strict data mode only for these tools (`*` matches any characters); empty: every tool. */
  data_tools: string[]
  /** The files strict data mode covers when a tool reads them (path patterns: `*.json`, `exports/**`). */
  data_files: string[]
  /** The web addresses strict data mode covers when a web tool fetches them (host patterns: `*.salesforce.com`). */
  data_hosts: string[]
  fields: FieldRule[]
  /** At most this many personal values in one tool output (null: no limit). */
  dump_guard: number | null
  images: ImagesMode
  /** Reading the text in images, once the models are installed. */
  ocr: OcrMode
  /** Read-only: the text recognition models, as `ner_status` for the name model. */
  ocr_status: NerStatus
  llm_review: LlmReviewSettings
  live_review: LiveReviewSettings
  /** Where agents' requests go after Latet, besides the providers it knows. */
  upstreams?: Destination[]
}

/** PUT /config takes any subset, with the etag of the GET /config it was made from; identity is merged field by field. */
type ReadOnlyConfig =
  | 'etag'
  | 'identity'
  | 'pii_available'
  | 'pii_catalog'
  | 'pii_base'
  | 'pii_effective'
  | 'id_regions_available'
  | 'ner_status'
  | 'ocr_status'
  | 'recognizer_status'
  | 'checksums'
  | 'data_effective'
export type ConfigUpdate = Partial<Omit<ConfigView, ReadOnlyConfig>> & {
  identity?: Partial<IdentityView>
}

export interface Percentiles {
  p50: number | null
  p95: number | null
}

export interface DayStats {
  day: string // YYYY-MM-DD
  exchanges: number
  masked: number
  blocked: number
}

export interface Stats {
  by_day: DayStats[]
  by_type: Record<string, number>
  by_agent: Record<string, number>
  latency_ms: Percentiles
  overhead_ms: Percentiles
  cache_read_ratio: number | null
  /** The period holds more exchanges than one read counts: these are the latest `latest` of them. */
  partial?: boolean
  latest?: number
}

export type CheckStatus = 'ok' | 'warn' | 'fail'

export interface Check {
  status: CheckStatus
  title: string
  detail: string
}

/** Where an agent's requests go, as its settings say: Anthropic, a company's gateway, Bedrock... */
export interface AgentRoute {
  summary: string | null
  /** Why Latet cannot go in front of it, when it cannot. */
  problem: string | null
  notes: string[]
}

export interface AgentIntegration {
  id: string
  name: string
  installed: boolean
  version: string | null
  status: string // "applied" | "partial" | "not applied" | "wrapper" (through Latet when run with its command)
  notes: string[]
  route?: AgentRoute | null
}

/** A destination of your settings (`upstreams`): requests to `/<name>/` go to `url`. */
export interface Destination {
  name: string
  url: string
  /** The agent whose settings it was read from; null: written by you. */
  agent?: 'claude' | 'codex' | null
}

export interface DestinationUse extends Destination {
  /** Epoch seconds of the last exchange through it. */
  last_used: number | null
  /** Whether Claude Code or Codex uses it: its settings point at it (yours, your organization's, Codex's), or it was registered since the daemon started (`latet claude`, `latet codex`, `agents apply`, `global-install`). Another agent's use is not seen. */
  in_use: boolean
  /** Set for everyone on the Latet server: only an administrator removes it. */
  shared?: boolean
}

export interface Integrations {
  checks: Check[]
  agents: AgentIntegration[]
  destinations?: DestinationUse[]
}

export interface Me {
  version: string
  profile: Profile
  port: number
  data_dir: string
  /** Whether exchanges are being recorded for the dashboard. */
  recording: boolean
  /** Set when this is the tutorial's sandbox (`latet tutorial`), null or missing on a real daemon. */
  tutorial?: TutorialMe | null
  /** On a Latet server (null on a computer). */
  server?: ServerMe | null
  /** This install serves the documentation of its own version at /_latet/docs/ (else the links go to docs.latet.ai). */
  docs: boolean
}

export interface Page<T> {
  items: T[]
  next: string | null
}

export interface ExchangeQuery {
  limit?: number
  before?: string
  session?: string
  agent?: string
  flag?: string
  project?: string
}

/** The masking keys of config.yaml, for one project. A repository file may set only REPO_KEYS. */
export interface ProjectSettings {
  profile?: Profile | null
  pii?: string[]
  id_regions?: string[]
  phone_regions?: string[]
  include_private_ips?: boolean | null
  ner?: NerMode | null
  terms?: Term[]
  recognizers?: RecognizerEntry[]
  allow_values?: string[]
  allow_patterns?: string[]
  policies?: Record<string, Policy>
  disabled_secret_rules?: string[]
  data?: DataMode | null
  data_tools?: string[]
  data_files?: string[]
  data_hosts?: string[]
  fields?: FieldRule[]
  dump_guard?: number | null
  images?: ImagesMode | null
  /** Reading the text in images there (`off`: not read). */
  ocr?: OcrMode | null
  llm_review?: ReviewMode | null
}

/**
 * Your own settings for a project; `enabled: false` turns Latet off there. `live_review` turns the
 * Live Reviewer on or off there (null: as in the global settings). `review_guidance`: your guidance to
 * the local reviewer there, after the global one (a repository file cannot set it).
 */
export type PersonalSettings = ProjectSettings & {
  enabled?: boolean
  live_review?: boolean | null
  review_guidance?: string | null
  /** The local reviewer is shown the images here (null: as in the global settings). */
  review_images?: boolean | null
}

export interface ProjectSummary {
  id: string
  name: string
  /** The project's folder. */
  root: string
  /** Its latet.yaml, when it has one. */
  repo_file: string | null
  has_personal: boolean
  enabled: boolean
  profile: Profile
  notice_count: number
  last_seen: number
}

export interface ProjectNotice {
  source: 'repo' | 'personal'
  key: string
  level: 'ignored' | 'error'
  message: string
  /** Why, for a notice the dashboard words in your language (lib/refused.ts); its message is the daemon's English. */
  reason?: string
}

export interface ProjectDetail extends ProjectSummary {
  repo: {
    /** Where the file is, or would go (null: the folder is not on this machine). */
    path: string | null
    exists: boolean
    settings: ProjectSettings
    /** What was read: a save over a file changed since is refused. */
    tag: string
    /** False when the file there could not be read (not a regular file, too large, refused): nothing is saved over it. */
    readable: boolean
    /** False in the container, when the folder is not on this machine, or when the file cannot be read. */
    writable: boolean
  }
  personal: PersonalSettings | null
  notices: ProjectNotice[]
  effective: {
    profile: Profile
    pii: string[]
    id_regions: string[]
    phone_regions: string[]
    include_private_ips: boolean | null
    ner: NerMode
    terms: number
    recognizers: number
    allow_values: number
    allow_patterns: number
    policies: Record<string, Policy>
    data: DataMode
    data_tools: string[]
    data_files: string[]
    data_hosts: string[]
    fields: FieldRule[]
    dump_guard: number | null
    images: ImagesMode
    llm_review: ReviewMode
  }
  /** The project's recognizers as loaded (empty: the global rules apply as they are). */
  recognizer_status: RecognizerStatus[]
}

export interface SuggestShape {
  shape: string
  count: number
  distinct: number
  file_count: number
  files: string[]
  checksum: string | null
  labels: { word: string; values: number }[]
}

export interface SuggestResult {
  files: number
  shapes: SuggestShape[]
  drafts: RecognizerEntry[]
}

export interface VaultPage {
  /** One page of the matching entities, the newest first. */
  items: VaultEntity[]
  /** All matching entities. */
  total: number
  /** The page shown (a page past the last is the last), counted from 1, of `pages` (at least 1). */
  page: number
  pages: number
  per_page: number
  /** Every type the vault holds, with how many values of it: the filter's menu, which does not shrink as you filter. */
  types: Record<string, number>
}

/** What the Vault page asks for: a search, a type, a class and a page. */
export interface VaultQuery {
  q?: string
  type?: string
  class?: string
  page?: number
  perPage?: number
}

export interface PlaygroundResult {
  masked: string
  replacements: Replacement[]
}

/** The Live Reviewer: every new text waits for your approval in the dashboard (config.yaml `live_review`). */
export interface LiveReviewSettings {
  enabled: boolean
  /** What waits for you. */
  kinds: ReviewKind[]
  /** Send without asking a text in which neither Latet nor the local reviewer found anything. */
  auto_approve_clean: boolean
  /** Seconds a text waits before it is not sent (at least 10); null: until you answer. */
  timeout_s: number | null
}

/** What the local reviewer found in a waiting text and did not mask (offsets in `original`, code points). */
export interface LiveSuggestion {
  start: number
  end: number
  category: string
}

/** One new text of a batch waiting for your approval. */
export interface LiveItem {
  kind: SlotKind
  path: string | null
  tool: string | null
  /** Rebuilt from the vault, as on the Exchange page. */
  original: string
  /** What leaves when you approve it as it is. */
  masked: string
  /** Offsets on both sides, in code points, like a slot's. */
  replacements: Replacement[]
  suggestions: LiveSuggestion[]
  /** The local reviewer could not read it: you decide. */
  failed: string | null
  /** The image this text was read in (kind image_text). */
  image?: LiveImage
}

/** An image waiting in the Live Reviewer: where each line of its text is, and each masked value. */
export interface LiveImage {
  width: number
  height: number
  media_type: string
  /** Each line read: its box, and where its text is in `original` (code points). */
  lines: { box: [number, number, number, number]; start: number; end: number }[]
  /** Where each masked value is covered (offsets in `original`). */
  covered: { rect: [number, number, number, number]; start: number; end: number; type: string | null }[]
  /** Rectangles you drew on this image before (kept for it). */
  rects: [number, number, number, number][]
  withheld: boolean
}

/** What you did to one image of a batch. */
export interface ApprovalImage {
  item: number
  rects?: [number, number, number, number][]
  withhold?: boolean
}

/** What one request (gateway) or one plugin call brings that is new, waiting for you. */
export interface Batch {
  id: string
  session: string
  /** The session's project id. */
  project: string | null
  agent: Agent
  /** Epoch seconds. */
  created: number
  /** When it is not sent any more (live_review.timeout_s); null: it waits until you answer. */
  expires: number | null
  items: LiveItem[]
}

/** How a batch left the queue. */
export type BatchState = 'approved' | 'rejected' | 'abandoned' | 'expired'

/** A value you masked by hand: offsets in that item's `original` (code points). */
export interface ApprovalMask {
  item: number
  start: number
  end: number
  /** The value at those offsets as you saw it: found again where it is now if the text was shown anew meanwhile. */
  value: string
  category: ReviewCategory
}

/** POST /approvals/{id}. */
export interface ApprovalDecision {
  action: 'approve' | 'reject'
  masks?: ApprovalMask[]
  images?: ApprovalImage[]
}

/**
 * A change to your settings that lowers protection: it does not apply until you approve it. Made by an
 * `latet` command an agent ran, config.yaml edited by hand, or found in config.yaml when Latet
 * started (it runs with the settings you approved until you decide). At most one waits.
 */
export interface SettingsChange {
  id: string
  /** Epoch seconds. */
  created: number
  source: 'command' | 'file' | 'start'
  /** The agent that ran the command (hermes, claude-code...); null when unknown. */
  agent: string | null
  /** What it lowers, one English sentence each, never a value. */
  loosened: string[]
  /** The top-level config.yaml keys it changes. */
  keys: string[]
  /** The local reviewer's guidance it writes, before and after: free text, to read before approving. */
  guidance?: GuidanceChange[]
  /** The destinations it moves or removes, with their addresses before and after (null: removed). */
  destinations?: DestinationChange[]
}

export interface DestinationChange {
  name: string
  before: string | null
  after: string | null
}

/** Where a change writes the reviewer's guidance: `where` null for the global one, else the project's name. */
export interface GuidanceChange {
  where: string | null
  before: string | null
  after: string | null
}

/** How a change to your settings stopped waiting. */
export type ChangeState = 'approved' | 'rejected' | 'stale' | 'superseded'

/** GET /approvals/stream: the queue first, then what changes. */
export type ApprovalEvent =
  | { type: 'snapshot'; batches: Batch[]; changes: SettingsChange[]; viewers: number }
  | { type: 'pending'; batch: Batch }
  /** A batch still waiting, masked again with the rules of now (they or the values masked changed): as it will leave. */
  | { type: 'updated'; batch: Batch }
  | { type: 'settled'; id: string; state: BatchState }
  | { type: 'change'; change: SettingsChange }
  | { type: 'change_settled'; id: string; state: ChangeState }
  | { type: 'viewers'; viewers: number }

/** One value the local reviewer found in a block: where it is in the block as it left, never the value. */
export interface ReviewFinding {
  category: ReviewCategory
  /** Masked by itself (its category is one it masks); otherwise only reported. */
  masked: boolean
  /** Left in clear because you marked the value Not sensitive (missing in older records). */
  allowed?: boolean
  /** The vault entry of a masked value. */
  entity: string | null
  /** Offsets in the block's masked text: the surrogate of a masked value, the value itself otherwise. */
  at: [number, number] | null
}

/** What the local reviewer said of one block (GET /reviews, and live as `review` events). */
export interface ReviewRecord {
  id: string
  ts: number
  session: string
  project: string | null
  block: string
  kind: SlotKind
  /** Reviewed before the request left (false: after, in the background). */
  inline: boolean
  findings: ReviewFinding[]
  /** Why the review did not happen or is not trusted: timeout, unreachable, unreadable, suspect, busy. */
  failed: string | null
  /** It asked to stop the text as a whole. */
  block_asked: boolean
  seconds: number
}

/** GET, PUT, DELETE /reviewer/key: where the reviewer server's API key comes from (never the key). */
export interface ReviewerKey {
  /**
   * `saved`: from the dashboard or `latet llm key`; `env`: from `variable`; `file`: from api_key_file; `server`: on a
   * Latet server, given by the server (its administrator sets it, for everyone).
   */
  source: 'saved' | 'env' | 'file' | 'server' | null
  variable: string | null
  /** After a PUT: where it was saved. */
  stored: 'keyring' | 'file' | null
}

/** POST /reviewer/test: the reviewer tried on a sample text (or one typed), with the settings given. */
export interface ReviewerTest {
  ok: boolean
  error: string | null
  models: string[]
  model: string | null
  slots: number | null
  n_ctx: number | null
  /** Its model sees images (llama.cpp with --mmproj); null: the server does not say. */
  vision?: boolean | null
  seconds: number | null
  text: string
  /** The text as the reviewer read it: the detectors' replacements as placeholders. */
  view: string
  findings: { text: string; category: ReviewCategory; masked: boolean }[]
  block: boolean
}

/** GET /rules/export: your rules (or your own settings for a project) as a file, for a colleague. */
export interface RulesExport {
  /** The file name to save it as. */
  name: string
  /** The YAML text: terms are in it in clear. */
  file: string
  warnings: string[]
}

/** What importing a rules file does, one English sentence each. */
export interface RulesReport {
  /** Adds protection: applied at once. */
  added: string[]
  /** Yours already, or yours kept (the same name, the same field). */
  skipped: string[]
  /** Would lower protection: applied only with `all`. */
  held: string[]
  /** What in the file was not read. */
  notices: string[]
}

/** POST /rules/import: the report, and whether it was applied (`apply`). */
export interface RulesImport {
  report: RulesReport
  applied: boolean
  rules_version?: number | string
}

/** POST /recognizers/draft: a recognizer the reviewer's model drafted, and what `rules test` says of it. */
export interface RecognizerDraft {
  draft: RecognizerEntry
  report: RecognizerReport
}

// ---- The tutorial (`latet tutorial`): routes under /tutorial exist in its sandbox only --------------

/** What a step waits for the user to do in the real dashboard. `pin`: open a value (the page sees it); the others: the daemon sees the effect. */
export type TutorialWaitKind = 'pin' | 'term' | 'allow' | 'approve' | 'change' | 'fields' | 'guidance'

/** `watch`: press the button and see the effect; `do`: an action in the real dashboard; `read`: explanation only. */
export type TutorialStepKind = 'watch' | 'do' | 'read'

/** `GET /me`'s `tutorial`: where the sandbox is. */
export interface TutorialMe {
  /** The chapter in progress and its step; null at the menu, and once the last step of the chapter ran (it is done). */
  current: { chapter: string; number: number; title: string; step: number } | null
  /** The local reviewer works here: the Tutorial Reviewer, simulated, in a chapter that has it. Missing or false: the sandbox refuses it. */
  reviewer?: boolean
}

/** One chapter of the menu. */
export interface TutorialCard {
  id: string
  /** 1-based, in the order the chapters are meant to be done. */
  number: number
  title: string
  minutes: number
  /** What it teaches: three short lines. */
  learn: string[]
  phase: 1 | 2
  done: boolean
  /**
   * What the chapter needs that this computer does not have yet: `ocr`, text recognition's models. It cannot start until
   * they are there; the sandbox looks for them again at each call.
   */
  missing: string[]
}

/** A step as the page needs it before it runs (the explanation comes with the state). */
export interface TutorialStep {
  index: number
  kind: TutorialStepKind
  title: string
  /** What the agent is told, shown in its pane. */
  prompt: string | null
  /** The pane's button ("Send it", "Let it write"); null: the step starts by itself. */
  button: string | null
  wait: {
    kind: TutorialWaitKind
    hint: string
    /** `guidance`: the sentence written in for the reader. */
    text?: string
  } | null
  /**
   * The conversation the step resumes from before Latet was on, as the agent's pane shows it (a `system` line, then
   * what was said, `resumed`): on screen from the start of the step, as /resume shows it, and the same lines the log
   * holds once the step ran. Empty for a step that resumes none.
   */
  resumed: TutorialLine[]
}

export interface TutorialChapter extends TutorialCard {
  agent: { name: string; model: string }
  steps: TutorialStep[]
}

/**
 * GET /tutorial/chapters: the menu. `requested` is the chapter named on the command line, given once. `current` is
 * the chapter in progress, until its last step ran: then its card says `done` and it is not in progress (a chapter
 * done and begun again is in progress).
 */
export interface TutorialMenu {
  chapters: TutorialCard[]
  requested: string | null
  current: { chapter: string; step: number } | null
}

/** The step waiting for an action of the user. */
export interface TutorialWaiting {
  step: number
  kind: TutorialWaitKind
  /** The daemon saw the effect. For `pin` it never does: the page sees it and says so with `next`. */
  satisfied: boolean
}

export interface TutorialStepResult {
  index: number
  ran: boolean
  /** Two to four plain sentences, with what the model received filled in once the step ran. */
  explain: string
  /** Its request was not sent: the dashboard says so in the reader's language (`explain` is the English). */
  not_sent?: boolean
  /** What the right pane shows. A `do` step has it before it runs (what it asks to look at). */
  shown: TutorialShown | null
}

/** A system line the dashboard says in the reader's language: a conversation resumed, or a new one. */
export type TutorialNote = 'resumed' | 'cleared'

/** What the agent's pane shows, in order. */
export type TutorialLine = { step: number } & (
  /**
   * `resumed`: said in a conversation from before Latet was on, which the step resumes. `note`: a system line the
   * dashboard says in the reader's language (`text` is the English).
   */
  | { type: 'prompt' | 'agent' | 'model' | 'error' | 'system'; text: string; resumed?: boolean; note?: TutorialNote }
  | { type: 'tool'; name: 'Read' | 'Write' | 'Bash'; summary: string; output?: string; pending?: boolean }
  | { type: 'file'; path: string; content: string }
)

/**
 * What the right pane shows for a step, resolved by the daemon. `exchange` is an exchange id of the
 * sandbox's history. `highlight` lists ORIGINAL values (the invented ones): the page works out which
 * marks they are (`targetOf`). `approvals`, `changes` and `columns` embed the real Approvals queue, the
 * change waiting for the user, and the strict data mode columns panel of `exchange`'s block `slot`. `review` is
 * the request of `exchange` with the Local reviewer panel of its block `slot` under it; `images` is the request of
 * `exchange` with its image above it, first as it is on this computer (`original`), then as it was sent; `slot` names
 * the block of the text read in the image; `guidance` embeds Rules → Reviewers → Local reviewer → Your guidance.
 */
export interface TutorialShown {
  component: 'exchange' | 'why' | 'terms' | 'command' | 'approvals' | 'changes' | 'columns' | 'review' | 'images' | 'guidance' | 'none'
  exchange?: string
  part?: 'request' | 'response'
  /** Request: the index in the request's blocks. Response: the tool call's index, -1 for the text. */
  slot?: number
  highlight?: string[]
  /** `images`: the picture as it is on this computer, a path under the API (already percent-encoded: `api.tutorial.picture`). */
  original?: string
  /** `guidance`: the sentence written in for the reader. */
  text?: string
  focus?: 'values' | 'block' | 'why' | 'terms'
  command?: string
  snippet?: string
  rules?: string
}

/** The end of a chapter: what it taught, where that is in the dashboard (paths), and the chapter after. */
export interface TutorialEnd {
  learned: string[]
  where: { label: string; to: string }[]
  next: string | null
}

/** Where a chapter stands (GET /tutorial/chapters/{id}/state, and what `start` and `next` answer). */
export interface TutorialState {
  chapter: string
  /** Changes at each start: another tab can tell the chapter started over. */
  run: string
  /** The step that runs next (the number of steps once the chapter is finished). */
  next: number
  /** The agent is working: poll the state until it is not. A request held in the Live Reviewer is busy until you decide. */
  busy: boolean
  finished: boolean
  lines: TutorialLine[]
  /** One per step, in order. */
  steps: TutorialStepResult[]
  waiting: TutorialWaiting | null
  end: TutorialEnd | null
  error: string | null
  /** The step `error` is about: the one asked for last. The error stays until the next call, so it can outlive that step. */
  error_step: number | null
}

/** POST /tutorial/chapters/{id}/next: `seen: ["pin"]` says the page saw the value opened. */
export interface TutorialNext {
  step: number
  seen?: ['pin']
}
