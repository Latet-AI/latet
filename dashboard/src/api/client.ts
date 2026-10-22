import type {
  ApprovalDecision,
  ApprovalEvent,
  Batch,
  BatchState,
  ChangeState,
  ColumnOpinions,
  ConfigUpdate,
  ConfigView,
  ExchangeDetail,
  ExchangeQuery,
  ExchangeSummary,
  Integrations,
  LlmReviewSettings,
  Me,
  OutputColumns,
  OutputShape,
  Page,
  PersonalSettings,
  PlaygroundResult,
  Profile,
  ProjectDetail,
  ProjectSettings,
  ProjectSummary,
  RecognizerDraft,
  RecognizerEntry,
  RecognizerReport,
  ReviewerKey,
  ReviewerPrompt,
  RulesExport,
  RulesImport,
  ReviewerTest,
  ServerCertificate,
  ReviewRecord,
  Session,
  SettingsChange,
  SlotKind,
  Stats,
  SuggestResult,
  TutorialChapter,
  TutorialMenu,
  TutorialNext,
  TutorialState,
  VaultPage,
  VaultQuery,
} from './types'

export const API_BASE = '/_latet/api/v1'

export type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'
export type StreamState = 'connecting' | 'open' | 'retrying' | 'closed'

/** How the dashboard reaches the daemon: HTTP for real, in-memory fixtures in mock mode. */
export interface Transport {
  request(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<unknown>
  /** Live exchanges (`GET /stream`), and the local reviewer's records. Returns a function that closes the stream. */
  subscribe(
    onExchange: (exchange: ExchangeSummary) => void,
    onState: (state: StreamState) => void,
    onReview?: (review: ReviewRecord) => void,
  ): () => void
  /**
   * The Live Reviewer's queue (`GET /approvals/stream`), with the history on or off: each open one
   * counts as a dashboard watching. Returns a function that closes the stream.
   */
  subscribeApprovals(onEvent: (event: ApprovalEvent) => void, onState: (state: StreamState) => void): () => void
  /** Where a picture of the API is shown from (an `<img>` reads it, not `request`): `path` under the API. */
  assetUrl?(path: string): string
  /** `GET /_latet/health`, which needs no sign-in: the sign-in screen asks whether this is the tutorial's sandbox. */
  health(signal?: AbortSignal): Promise<Health>
  readonly mock: boolean
}

/** What `GET /_latet/health` says that the dashboard uses. */
export interface Health {
  /** Only in the tutorial's sandbox. */
  tutorial?: boolean
  /** Answered by a Latet server's instance. */
  server?: boolean
  /** It serves the documentation of its version at /_latet/docs/ (what the sign-in page opens). */
  docs?: boolean
}

export class UnauthorizedError extends Error {
  constructor() {
    super('unauthorized')
    this.name = 'UnauthorizedError'
  }
}

export class ApiError extends Error {
  readonly status: number
  /** Validation details (PUT /config answers 400 {"error": "invalid configuration", "details": [...]}). */
  readonly details: unknown[]
  /** The answer's JSON as it came, when it was (a tutorial 409 carries the chapter's `state`). */
  readonly body: unknown
  constructor(status: number, message: string, details: unknown[] = [], body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.details = details
    this.body = body
  }
}

/** The daemon could not be reached at all (stopped, restarting, wrong port). */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

type AuthListener = () => void
const authListeners = new Set<AuthListener>()

/** Called whenever the daemon answers 401: the app swaps to the "run latet dashboard" screen. */
export function onUnauthorized(listener: AuthListener): () => void {
  authListeners.add(listener)
  return () => authListeners.delete(listener)
}

export function notifyUnauthorized(): void {
  for (const listener of authListeners) listener()
}

async function toApiError(res: Response): Promise<ApiError> {
  let message = res.statusText || `HTTP ${res.status}`
  let details: unknown[] = []
  let body: unknown
  try {
    body = await res.json()
    if (body && typeof body === 'object') {
      const { error, details: raw } = body as { error?: unknown; details?: unknown }
      if (typeof error === 'string') message = error
      else if (error && typeof error === 'object' && 'message' in error) message = String((error as { message: unknown }).message)
      if (Array.isArray(raw)) details = raw
    }
  } catch {
    // not JSON: keep the status text
  }
  return new ApiError(res.status, message, details, body)
}

export const httpTransport: Transport = {
  mock: false,
  async request(method, path, body, signal) {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (method !== 'GET') headers['X-Latet-UI'] = '1'
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    let res: Response
    try {
      res = await fetch(API_BASE + path, {
        method,
        headers,
        credentials: 'same-origin',
        cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      })
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err
      throw new NetworkError(err instanceof Error ? err.message : String(err))
    }
    if (res.status === 401) {
      notifyUnauthorized()
      throw new UnauthorizedError()
    }
    if (!res.ok) throw await toApiError(res)
    if (res.status === 204) return undefined
    return res.json()
  },
  async health(signal) {
    try {
      const res = await fetch('/_latet/health', { credentials: 'same-origin', cache: 'no-store', signal })
      return res.ok ? ((await res.json()) as Health) : {}
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err
      return {} // not reachable: nothing known of it
    }
  },
  subscribe(onExchange, onState, onReview) {
    return eventStream(
      '/stream',
      {
        exchange: (data) => onExchange(data as ExchangeSummary),
        review: (data) => onReview?.(data as ReviewRecord),
      },
      onState,
    )
  },
  subscribeApprovals(onEvent, onState) {
    return eventStream(
      '/approvals/stream',
      {
        snapshot: (data) => {
          const { batches, changes, viewers } = data as { batches: Batch[]; changes?: SettingsChange[]; viewers: number }
          onEvent({ type: 'snapshot', batches, changes: changes ?? [], viewers })
        },
        pending: (data) => onEvent({ type: 'pending', batch: data as Batch }),
        updated: (data) => onEvent({ type: 'updated', batch: data as Batch }),
        settled: (data) => {
          const { id, state } = data as { id: string; state: BatchState }
          onEvent({ type: 'settled', id, state })
        },
        change: (data) => onEvent({ type: 'change', change: data as SettingsChange }),
        change_settled: (data) => {
          const { id, state } = data as { id: string; state: ChangeState }
          onEvent({ type: 'change_settled', id, state })
        },
        viewers: (data) => onEvent({ type: 'viewers', viewers: (data as { viewers: number }).viewers }),
      },
      onState,
    )
  },
}

/**
 * An SSE stream of the API, its events given parsed to `handlers`. When the browser gives up (a 401,
 * a stopped daemon) it probes the session, then reopens with backoff (1 s, doubling up to 15 s).
 * Returns a function that closes it.
 */
function eventStream(
  path: string,
  handlers: Record<string, (data: unknown) => void>,
  onState: (state: StreamState) => void,
): () => void {
  let source: EventSource | null = null
  let closed = false
  let retry: ReturnType<typeof setTimeout> | undefined
  let delay = 1000

  const open = () => {
    if (closed) return
    onState('connecting')
    source = new EventSource(API_BASE + path, { withCredentials: false })
    source.onopen = () => {
      delay = 1000
      onState('open')
    }
    for (const [name, handle] of Object.entries(handlers)) {
      source.addEventListener(name, (event) => {
        let data: unknown
        try {
          data = JSON.parse((event as MessageEvent<string>).data)
        } catch {
          return // a malformed event is skipped; the next one still arrives
        }
        handle(data)
      })
    }
    source.onerror = () => {
      if (closed || !source) return
      if (source.readyState === EventSource.CLOSED) {
        // The browser gave up (e.g. a 401 or a stopped daemon): probe, then reopen with backoff.
        source.close()
        source = null
        onState('retrying')
        void fetch(API_BASE + '/me', { credentials: 'same-origin', cache: 'no-store' })
          .then((res) => {
            if (res.status === 401) notifyUnauthorized()
          })
          .catch(() => undefined)
        retry = setTimeout(open, delay)
        delay = Math.min(delay * 2, 15000)
      } else {
        onState('retrying')
      }
    }
  }

  open()
  return () => {
    closed = true
    if (retry) clearTimeout(retry)
    source?.close()
    onState('closed')
  }
}

let transport: Transport = httpTransport

export function setTransport(next: Transport): void {
  transport = next
}

export function isMock(): boolean {
  return transport.mock
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

const asset = (path: string) => transport.assetUrl?.(path) ?? API_BASE + path
const chapterPath = (id: string, tail = '') => `/tutorial/chapters/${encodeURIComponent(id)}${tail}`
const get = <T>(path: string, signal?: AbortSignal) => transport.request('GET', path, undefined, signal) as Promise<T>
const send = <T>(method: Method, path: string, body?: unknown) => transport.request(method, path, body) as Promise<T>

type Saved = { ok: true; rules_version: number | string }
const projectPath = (id: string, tail = '') => `/projects/${encodeURIComponent(id)}${tail}`
// A save of your settings carries the etag of the GET /config the page was made from: when they changed since (another
// tab, a change applied meanwhile), nothing is saved (409, `settings_stale`). A Latet server refuses a save without it.
const saveConfig = (config: ConfigUpdate, etag: string | undefined) => send<Saved>('PUT', '/config', { ...config, etag })
const savePersonal = (id: string, settings: PersonalSettings, etag: string | undefined) =>
  send<Saved>('PUT', projectPath(id, '/personal'), { ...settings, etag })

export const api = {
  me: (signal?: AbortSignal) => get<Me>('/me', signal),
  health: (signal?: AbortSignal) => transport.health(signal),
  /** On a Latet server: this browser's session ends, on every instance. */
  logout: () => send<{ ok: true }>('POST', '/logout'),
  exchanges: (q: ExchangeQuery = {}, signal?: AbortSignal) =>
    get<Page<ExchangeSummary>>(
      '/exchanges' +
        query({
          limit: q.limit ?? 100,
          before: q.before,
          session: q.session,
          agent: q.agent,
          flag: q.flag,
          project: q.project,
        }),
      signal,
    ),
  exchange: (id: string, signal?: AbortSignal) => get<ExchangeDetail>(`/exchanges/${encodeURIComponent(id)}`, signal),
  /** An image of a request as it was sent. */
  imageUrl: (id: string) => asset(`/images/${encodeURIComponent(id)}`),
  /** An image waiting in the Live Reviewer: as it came, or as it would leave now. */
  approvalImageUrl: (id: string, item: number, view: 'original' | 'sent' = 'original') =>
    asset(`/approvals/${encodeURIComponent(id)}/images/${item}${view === 'sent' ? '?view=sent' : ''}`),
  /** Strict data mode, guided: the columns of a tool's output (block `slot` of the exchange), each with a proposal. */
  columns: (id: string, slot: number, signal?: AbortSignal) =>
    get<OutputColumns>(`/exchanges/${encodeURIComponent(id)}/columns${query({ slot })}`, signal),
  /** The light form of `columns`, for the exchange's notice: the columns' names, strict data mode and whether it covers the tool. */
  columnShape: (id: string, slot: number, signal?: AbortSignal) =>
    get<OutputShape>(`/exchanges/${encodeURIComponent(id)}/columns${query({ slot, shape: 1 })}`, signal),
  /** The local reviewer's opinion on each of those columns (shown, never applied). Up to two minutes. */
  reviewColumns: (id: string, slot: number) =>
    send<ColumnOpinions>('POST', `/exchanges/${encodeURIComponent(id)}/columns/review${query({ slot })}`),
  /** Clears the recorded history (the vault is kept). */
  clearHistory: () => send<{ ok: true }>('DELETE', '/exchanges'),
  sessions: (limit = 50, signal?: AbortSignal) => get<{ items: Session[] }>(`/sessions${query({ limit })}`, signal),
  vault: (query: VaultQuery, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q: query.q ?? '', type: query.type ?? '', class: query.class ?? '' })
    if (query.page) params.set('page', String(query.page))
    if (query.perPage) params.set('per_page', String(query.perPage))
    return get<VaultPage>(`/vault?${params}`, signal)
  },
  reveal: (id: string) => send<{ original: string }>('POST', `/vault/${encodeURIComponent(id)}/reveal`),
  forget: (id: string) => send<{ ok: true }>('DELETE', `/vault/${encodeURIComponent(id)}`),
  config: (signal?: AbortSignal) => get<ConfigView>('/config', signal),
  /** Saves `config` over the settings read with `etag` (`ConfigView.etag`). */
  saveConfig,
  /** Some of your settings changed from what they are now: read, changed and saved with the etag of that read. */
  changeConfig: async (change: (config: ConfigView) => ConfigUpdate) => {
    const config = await get<ConfigView>('/config')
    return saveConfig(change(config), config.etag)
  },
  allow: (value: string) => send<{ ok: true }>('POST', '/allow', { value }),
  addTerm: (value: string, type: string) => send<{ ok: true }>('POST', '/terms', { value, type }),
  /** `profile`: the text masked as that profile would mask it, your settings left as they are; none: your rules. */
  playground: (text: string, kind: SlotKind, profile?: Profile) =>
    send<PlaygroundResult>('POST', '/playground', profile ? { text, kind, profile } : { text, kind }),
  testRecognizer: (recognizer: RecognizerEntry, text: string) =>
    send<RecognizerReport>('POST', '/recognizers/test', { recognizer, text }),
  draftRecognizer: (description: string) => send<RecognizerDraft>('POST', '/recognizers/draft', { description }),
  /** Your rules as a file (or your own settings for the project `project`). */
  exportRules: (project?: string) => get<RulesExport>(`/rules/export${query({ project })}`),
  /** What a rules file would do; with `apply`, done (with `all`, also what lowers protection). */
  importRules: (body: { file: string; project?: string; apply?: boolean; all?: boolean }) =>
    send<RulesImport>('POST', '/rules/import', body),
  /**
   * The reviewer tried with the settings given (not saved), on a sample text or `text`; from a project's
   * page with its guidance (`projectGuidance`) after the global one.
   */
  testReviewer: (settings?: Partial<LlmReviewSettings>, text?: string, projectGuidance?: string | null) =>
    send<ReviewerTest>('POST', '/reviewer/test', {
      ...(settings ? { llm_review: settings } : {}),
      ...(text ? { text } : {}),
      ...(projectGuidance ? { project_guidance: projectGuidance } : {}),
    }),
  /** The reviewer's own instructions, as the daemon sends them (read-only). */
  reviewerPrompt: (signal?: AbortSignal) => get<ReviewerPrompt>('/reviewer/prompt', signal),
  reviewerKey: (signal?: AbortSignal) => get<ReviewerKey>('/reviewer/key', signal),
  /** Saved in the operating system's credential store by the daemon, never in config.yaml. */
  saveReviewerKey: (key: string) => send<ReviewerKey>('PUT', '/reviewer/key', { key }),
  removeReviewerKey: () => send<ReviewerKey>('DELETE', '/reviewer/key'),
  /** The certificate the server at `url` shows now, to compare with the one on the server. */
  reviewerCertificate: (url: string) => send<ServerCertificate>('POST', '/reviewer/certificate', { url }),
  /** Keeps it as trusted when it is still the one compared; saving the settings with it trusts it. */
  trustCertificate: (url: string, fingerprint: string) =>
    send<ServerCertificate>('PUT', '/reviewer/certificate', { url, fingerprint }),
  reviews: (session?: string, limit = 200, signal?: AbortSignal) =>
    get<{ items: ReviewRecord[] }>(`/reviews${query({ session, limit })}`, signal),
  projects: (signal?: AbortSignal) => get<{ items: ProjectSummary[] }>('/projects', signal),
  project: (id: string, signal?: AbortSignal) => get<ProjectDetail>(`/projects/${encodeURIComponent(id)}`, signal),
  /** A project by its folder, before any session there. */
  addProject: (path: string) => send<ProjectDetail>('POST', '/projects', { path }),
  /** Saves your own settings for a project over the settings read with `etag` (that of `GET /config`). */
  savePersonal,
  /**
   * Your own settings for a project changed from what they are now: the settings' etag is read first, then the project,
   * so that a change landing between the two reads refuses the save (409) rather than being written over.
   */
  changePersonal: async (id: string, change: (personal: PersonalSettings) => PersonalSettings) => {
    const { etag } = await get<ConfigView>('/config')
    const project = await get<ProjectDetail>(projectPath(id))
    return savePersonal(id, change(project.personal ?? {}), etag)
  },
  removePersonal: (id: string) =>
    send<{ ok: true; rules_version: number | string }>('DELETE', `/projects/${encodeURIComponent(id)}/personal`),
  /** Writes latet.yaml; `tag` is the file as read (409 when it changed since). */
  saveRepo: (id: string, settings: ProjectSettings, tag: string) =>
    send<ProjectDetail>('PUT', `/projects/${encodeURIComponent(id)}/repo`, { settings, tag }),
  suggestProject: (id: string) => send<SuggestResult>('POST', `/projects/${encodeURIComponent(id)}/suggest`),
  stats: (days: number, signal?: AbortSignal) => get<Stats>(`/stats${query({ days })}`, signal),
  integrations: (signal?: AbortSignal) => get<Integrations>('/integrations', signal),
  /** Takes away that destination alone: 404 when there is none of that name, 409 (with the reason) while an agent still uses it. */
  removeDestination: (name: string) =>
    send<{ ok: true; rules_version: number | string }>('DELETE', `/destinations/${encodeURIComponent(name)}`),
  /** The batches waiting for your approval (works with the history off). */
  approvals: (signal?: AbortSignal) => get<{ batches: Batch[]; viewers: number }>('/approvals', signal),
  /** Approve a batch (with the values you masked in it) or stop it: 409 when it no longer waits, 422 for a mask refused. */
  decide: (id: string, decision: ApprovalDecision) => send<{ ok: true }>('POST', `/approvals/${encodeURIComponent(id)}`, decision),
  /** Every batch waiting, as Latet masked it. */
  /** Every batch waiting, or with `project` only that project's (null: those of no project). */
  approveAll: (project?: string | null) =>
    send<{ approved: number }>('POST', '/approvals/approve-all', project === undefined ? undefined : { project }),
  /** The change to your settings waiting for your approval (at most one). */
  changes: (signal?: AbortSignal) => get<{ changes: SettingsChange[] }>('/changes', signal),
  /** Approve it (it applies) or undo it (config.yaml goes back): 409 when it no longer waits or your settings changed since. */
  decideChange: (id: string, action: 'approve' | 'reject') =>
    send<{ ok: true; state: ChangeState }>('POST', `/changes/${encodeURIComponent(id)}`, { action }),
  /**
   * The tutorial's sandbox (`latet tutorial`); these routes answer 404 anywhere else. `lang`: the page's language,
   * which the texts of the chapters come in (English where they have none).
   */
  tutorial: {
    chapters: (lang: string, signal?: AbortSignal) => get<TutorialMenu>(`/tutorial/chapters${query({ lang })}`, signal),
    /** The chapter started comes in the language it runs in, whatever `lang` is. */
    chapter: (id: string, lang: string, signal?: AbortSignal) => get<TutorialChapter>(chapterPath(id) + query({ lang }), signal),
    /** Resets the sandbox to the chapter's first step, in `lang`, which the run keeps. 409 while the agent is working. */
    start: (id: string, lang: string) =>
      send<{ chapter: TutorialChapter; state: TutorialState }>('POST', chapterPath(id, '/start'), { lang }),
    /**
     * Runs a step: the agent works in the background, and the answer comes at once, `busy`, with the step's
     * first lines. Poll `state` until it is not busy (a request the Live Reviewer holds waits on you). 409 (not
     * this step, waiting, busy, not started) carries the `state` to go on from (`conflictState`; null when not
     * started).
     */
    next: (id: string, body: TutorialNext) => send<{ state: TutorialState }>('POST', chapterPath(id, '/next'), body),
    state: (id: string, signal?: AbortSignal) => get<{ state: TutorialState }>(chapterPath(id, '/state'), signal),
    /** A chapter's picture as it is on this computer: `path` is the `original` a step shows, which the daemon encoded already. */
    picture: (path: string) => asset(path),
    /** Ends the tutorial: the sandbox stops and its folder is deleted. */
    end: () => send<{ ending: true }>('POST', '/tutorial/end'),
  },
  approvalsStream: (onEvent: (event: ApprovalEvent) => void, onState: (state: StreamState) => void) =>
    transport.subscribeApprovals(onEvent, onState),
  stream: (
    onExchange: (exchange: ExchangeSummary) => void,
    onState: (state: StreamState) => void,
    onReview?: (review: ReviewRecord) => void,
  ) => transport.subscribe(onExchange, onState, onReview),
}
