# Dashboard API contract (v1)

Served by the daemon on `http://127.0.0.1:<port>`. The UI lives at `/_latet/ui/` (static files,
unknown sub-paths fall back to `index.html`). The API lives at `/_latet/api/v1/`.

## Authentication
- `latet dashboard` asks the daemon for a one-time code and opens
  `/_latet/ui/login?code=<code>`; the daemon sets the HttpOnly, SameSite=Strict cookie
  `latet_session` and redirects to `/_latet/ui/`.
- Every API call sends that cookie (same origin, `credentials: "same-origin"`). State-changing
  calls (POST/PUT/DELETE) must also send the header `X-Latet-UI: 1`.
- Without a valid session every API call answers `401 {"error":"unauthorized"}`: the UI then shows
  a screen telling the user to run `latet dashboard`.
- The CLI's own calls live under `/_latet/admin/` and need the local token (`X-Latet-Token`,
  never from a web page): `login-code`, `reload` (answers `{ok, rules_version, recognizers}`, the
  recognizers with problems), `shutdown`, `user-name`, which answers `{surrogate}`, the OS user
  name's surrogate that `latet agents apply claude` gives Claude Code with
  `auto_mode_review.user_name: surrogate`, and `known` (`{values}` → `{known: boolean[]}`: whether
  each value is in the vault, for `latet rules add` to refuse real values as examples).

## Shapes

```ts
type EntityClass = "SECRET" | "CUSTOM" | "IDENTITY" | "PII" | "NER" | "STRUCT"  // STRUCT: a value of a tool output's
                                                                          // table, JSON or key=value (strict data mode, field rules)
type Mode = "rewrite" | "verify" | "passthrough"   // rewrite: gateway mode; verify: Claude Code (client mode)
type Agent = "claude-code" | "codex" | "opencode" | "tutorial-agent" | "other"  // tutorial-agent: the tutorial's pretend agent

type ExchangeSummary = {
  id: string                 // "ex_..."
  ts: number                 // unix seconds (float)
  session: string            // "cc:<Claude Code session>", "session-id:<Codex>", "conv:<hash of the opening>"
  agent: Agent
  agent_version: string | null
  upstream: string           // "anthropic" | "openai" | "chatgpt" | "openrouter" | custom name
  path: string               // "/v1/messages"
  protocol: string | null    // "anthropic" | "chat" | "responses" | "embeddings" | null
  mode: Mode
  transport: "http" | "ws"
  model: string | null
  status: number | null      // HTTP status (101 for a WebSocket turn; 400 for one Latet stopped, as over HTTP)
  latency_ms: number | null  // whole exchange
  overhead_ms: number | null // time Latet added before forwarding
  tokens: { input: number; output: number; cache_read: number; cache_write: number } | null
  masked: number             // values replaced in this request
  new_masked: number         // of which in blocks never sent before in this session
  by_type: Record<string, number>   // e.g. {"EMAIL": 1, "PASSWORD": 2}
  new_by_type: Record<string, number> // the same, counting only blocks new to the session
  by_class: Record<EntityClass, number> // values masked in this request, by class
  by_module: Record<Module, number> // the same, by the module that found them (missing in older records)
  restored: number           // values restored in the response
  flags: string[]            // "blocked", "leak", "uninspected:image", "degraded:pii", "unknown-route",
                             // "withheld" (the dump guard kept a tool output from the model)
                             // "outbound:<tool>" (a tool outside this computer got a stand-in: stopped
                             // by the Claude Code plugin, or run by the provider; its input is an
                             // "outbound_tool" slot)
  verified: boolean          // the egress check ran and found nothing in clear
  block_reason: string | null // why a blocked request was stopped, as told to the agent (types and lengths only)
  project: { id: string; name: string } | null // the session's project (flag "project-off": Latet is off there)
}

type Replacement = {
  start: number; end: number                 // offsets in `original`
  masked_start: number; masked_end: number   // offsets in `masked`
  type: string; class: EntityClass
  rule: string | null  // the detector rule that found the value; for a value found again (via other
                       // than "detector"), the rule of its first sighting; null when unknown (a vault
                       // from before rules were kept)
  module: Module       // "secrets" (gitleaks), "secret-structure" (assignments, private keys, credentials),
                       // "personal", "ids" (national ID and VAT numbers), "identity", "terms",
                       // "recognizers", "names" (name model), "reviewer" (local model), "fields"
                       // (your field rules), "data" (strict data mode), "other"
  via: "detector" | "known" | "repeat" | "echo" | "plugin" | "restore"
                       // how it was found in this text: by a detector; a value already in the vault;
                       // another occurrence in the same text; a surrogate the client sent back; masked
                       // by the Claude Code plugin before the daemon saw it; restored in the answer
  entity: string | null                      // vault entity id (null for one-way redaction)
  known: boolean       // false: the vault no longer holds it (forgotten, or redacted): original shows the masked text
  json?: true          // found in its JSON-escaped form (inside a JSON document)
}

type Slot = {
  block: string              // the block's id in the history (a leak's `block` names it)
  path: string               // JSON path in the request body, e.g. "$.messages[3].content[0].text"
  kind: "system" | "user_text" | "assistant_text" | "tool_input" | "tool_result" | "attachment" | "tool_description" | "outbound_tool" | "other"
  from_model: boolean
  new: boolean               // first time this block left in this session
  original: string           // rebuilt from the vault when shown: nothing original is stored with the history
  masked: string             // exactly what was sent upstream
  replacements: Replacement[]
}

type ExchangeDetail = {
  summary: ExchangeSummary
  request: { headers: [string, string][]; slots: Slot[]; body_bytes: number }
  response: {
    headers: [string, string][]
    text_masked: string      // the model's answer as the model wrote it (surrogates)
    text_restored: string    // as the agent received it
    text_spans: Replacement[] // masked_* offsets in text_masked, start/end in text_restored
    tool_calls: { name: string; args_masked: string; args_restored: string; spans: Replacement[] }[]
  } | null
  leaks: Leak[]
}

// A sensitive value the final check found in clear in a request that is only checked (every Claude Code
// request, as the plugin masks it first; one signed with a Claude subscription; Bedrock's and Vertex's; the
// plugin's own final check). Never the value itself.
type Leak = {
  type: string; kind: string; preview: string  // preview: type and length, e.g. "GITHUB_PAT[40]"
  new: boolean               // in a block new to the session: it stopped the request (else only flagged)
  // detail and allowed: auto mode reviews only. allowed: let through to the reviewer by the rules
  // (then not a block, and not a "leak" flag on its own).
  detail?: ReviewDetail; allowed?: boolean
  // Where the value is, when the history keeps texts (history: full): the block it is in and its
  // place in it, so the page can mark it for Not sensitive. The value is in that block's text, as it
  // would have been sent. Missing with history: metadata, and in records from before this.
  block?: string             // the id of the block it is in: the Slot with the same `block`
  masked_start?: number; masked_end?: number  // offsets in that slot's `masked`, as sent (code points)
  start?: number; end?: number                // offsets in that slot's `original` (missing when the block is gone)
  class?: EntityClass; rule?: string          // what it is, and the rule that found it
  // Leaks can overlap: two rules read a PEM private key, whole and by its body inside. Each is its own
  // entry. The page marks overlapping ones once, over their union, and Not sensitive allows the text of each.
}

type ReviewDetail = "user_name" | "paths" | "host_name" | "git_author"

type Session = { key: string; agent: Agent; first_ts: number; last_ts: number; exchanges: number; masked: number; blocked: number }

type VaultEntity = { id: string; type: string; class: EntityClass; surrogate: string; created: number; sessions: number }
```

## Endpoints
| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/me` | | `{version, profile, port, data_dir, recording, tutorial, server}` (`tutorial`: null but in the tutorial's sandbox, below; `server`: null but on a Latet server, below) |
| GET | `/exchanges?limit=100&before=<id>&session=<key>&agent=<agent>&flag=<flag>&project=<id>` | | `{items: ExchangeSummary[], next: string \| null}` |
| GET | `/exchanges/{id}` | | `ExchangeDetail` |
| DELETE | `/exchanges` | | `{ok: true}` (clears the recorded history; the vault is kept) |
| GET | `/sessions?limit=50` | | `{items: Session[]}` |
| GET | `/vault?q=&type=&class=&page=1&per_page=50` | | `{items: VaultEntity[], total, page, pages, per_page, types: {TYPE: count}}` (no originals; newest first, then by id; `per_page` 10-200; a page past the last is the last; `types` counts the whole vault) |
| POST | `/vault/{id}/reveal` | | `{original}` |
| DELETE | `/vault/{id}` | | `{ok: true}` |
| GET | `/config` | | `ConfigView` (below), with `etag` |
| PUT | `/config` | `ConfigView` (partial ok), and the `etag` GET answered | `{ok: true, rules_version}`; 409 `{error, reason: "settings_stale"}` when the settings in use changed since that `etag` was read (another tab saved, a change applied since): nothing is saved, the page reads them again. Without `etag`, as before on a computer, and 409 `settings_stale` on a Latet server (below); an `etag` that is not a text: 400 |
| POST | `/allow` | `{value}` | `{ok: true}` (adds to allow_values) |
| POST | `/terms` | `{value, type}` | `{ok: true}` (adds a custom term) |
| POST | `/playground` | `{text, kind}` | `{masked, replacements: Replacement[]}` (nothing is stored) |
| POST | `/recognizers/test` | `{recognizer, text}` | What `latet rules test` reports for one recognizer (`{name, state, errors, warnings, type, class, examples[], stdlib}`), and `try: {found, reason, spans}` for `text`; nothing is saved |
| GET | `/projects` | | `{items: ProjectSummary[]}`: projects seen, and those you have settings for |
| POST | `/projects` | `{path}` | `ProjectDetail` (a project by its folder, before any session there) |
| GET | `/projects/{id}` | | `ProjectDetail` |
| PUT | `/projects/{id}/personal` | your settings (`ProjectSettings` keys and `enabled`), and the `etag` of `GET /config` | `{ok, rules_version}`; saved in config.yaml; 409 `settings_stale` as `PUT /config` |
| DELETE | `/projects/{id}/personal` | | `{ok, rules_version}` |
| PUT | `/projects/{id}/repo` | `{settings, tag}` | `ProjectDetail`; writes latet.yaml: only the keys a repository file may set (400 otherwise), 409 when `tag` is not the file's current one, 403 in the container |
| POST | `/projects/{id}/suggest` | | `{files, shapes, drafts}`, as `latet rules suggest` (never a value) |
| GET | `/stats?days=7` | | `Stats` (below) |
| GET | `/integrations` | | `{checks: {status: "ok"\|"warn"\|"fail", title, detail}[], agents: {id, name, installed, version, status, notes}[]}` |
| DELETE | `/destinations/{name}` | | `{ok: true, rules_version}`: that destination alone (Company endpoints, below); 404 when there is none, 409 while it is in use |
| GET | `/stream` | | Server-sent events: `event: exchange`, `data: ExchangeSummary` |

```ts
type ConfigView = {
  profile: "balanced" | "max" | "secrets"
  pii: string[] | null             // enabled PII types (null = profile default)
  pii_available: string[]          // every PII type Latet knows
  pii_catalog: { type: string; country: string | null }[]  // read-only: display order, country of each
  pii_base: string[]               // read-only: types of no country, on by default
  pii_effective: string[]          // read-only: the types in use with the saved rules
  id_regions: string[]             // countries whose ID and tax numbers are recognised (max: all)
  id_regions_available: string[]   // read-only
  phone_regions: string[]
  include_private_ips: boolean | null
  terms: { value: string; type: string; case_sensitive: boolean; whole_word: boolean }[]
  // The user's own recognizers, exactly as in config.yaml (broken ones included); PUT answers 400
  // with details when one would not load. The schema: `latet config schema`.
  recognizers: Record<string, unknown>[]
  recognizer_status: { name: string; state: "active" | "off" | "error"; errors: string[]; warnings: string[] }[]
  checksums: { name: string; description: string }[]  // read-only: what `checksum:` may name
  allow_values: string[]
  allow_patterns: string[]
  policies: Record<string, "mask" | "redact" | "allow">
  identity: { enabled: boolean; usernames: string[]; hostname: string | null; hostnames: string[]; full_name: string | null; emails: string[] }
  history: "full" | "metadata" | "off"   // what is kept of each exchange (metadata: no text)
  retention_days: number                  // 1-365
  ner: "auto" | "off"                     // local name and address detection
  ner_status: { runtime: boolean; model: boolean; active: boolean }  // read-only: the `ner` extra,
                                          // the downloaded model (`latet ner install`), in use now
  ocr: "auto" | "off"                     // reading the text in images
  ocr_status: { runtime: boolean; model: boolean; active: boolean }  // read-only: the `ocr` extra, the models
                                          // (`latet ocr install`) in the data folder, or a reader the daemon holds that
                                          // works (the tutorial's sandbox reads with the models of your own folder, not
                                          // its own), in use now (a reader that works, and `ocr: auto`)
  // Claude Code's auto mode reviewer: what of this machine's identity may reach it. Always the
  // object here; PUT also takes "block" or "allow-identity" (every detail at once), and config.yaml
  // keeps those short forms when the choices are all one way.
  auto_mode_review: {
    user_name: "block" | "allow" | "surrogate"  // surrogate: Claude Code gets the user name's surrogate
    paths: "block" | "allow"
    host_name: "block" | "allow"
    git_author: "block" | "allow"
  }
  // Production data: strict data mode, the tools it covers, field rules, the dump guard, images.
  data: "normal" | "strict" | null        // null: not set, the profile decides (max: strict); PUT null unsets it
  data_effective: "normal" | "strict"     // read-only: as it applies, the profile's when `data` is null
  data_tools: string[]                    // `*` patterns; empty: every tool but those that read files
  fields: { name: string; type?: string; mask?: boolean; secret?: boolean }[]  // defaults left out
  dump_guard: number | null               // at most this many personal values in one tool output (0/null: no limit)
  images: "allow" | "block"
  etag: string                            // the settings in use as this answer read them: a save sends it back
}

type Stats = {
  by_day: { day: string; exchanges: number; masked: number; blocked: number }[]
  by_type: Record<string, number>
  by_agent: Record<string, number>
  latency_ms: { p50: number | null; p95: number | null }
  overhead_ms: { p50: number | null; p95: number | null }
  cache_read_ratio: number | null   // cache_read / (input + cache_read + cache_write)
  partial?: true                    // the period holds more exchanges than one read counts: the latest `latest`
  latest?: number                   //   of them (100,000) are counted
}
```

## Storage notes
- Only masked text is stored (`exchanges.db`, AES-GCM sealed, zstd). Originals are rebuilt from the
  vault when a block is shown, so forgetting a vault value removes it from the whole history.
- Blocks are content-addressed by their masked text: history resent every turn is stored once.
- Recording runs on its own thread after the exchange ends; it never delays or breaks traffic.
- Retention: 14 days (exchanges, and blocks no newer exchange carried).

## Projects

```ts
type ProjectSummary = {
  id: string; name: string; root: string     // the project's folder
  repo_file: string | null                   // its latet.yaml
  has_personal: boolean; enabled: boolean     // your settings for it; false: Latet is off there
  profile: Profile; notice_count: number; last_seen: number
}
type ProjectDetail = ProjectSummary & {
  repo: { path: string | null; exists: boolean; settings: ProjectSettings; tag: string; writable: boolean }
  personal: (ProjectSettings & { enabled: boolean }) | null
  notices: { source: "repo" | "personal"; key: string; level: "ignored" | "error"; message: string }[]
  effective: { profile; pii: string[]; id_regions; phone_regions; include_private_ips; ner;
               terms: number; recognizers: number; allow_values: number; allow_patterns: number; policies;
               data; data_tools; fields; dump_guard; images }  // data: as it applies there, max's when unset
  recognizer_status: RecognizerStatus[]      // of the project's rules (empty: the global ones apply)
}
// ProjectSettings: the masking keys of config.yaml (profile, pii, id_regions, phone_regions,
// include_private_ips, ner, terms, recognizers, allow_values, allow_patterns, policies,
// disabled_secret_rules, data, data_tools, fields, dump_guard, images). A repository file may set
// profile to policies, data, fields, dump_guard and images, and only to add protection.
```

## Local reviewer

```ts
// GET /config and PUT /config carry `llm_review` (config.yaml's, see the configuration reference).
type ReviewRecord = {          // GET /reviews?session=&limit=, and live as `event: review` on /stream
  id: string; ts: number; session: string; project: string | null
  block: string                // the block's id: ExchangeDetail slots carry `block` too
  kind: SlotKind; inline: boolean // inline: read before the request left
  findings: { category: string; masked: boolean; allowed: boolean; entity: string | null; at: [number, number] | null }[]
                               // allowed: left in clear because you marked the value Not sensitive
                               // `at`: code-point offsets in the block's masked text (the surrogate of a
                               // masked value, the value itself otherwise); never a value
  failed: string | null        // timeout | unreachable | unreadable | suspect | busy
  block_asked: boolean; seconds: number
}
// POST /reviewer/certificate {url} → {fingerprint, readable}: the certificate the https server at
//   url shows now, to compare (502 when it cannot be reached). PUT {url, fingerprint} keeps it as
//   trusted when it is still that one (409 otherwise); saving llm_review.certificate trusts it.
// POST /reviewer/test {llm_review?: partial settings, text?, project_guidance?} → {ok, error, models,
//   model, slots, n_ctx, seconds, text, view, findings: [{text, category, masked}], block}  (nothing is
//   kept). project_guidance: a project's guidance, read after the settings' own (a project's page).
// GET /reviewer/prompt → {system, intro, version, max_guidance}: the reviewer's own instructions, the
//   very text the daemon sends; your guidance follows them (`intro`, a newline, the guidance), from
//   llm_review.guidance and a project's personal `review_guidance` (at most max_guidance characters each).
// POST /recognizers/draft {description} → {draft: RecognizerEntry, report: RecognizerReport}
//   409 when the reviewer is off; 502 when its server does not answer
```

## Rules in a file (src/latet/transfer.py)

```ts
// GET /rules/export[?project=id] → {name, file, warnings}: your rules (or your own settings for a
//   project) as YAML, for a colleague; recognizers whose examples the vault knows are left out.
type RulesReport = {added: string[]; skipped: string[]; held: string[]; notices: string[]}  // sentences
// POST /rules/import {file, project?, apply?, all?} → {report: RulesReport, applied, rules_version?}
//   apply false: what it would do. apply true: applies added changes; all also accepts the imported
//   held changes. Neither choice approves an existing pending shared-change proposal.
//   On a server, a kept import whose pending proposal could not be refreshed answers 409
//   {error, reason: "settings_saved_change_pending", saved: true}; reload without replaying the import.
//   422 when the file cannot be read (newer format, not YAML, aliases).
// Every dashboard request body is read up to 8 MB, no further.
```

## Live Reviewer

```ts
// GET /config and PUT /config carry `live_review`: {enabled, kinds: SlotKind[], auto_approve_clean,
// timeout_s: number | null}. A project's personal settings may carry `live_review: boolean | null`.
type LiveItem = {              // one new text of a batch
  kind: SlotKind; path: string | null; tool: string | null
  original: string             // rebuilt from the vault, as on the Exchange page
  masked: string               // what leaves when you approve it as it is
  replacements: Replacement[]  // offsets on both sides (code points), like an ExchangeDetail slot
  suggestions: { start: number; end: number; category: string }[]
                               // what the local reviewer found and did not mask (offsets in `original`)
  failed: string | null        // the local reviewer could not read it: you decide
}
type Batch = {                 // what one request (gateway) or one plugin call brings that is new
  id: string; session: string; project: string | null
  agent: 'claude-code' | 'codex' | 'opencode' | 'other'
  created: number; expires: number | null   // epoch seconds; expires: live_review.timeout_s
  items: LiveItem[]
}
// GET /approvals → {batches: Batch[], viewers: number}       (oldest first; works with history off)
// GET /approvals/stream (SSE): `snapshot` {batches, viewers} first, then `pending` Batch,
//   `settled` {id, state: approved | rejected | abandoned | expired}, `viewers` {viewers}.
//   Each open stream counts as a dashboard watching (the agent tells you when none is open).
// POST /approvals/{id} {action: 'approve' | 'reject', masks?: [{item, start, end, category}]}
//   masks: offsets in that item's `original`; category one of the local reviewer's categories.
//   → {ok: true}; 409 when it no longer waits; 422 when a mask would not add protection (over a
//   value masked already, a fake in use, spaces around it, fewer than two characters).
// POST /approvals/approve-all {project?: string | null} → {approved: number}
//   every batch waiting, as Latet masked it; with `project`, only that project's (null: no project)
// A value you mask is masked with a fake from the vault (module `live`, rule `human:<category>`,
// class CUSTOM; SECRET for `secret`): from then on wherever it appears, and the final check stops
// it in clear.
```

## Changes to your settings that lower protection

```ts
// A change that lowers protection (see src/latet/protection.py) does not apply until you approve
// it: one made by an `latet` command an agent ran, config.yaml edited by hand then reloaded, or
// found when the daemon started (it runs with the settings you approved last until then).
type SettingsChange = {
  id: string; created: number                 // epoch seconds
  source: 'command' | 'file' | 'start'        // a latet command; config.yaml edited; found at start
  agent: string | null                        // the agent that ran the command (hermes, claude-code...)
  loosened: string[]                          // what it lowers, one sentence each, never a value
  keys: string[]                              // the top-level config.yaml keys it changes
  guidance: {where: string | null; before: string | null; after: string | null}[]
                                              // the reviewer's guidance it writes (where: null global,
                                              // else the project's name); the dashboard only, not the CLI
  destinations: {name: string; before: string | null; after: string | null}[]
                                              // destinations it moves (after: the new address) or
                                              // removes (after: null): where requests and keys would go
}
// At most one waits: a newer change replaces it (`change_settled` with state superseded).
// GET /changes → {changes: SettingsChange[]}
// POST /changes/{id} {action: 'approve' | 'reject'} → {ok: true, state}
//   approve: it applies and is saved; reject: config.yaml goes back to the settings in use.
//   409 {error} when it no longer waits, or when your settings changed since (state stale: dropped).
// On GET /approvals/stream: the snapshot carries `changes` too; events `change` (SettingsChange)
// and `change_settled` {id, state: approved | rejected | stale | superseded}.
```

## Company endpoints (src/latet/integrations/routes.py, gateway/cloud.py)

```ts
// GET /integrations: each agent's route, and your destinations (config.yaml `upstreams`).
type Integrations = {
  checks: Check[]
  agents: (AgentIntegration & {route: {summary: string | null; problem: string | null; notes: string[]} | null})[]
  destinations: {name: string; url: string; agent: 'claude' | 'codex' | null
                 last_used: number | null   // epoch seconds of the last exchange through it
                 in_use: boolean}[]          // Claude Code or Codex uses it (an unused one may be removed): its
                                             // settings point at it (Claude Code's, the managed ones, Codex's
                                             // files; the address of Latet on 127.0.0.1, localhost or [::1], the
                                             // name read whole), or it was registered since the daemon started
                                             // (latet claude, latet codex, agents apply, global-install);
                                             // another agent's settings (OpenCode...) are not read
}
// PUT /config {upstreams: Destination[]}: from the dashboard, a destination moved applies at once.
// DELETE /destinations/{name}: takes away that destination alone, from the settings as they are now (one
//   added since the page was loaded stays) → {ok: true, rules_version}. 404 {error} when there is none of
//   that name; 409 {error} while it is in use (the same test as `in_use`, made under the settings' lock;
//   the reason is plain words, for a toast).
// ExchangeSummary gains `host` (where it went) and `signed` ('aws-profile:<name>' | 'aws-session' | null).
// Flag `route-bypass`: the plugin found a Claude Code session sending around Latet (its prompts stopped).
```

With the local token (`x-latet-token`), not the dashboard's session:
- `POST /_latet/admin/destinations {destinations: [{url, agent}]}` → `{names}`: the wrappers and
  `global-install` add the destinations an agent's settings name (an address already known keeps its
  destination; in use at once). A change waiting for you is proposed again with them in it (but one
  whose address it has, or whose name it gives another address: that move is what you must see): a
  new id, its `loosened`, `keys`, `guidance` and `destinations` weighed against the settings now in
  use, the same source and agent; the one before is settled as superseded. When it no longer lowers
  anything, it applies and is saved instead (the one before superseded too). Each name it answers, new or
  known, is kept in memory (`Runtime.wrapped`): that destination is `in_use` until the daemon restarts.
- `POST /_latet/engine/v1/route-check {bridge, session, env, sources, entrypoint, surface}` →
  `{ok, reason, toast}`: the plugin, at the start of a Claude Code session (or at its first prompt,
  when the daemon was away). `sources` also names where CLAUDE_CODE_ENTRYPOINT,
  ANTHROPIC_FOUNDRY_RESOURCE and the proxy variables are set, never their values. Facts that cannot be
  read answer `ok: false`; the plugin stops prompts on any answer but a verdict, except a 404 (an
  older daemon).
- `POST /_latet/engine/v1/aws-sessions {access_key, secret_key, token?, expires?}` → `{id}`, and
  `DELETE /_latet/engine/v1/aws-sessions/{id}` → `{dropped}`: AWS credentials found only in
  `latet claude`'s shell, kept in memory for its session's Bedrock route. `expires` (epoch seconds) is
  the time in the shell's `AWS_SESSION_EXPIRATION` or `AWS_CREDENTIAL_EXPIRATION` (ISO 8601, the earlier
  one when both are set; a value that is not a time is ignored): from then on the session is gone.
- `GET /_latet/engine/v1/aws-sessions/{id}/hold`: the session lives as long as this request stays open.
  The answer is a newline now and every 15 s while the credentials last (`SESSION_BEAT`), and it ends
  when they do. When the client goes away (`latet claude` ended, or was killed: the operating system
  closes its connection) the daemon drops the session, as soon as it sees the connection close. 401
  without the token; 404 `{error}` when there is no such session. An older daemon has no such route (it
  answers 404 as for any path it has none for): `latet claude` then goes on without a hold, and the
  session lasts until its `DELETE` or the daemon restarts. The client sends nothing after its request
  and never times out reading: a computer that sleeps does not end a session. The id of a session is
  never in a log line (the access log shows `***`).

## Strict data mode, guided (src/latet/columns.py, engine/reviewer/columns.py)

```ts
// GET /exchanges/{id}/columns?slot=N: the columns of a tool's output (slot N of the exchange), each with
//   a proposal, worked out from the original the history keeps (nothing is stored, nothing applies).
//   409 when history is metadata or off; 404 when the slot is not a tool's output.
type Columns = {
  columns: {
    name: string               // a table's column, a JSON path (items[].sku), a key=value key
    masked: number             // how many of its values strict data mode masked in this output
    proposal: 'keep' | 'mask' | 'ask'
    reason: string             // keep (only when its name says so): dates | ids | states | flags | counts
                               // mask: secret-name | secret | people | person | sensitive | free-text | birth-dates
                               // ask: amounts | dates-unclear | flags-unclear | repeats | unclear
    detail: string | null      // what was found (EMAIL), or the states seen
    kept_now: boolean          // a rule in effect there keeps it in clear already
    everywhere: boolean        // a rule for everywhere would change it there: false when a rule of the
                               // project's own (your settings for it, its repository file) matches first
    examples: string[]         // up to three of its values, in clear
  }[]
  strict: boolean              // strict data mode is on where the exchange was (its project, or global)
  applies: boolean             // strict data mode covers this tool (not a file read, nor a web page
                               // `data_tools` does not name, nor a tool `data_tools` leaves out)
  project: {id: string; name: string; loaded: boolean} | null  // loaded: false after a restart, until
                               // the project's next session (only "Everywhere" can be chosen then)
  reviewer: boolean            // the local reviewer is on: its opinion can be asked
}
// GET /exchanges/{id}/columns?slot=N&shape=1 → {columns: string[], strict: boolean, applies: boolean}:
//   the light answer the exchange's notice asks: the columns' names alone, with no proposal (no detector
//   runs, name detection included), and none when `applies` is false (the output is not read for them).
//   409 and 404 as above.
// POST /exchanges/{id}/columns/review?slot=N → {opinions: {[column]: {keep: boolean, why: string}}}:
//   the local reviewer's opinion, shown beside the proposals, never applied. 409 when the reviewer is
//   off (or as GET); 502 when its server fails or its answer cannot be read. Up to 120 s.
```

## Tutorial (src/latet/tutorial/: sandbox.py, session.py, api.py)

`latet tutorial` runs a sandbox: Latet with a folder, settings, vault and history of its own, the fixed
root key of `sandbox.ROOT_KEY` (the same fakes at every run), and one route, `/anthropic`, to a pretend model
reached in-process. Its pretend agent, Tutorial Agent (`tutorial-agent`), sends its requests through the real gateway.
The routes below exist in the sandbox only (a real daemon answers 404), guarded like the rest of the API.

- The sandbox's cookie is `latet_tutorial_<port>`, not `latet_session`: a cookie of 127.0.0.1 goes to every port,
  and the two sign-ins never take each other's place.
- `GET /_latet/health` has `"tutorial": true` (the sign-in page then says to start `latet tutorial` again).
- The error of a request the gateway stopped ends "see where it was in Latet's pane", where a real daemon's says
  to run `latet dashboard` (in a second terminal that opens the user's own dashboard).
- `GET /me` has `tutorial: {current: {chapter, number, title, step} | null, reviewer: boolean}`: the chapter in
  progress (its title in the language it runs in), until its last step ran (then it is done: `null`);
  `reviewer` is true while the sandbox's settings turn the local reviewer on: in a chapter with the Tutorial Reviewer
  (a pretend reviewer, reached in-process like the Tutorial Model), until another chapter starts. `reviewer: false`
  hides the local reviewer's controls, which the sandbox refuses (the Rules page says the sandbox has none, and the
  columns panel does not offer to ask it); with it on, the reader may change the reviewer's guidance only (the 409
  below). With `tutorial` set, the Rules page has no identity switch (the sandbox has nobody's identity); without it,
  on a real daemon, the page's `/tutorial` routes say to run `latet tutorial`.
- Refused with 409 `{"error": "not in the tutorial's sandbox: <what>"}`: `PUT /config` when it changes
  `upstreams` or `identity`, turns `llm_review` on, or, while it is on (the Tutorial Reviewer), changes any of its
  settings but `guidance` (the Rules page's whole draft saves: only a change is refused),
  `POST /reviewer/test`, `PUT|DELETE /reviewer/key`, `POST|PUT /reviewer/certificate`, `POST /recognizers/draft`,
  `POST /exchanges/{id}/columns/review`, `PUT /projects/{id}/repo`, and the CLI's
  `POST /_latet/admin/destinations`. The Live Reviewer and the changes waiting for approval are real.
- `GET /integrations` answers the sandbox's own view (Tutorial Agent, no destination): no agent of the computer is run
  or read.
- The page opens a chapter (`/tutorial/<id>`) as it stands: `GET state`, or `start` when that chapter is not the
  one in progress (409 "not started"). A card that is done leads to `/tutorial/<id>?restart` (Start again): the
  page calls `start` without reading the state, which is still the end of the run that finished, and takes
  `?restart` out of the address once the new run is open, so that a reload does not start it again.

```ts
// The page's language, `lang` (en, it, de, fr, es): the texts of the menu and of a chapter come in it, from the
// chapter's file for that language (`chapters/<lang>/<chapter>.yaml`); English where it has none, and for a language
// the tutorial has no texts in, or none given. A chapter runs in the language it started in.
// GET /tutorial/chapters?lang=it → {chapters: Card[], requested: string | null, current: {chapter, step} | null}
//   chapters: in the page's language.
//   requested: the chapter named on the command line, given once.
//   current: the chapter in progress, until its last step ran: then its card says `done` and `current` is null.
//   A chapter done and begun again (`start`) is in progress, and done still.
//   A card's `missing`: what the chapter needs of this computer that the sandbox does not have yet, `["ocr"]` until
//   it finds a reader of your text recognition models (the `ocr` extra's runtime and the models `latet ocr install`
//   puts in your data folder: without either, they are missing), [] for a chapter that needs nothing or has it. The
//   sandbox looks again at each call (the models may be installed while the tutorial runs), and keeps the reader
//   once it has found it; a reader whose models failed to load counts as missing, and a look that fails (a broken
//   `ocr` extra) finds none: it is said in the sandbox's log, and the menu answers all the same. A chapter's view
//   (below) has `missing` too.
// GET /tutorial/chapters/{id}?lang=it → Chapter, in the page's language; the chapter started (the last `start`) in
//   the language it runs in, whatever the page asks, as its state is. 404 {error} when there is none (or it is not
//   written yet)
// POST /tutorial/chapters/{id}/start {lang?: string} → {chapter: Chapter, state: State}: the sandbox back to the
//   chapter's beginning (approvals let go, vault and history emptied, what the local reviewer read forgotten, so that
//   it reads its texts afresh, the agent's folder and the settings as the chapter has them, a new conversation), in
//   `lang` (the page's language), which the run keeps until the next start: its state, its chapter and `/me`'s title
//   speak it. A request the agent waits on in the Live Reviewer is not sent; 409 {error: "busy", state} while a step
//   is otherwise at work; 409 {error: "needs ocr", state} for a chapter whose `missing` is not empty (nothing is
//   reset; `state` is that of the chapter in progress, null when there is none); 400 when `lang` is not a text.
// POST /tutorial/chapters/{id}/next {step: number, seen?: ["pin"]} → {state}: runs that step in the background
//   and answers at once (busy: poll GET state until it is not). 409 {error, state}: "not started" (state null),
//   "not this step" (another tab, a second Enter), "waiting" (a `do` step's action not done yet), "busy".
//   400 for another body.
// GET /tutorial/chapters/{id}/state → {state}; 409 {error: "not started", state: null} for a chapter not in progress
// GET /tutorial/chapters/{id}/pictures/{path} → a picture of that chapter's project (its `pictures`), as it is on this
//   computer: the package's own bytes, of the type its path says (image/png, image/jpeg, image/gif, image/webp), with
//   `X-Content-Type-Options: nosniff` and `Cache-Control: private, max-age=3600`. 404 {error} for anything else: a
//   path that is not one of the chapter's pictures (`..` or `%2F` in it included), a chapter not written.
// POST /tutorial/end → {ending: true}: what the agent waits on is let go, and the server stops.
type Card = { id: string; number: number; title: string; minutes: number; learn: string[]; phase: 1 | 2; done: boolean
              missing: 'ocr'[] }  // what the chapter needs that the sandbox lacks now (the chapter's view has it too)
type Chapter = Card & {
  agent: { name: string; model: string }  // "Tutorial Agent", "Tutorial Model"
  steps: { index: number; kind: 'watch' | 'do' | 'read'; title: string; prompt: string | null
           button: string | null; wait: { kind: WaitKind; hint: string; text?: string } | null
                                    // text: with `guidance`, the sentence written in for the reader (in the
                                    // chapter's language): the page's editor starts with it, to be saved as it is
           resumed: PaneLine[] }[]  // the conversation the step resumes from before Latet was on, as the pane
                                    // shows it: on screen from the start of the step, as /resume shows it, and the
                                    // very lines `lines` holds once it ran; [] for a step that resumes none
}
type WaitKind = 'pin' | 'term' | 'allow' | 'approve' | 'change' | 'fields' | 'guidance'
// How each is met: pin, the page saw the value opened (it sends seen: ["pin"]); term, a term of the rules
// finds the value; allow, the value is marked Not sensitive; change, the last change the agent asked for
// (`latet config set`) that changes the setting named is decided, approved or not; fields, the columns
// named are kept in clear (by the first field rule that names each: mask: false); guidance, the local
// reviewer's `guidance` is saved (`PUT /config`): the sentence written in, or the reader's own words, any
// guidance at all (an emptied one is none); approve, the Live Reviewer's batch that holds the value is decided.
// An `approve` is met while the step runs (its own request waits): its `next` comes first, and the state stays
// busy until the user decides; a batch of an earlier step, or of a run Start over or the end let go, never meets
// it. Every other wait is met before `next`.
type State = {
  chapter: string; run: string        // run: changes at each start (another tab can tell)
  next: number                        // the step that runs next (the number of steps once finished)
  busy: boolean; finished: boolean
  lines: PaneLine[]                   // the agent's pane; while busy, the step's lines so far
  steps: { index: number; ran: boolean; explain: string; shown: Shown | null }[]
                                      // a `do` step has its explain and shown before it runs (what to act
                                      // on); a step to watch has "" and null until it ran
  waiting: { step: number; kind: WaitKind; satisfied: boolean } | null  // pin: satisfied stays false
  end: { learned: string[]; where: { label: string; to: string }[]; next: string | null } | null
                                      // to: a dashboard path (/live, /exchanges/<id>, /rules#terms)
  error: string | null                // the sandbox's own trouble (a step that could not run, a record late,
                                      // a request meant to be stopped that reached the model)
  error_step: number | null           // the step the error is about: the one `next` ran last (null with no
                                      // error). The error stays until the next `next`, so a step that ran
                                      // with it is followed by one that has none: show it at that step only
}
type PaneLine = { step: number } & (
  | { type: 'prompt' | 'agent' | 'model' | 'error' | 'system'; text: string; resumed?: true }  // resumed: said in a
  //   conversation from before Latet was on, which the step resumes (after a `system` line that says so)
  | { type: 'tool'; name: 'Read' | 'Write' | 'Bash'; summary: string; output?: string; pending?: boolean }
  | { type: 'file'; path: string; content: string })   // pending: left for the next step (false once it ran)
type Shown = {
  component: 'exchange' | 'why' | 'terms' | 'command' | 'approvals' | 'changes' | 'columns' | 'review' | 'guidance'
           | 'images' | 'none'   // review: an exchange (`part: request`) with the Local reviewer panel of its block
                                 // (`slot`: the block the values are in); guidance: Rules → Reviewers → Local
                                 // reviewer → Your guidance, its editor starting with `text`; images: an exchange (`part: request`)
                                 // with its images, as they were and as they were sent, and the text read in them
                                 // (`slot` as for `exchange`; with a reader, the text read in a picture is a block of
                                 // its own (kind `image_text`), so `slot` names it when a value is in the picture;
                                 // `original`: the picture as it is here)
  exchange?: string                   // an exchange id: GET /exchanges/{id} is what the page renders
  part?: 'request' | 'response'
  slot?: number    // request: the index in request.slots; response: the tool call's index, -1 the text;
                   // columns: the tool output with values strict data mode masked
  highlight?: string[]                // the original (invented) values to point at
  original?: string                   // images: the chapter's picture as it is on this computer, by the route that
                                      // serves it, under the API (/tutorial/chapters/<id>/pictures/<path>, the path
                                      // percent-encoded: ask for it as it is): the page shows it above the image as
                                      // it was sent
  text?: string                       // guidance: the sentence the editor starts with while the step waits (the
                                      // wait's `text`); once guidance is saved, the page shows the saved one
                                      // (GET /config)
  focus?: 'values' | 'block' | 'why' | 'terms'; command?: string; snippet?: string; rules?: string
}
// A step whose request was stopped before the answer its view names (the user did not send it) shows
// {component: 'why', exchange, part: 'request', focus: 'why'}; when its explanation names what the model would
// have received, it says instead that the request was not sent. A step whose request waited in the Live Reviewer
// says how it ended in its chapter's words (`otherwise`): not sent, approved with the value to mask in clear, or
// with a word of it in clear (part of it masked by hand). At or after a step that waits for a change to the
// settings, a step says what the chapter has for the change approved instead of undone (`otherwise.approved`, and
// the model a turn's `approved`).
```

The agent's Read sends a picture as the image itself, as Claude Code's Read does: a file whose name ends `.png`,
`.jpg`, `.jpeg`, `.gif` or `.webp` goes in the tool result as an `image` block (base64, of the type its suffix says),
and the pane's tool line says `Read image (N KB)` in place of `Read N lines`. A chapter's pictures (`pictures`, shipped
in `latet/tutorial/assets/`) are written into the agent's project as they are, at each start; the route above serves the
package's own copy, so the page shows the picture as it was on this computer whatever the agent did to its folder.

The agent's Bash runs `latet config set KEY VALUE` (or `--unset`) and `latet config get KEY` for real in
the sandbox: config.yaml is written, then the daemon's reload (`POST /_latet/admin/reload` with the local token,
agent `tutorial-agent`), where a change that lowers protection waits for approval. It prints what the CLI prints, with
`saved config.yaml` for the file's path, and the tutorial's own dashboard where the change waits ("here: Latet's
pane"): the CLI's `latet dashboard` would open the user's own, where the sandbox's changes are not. A setting the sandbox keeps for itself (`port`, `upstreams`,
`capture_dir`, `projects`, `identity`, `llm_review`, and every key under them) is refused before config.yaml is
written: ``Error: not in the tutorial's sandbox: `capture_dir` is the sandbox's own``. The reviewer's guidance is the
reader's to save, on the Rules page (`PUT /config`): the agent's command is refused `llm_review.guidance` too.
Start over undoes a change waiting as Undo it does, under the settings' lock.

## Latet on a server

The same API, at the server's address, for the space signed in (a person's; an API key has none): `latet dashboard`
on a connected computer asks the server for a link (`POST /_latet/server/v1/login-code`), and
`/_latet/ui/login?code=` sets the server's own `latet_session` (`Secure` over HTTPS). Differences:

- Each identity may hold 64 long HTTP connections on an instance: the two dashboard streams,
  bridge approval waits and AWS session holds together. At most four of these may be dashboard
  streams, both kinds together. A further connection receives 429 with `Retry-After: 2` before
  its space or body is read; the dashboard retries with its existing backoff.
- Other dashboard requests and other bridge requests each have their own `requests_in_flight`
  allowance (16 by default), separate from destination requests. A further request receives
  429 with `Retry-After: 2`. The computer calls retain their limits below.

- `GET /me` adds `server: {url, identity: {id, name, kind: "person" | "key"}} | null` (null on a computer).
- `GET /config` has no `shared` (until 2026-10-20 it named the items set for everyone; rules for everyone are the
  Enterprise Edition's since). The settings in use are the space's own over the shared
  settings, which hold only what a server needs to run (destinations, the reviewer, OCR, history); the lists stay
  whole, and a save that lacks a destination set for everyone answers 409
  `{error, reason: "settings_drops_shared", dropped: {"upstreams": <count>}}`: how many, never which addresses.
  `identity.hostnames` lists the host names of the person's other computers (`hostname` holds the first). While a
  change to the settings waits for the person (an administrator moved a destination set for everyone or turned the
  reviewer off; on the upgrade, the rules for everyone an earlier version applied), the settings in use are the
  stricter ones she approved: a save of hers applies her own changes at once and approves nothing that waits, even an
  item the save names, and the change waiting is asked again with her changes in it. Such a save, when what she
  approved last cannot be read, answers 409 `{error, reason: "settings_change_waits"}`, nothing saved. Rejecting the
  upgrade's change keeps the rules as hers, which a person past 500 own recognizers, field rules or patterns with
  them added cannot do (`settings_too_many_rules`): she can approve it. Her other saves meanwhile are counted on her
  own rules alone, the kept ones left out (2026-10-20, the final review's M-1).
- `GET /config`'s `etag` and `GET /stats`' `partial` are a computer's too (above): a save from a page older than the
  settings in use answers 409 `settings_stale`, as one refused because another instance wrote the settings since. On a
  server `PUT /config` and `PUT /projects/{id}/personal` must carry the `etag` (the dashboard and the server ship
  together): one without it answers 409 `settings_stale` too, nothing saved, and the page reads the settings again. A
  save its store does not keep answers 409 `settings_stale`, 413 `settings_too_large`, or 503
  `settings_unavailable` with `Retry-After: 1`, nothing of that save kept. These no-write outcomes are distinct from
  a kept personal edit whose pending shared-change proposal could not be refreshed: that answers 409
  `{error, reason: "settings_saved_change_pending", saved: true}`. Keep the saved indication, reload the settings
  and pending change, and do not automatically repeat the mutation. The HTTP status alone does not say whether
  the edit was saved. This partial result applies to settings mutations through `PUT /config`,
  `PUT` or `DELETE /projects/{id}/personal`, `POST /allow`, `POST /terms`,
  `DELETE /destinations/{name}`, and `POST /rules/import` with `apply: true`. An import preview
  does not mutate settings. Import `all: true` accepts the imported held changes; it does not
  approve an existing pending shared-change proposal.
- The other refusals of what a save holds carry a `reason` the dashboard words in its reader's language: 409
  `settings_too_many_rules`, `settings_too_many_projects`, `settings_drops_shared` (above), `settings_change_waits`,
  `settings_terms_too_long` (a term longer than 8,192 characters; no term value in the reason, nothing saved);
  in a 400's `details`, `reason: "destination_refused"` (with `url`, the identity's own address) for a destination of
  its own that only the server would reach, and `reason: "reviewer_key_server"` for a setting that names where the
  local reviewer's key is read from. A project's notice gives `reason: "space_past_share"` or `"project_unbuilt"`
  (its rules could not be built), the latter on a computer too; a repository file's ignored setting gives
  `"repo_cannot_set"` (only your own settings for the project set it), `"repo_whole_latet"` (only config.yaml) or
  `"repo_unknown"`, on a computer and on a server. Clients localize `settings_terms_too_long` on any
  save that returns it, including applying an import. Import preview does not change settings and is not proof
  that the server will accept the later save.
- `GET /reviewer/key` answers `{source: "server", variable: null, stored: null}`: the server gives the key (PUT and
  DELETE 409), and its address, model, slots, piece size and certificate are the shared reviewer's.
- A sign-in link that signs no one in sends the browser back to the dashboard's page (303), never a JSON body:
  `?login=expired` (used, more than two minutes old, of no one, or opened from another site's page), `?login=limited`
  (too many links tried from its address: wait a minute), `?login=busy` (the server could not look it up now). The
  address holds nothing of the code. `POST /logout` (with `X-Latet-UI`) ends the session on every instance.
- `GET /integrations` lists no agent (they run on the computers), its one check is the server, and each destination
  adds `shared: boolean`; a shared one is not removed (409).
- `POST /_latet/admin/user-name` takes `{name}`, the computer's user name (a text of 1 to 256 characters, not only
  white space, with no control, format or separator character, nothing else in the body, 16 KiB at most: 400 or 413
  otherwise), interned as it is sent when the space's settings want a surrogate, and answers `{surrogate: null}` when
  they keep it; the server's own user name is never read. `POST /_latet/admin/destinations` takes 20 addresses at
  most (64 KiB, each 2,048 printable ASCII characters at most) and refuses (400) one only the server would reach, as a
  save of `upstreams` does: http, the server itself (its names, its address, a loopback, link-local or unspecified
  address), or an address in its own network (private, unique-local, carrier-grade NAT, reserved, documentation), as a
  literal or as a name that resolves to one; a shared destination's address is named, never judged, and the call is
  refused whole (409) when the settings changed under it meanwhile. A computer calls both at `/u/<route key>/…` with
  its token, by POST alone (any other method, or a WebSocket, 404), one at a time for each identity on an instance
  (429, `Retry-After: 1`) and 30 a minute for each identity (429, `Retry-After: 60`). An API key's token gets 403
  `{error: "… an API key has no computer"}`: its settings are its administrator's. The CLI's other calls (`login-code`,
  `reload`, `changes/{id}`, `known`) answer 409 in a space on a server, and the server's gate lets none of them reach
  it (404).
- Every answer under `/u/<route key>/` says `cache-control: no-store`.

### Server API for computers and programs

A server's routes outside the administration and the identity's dashboard. `SERVER_API` is `/_latet/server/v1`
(`src/latet/server/settings.py`); the handlers are `machine_routes` in `src/latet/server/machines.py`. A personal token
(`latet_pat_…`) belongs to a person's computer; an API key's token (`latet_key_…`) to a program. Both go in one
`x-latet-token` header (two such headers name no token); `Authorization` is not read there. Every route under `{SERVER_API}`
and `/u/<route key>/` refuses a browser's request (403), and an API key's token gets 403 on `login-code`, `identity` and `token`. A refused credential is counted against its address's budget (429 with `Retry-After`
past it, 503 with `Retry-After` while the credentials' threads are busy); a refusal never says which part was wrong.

A computer also gets a **computer key** (`latet_ck_…`, R16) at enrolment, shown once with its token. Its CLI keeps it
in the token's own record (the credential store, or the private token file) and sends it in the
`x-latet-computer-key` header (two such headers name none) on the two calls that ask for it, and on no other:
`POST {SERVER_API}/login-code` and `PUT {SERVER_API}/identity`. The token alone, which Claude Code holds in its
environment, keeps `whoami`, `DELETE token`, the bridge, the space's proxy routes and the computer's two calls under
`/u/<route key>/_latet/admin/` (a destination added, which lowers nothing; the user name's fake, as the bridge's mask
gives one). The server keeps the key's keyed hash with the token's row, bound to the token's id, and compares it in
constant time with what PostgreSQL holds of the row now (a token revoked meanwhile answers 401). A key missing or
wrong answers 403 `{error, reason: "computer_key_required"}`, the same words for both, and counts as a miss of its
address (past the budget, 429); a valid token with its right key passes while its address is past its budget. A token
made before computer keys answers 403 `{error, reason: "computer_key_missing"}`, whose words say to connect again.

| Method and path | Credential | Request and response |
| --- | --- | --- |
| `GET /_latet/health` | none | The instance is alive; answered under any host name, for a load balancer. |
| `GET /_latet/ready` | none | The instance reaches PostgreSQL and Valkey; any host name. |
| `POST {SERVER_API}/enrol` | none: the one-time code is the credential | Body `{code, label}`, 16 KiB at most (a code of 128 characters at most; of the label, 1,000 characters are read and 100 kept, cleaned). 429 past 10 codes in 10 minutes from one address. Answers once `{token, computer_key, identity: {id, name, kind}, url, base, openai, anthropic}`: the computer's personal token, its computer key and its space's routes. 401 for a code unknown, used or late; 413 past the body's bound. |
| `POST {SERVER_API}/login-code` | personal token and its computer key (`x-latet-computer-key`) | Answers `{code, url}`: a one-time sign-in link for the dashboard (`{url}/_latet/ui/login?code=…`), valid two minutes, opening a session only while the token that asked holds; 10 a minute for each person (429). |
| `GET {SERVER_API}/whoami` | personal token or API key's token | Answers `{id, name, kind, url, version, base, openai, anthropic}`: who the token is and its space's routes. |
| `PUT {SERVER_API}/identity` | personal token and its computer key (`x-latet-computer-key`) | The identity facts Latet found on the person's computer, one call at a time for each person. Answers `{ok: true}`. |
| `DELETE {SERVER_API}/token` | personal token | Revokes the calling token itself (`latet disconnect`). Answers `{ok: true}`. |
| `… /u/<route key>/…` | the route key in the path; `x-latet-token` for the bridge and the computer's two calls | The identity's space: the agents' provider routes, and the plugin's bridge under `/u/<route key>/_latet/engine/v1/`. A computer's two calls, `/_latet/admin/destinations` and `/_latet/admin/user-name`, by POST alone (above); an API key's token gets 403 there. |
| `GET /_latet/metrics` | `Authorization: Bearer <metrics token>` | Only on the metrics socket (`metrics_host`:`metrics_port`, 127.0.0.1:7283 by default), opened only when `metrics_token_file` is set; the instance's own port never answers it. Prometheus text; a browser's request 403, a wrong token 401, past the address's budget 429. |

### Administration API

These are program-facing routes under `/_latet/server/v1/admin`, separate from the identity's dashboard API.
They use `X-Latet-Admin: <administration token>`, not a dashboard cookie or personal token. Browser requests,
including same-origin browser fetches, are refused with 403. A server configured with an HTTPS public address
refuses administration over HTTP. Keep credentials out of URL paths and query strings. All administration
answers carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

| Method and relative path | Request and response |
| --- | --- |
| `GET /identities` | Optional `kind=person` or `kind=key`; returns `{identities}` including computer-token counts and tampered-token counts. |
| `POST /identities` | `{kind, name, email?}`, with email only for a person. Returns 201 `{identity, code}` for a person or `{identity, key, ...routes}` for an API key; these secrets are shown once. |
| `GET /identities/{id}` | Returns `{identity, tokens}`. Token metadata contains no credential. |
| `PATCH /identities/{id}` | `{name?, email?, disabled?}`; returns `{identity}`. Disabling retains the space. |
| `DELETE /identities/{id}` | Removes the identity and its live server space; returns `{ok: true}`. This does not erase external copies or backups. |
| `GET /identities/{id}/tokens` | Returns `{tokens}` with ids and metadata, never the token secrets. |
| `DELETE /identities/{id}/tokens` | Optional `token` query parameter is the listed token id; absent means all. Returns `{revoked: count}`. A malformed id is refused, never treated as all. |
| `POST /identities/{id}/code` | Person only; returns `{code}`. An API key receives 409. |
| `POST /identities/{id}/rotate` | API key only; returns `{key, ...routes}`. A person receives 409. The new key and route addresses replace the old ones. |
| `GET /identities/{id}/settings` | Enabled API key only; returns `{settings, etag}`. A person's settings belong to their dashboard. |
| `PUT /identities/{id}/settings` | Settings keys at the top level with optional `etag`; returns `{settings, etag}`. A key left out keeps its value (since 2026-10-20: it used to reset to its default); a key that is no setting receives 400, counted and never quoted, nothing written. Preserves shared-item restrictions and the administrator-owned reviewer; do not treat this route as a personal dashboard save. |
| `GET /shared` | Returns the current shared `{settings, etag, kept_rules}` from PostgreSQL: `settings` the keys a server needs to run (`SETTING_KEYS`: upstreams, llm_review, ocr, history, retention_days), `etag` the stored row's, `kept_rules` the rules for everyone an earlier version kept in it (`{}` when none), applied by no space. An unreadable shared row receives 500. |
| `PUT /shared` | Shared settings keys at the top level with optional `etag` and `forget_kept_rules`; returns `{settings, etag, kept_rules}`. A key left out keeps its value (since 2026-10-20: it used to reset to its default; `{}` changes nothing); a key that is no setting receives 400, counted and never quoted, nothing written. A rule key (`RULE_KEYS`) receives 400 with `RULES_FOR_EVERYONE` and `details` naming the keys, nothing written. The kept rules are written back with the settings, unless `forget_kept_rules` is true, which deletes them. With an etag, a changed or unreadable row receives 409; oversized shared settings receive 413 without a write. |
| `GET /admin-tokens` | Returns `{tokens}` with id, label, timestamps, issuer, `this` and `tampered`; never a secret or hash. |
| `POST /admin-tokens` | `{label, days?}` or `{label, never: true}`; returns 201 `{id, token, expires}`. `days` is an integer from 1 to 3,650, mutually exclusive with `never`. The new token cannot outlive its issuer; an explicitly longer request receives 403. |
| `DELETE /admin-tokens/{token}` | The listed token id, not its secret; returns `{revoked: true}` or 404 if absent. |
| `GET /log` | Filters `since`, `until`, `actor`, `action`, `limit`, `cursor`; returns `{entries, next_cursor}`. Duplicate filter parameters or an invalid/mismatched cursor receive 400. |
| `GET /log/verify` | Returns `{ok, checked, broken_at, head}`; `head` is null or `{id, hash}`. A failed walk returns `ok: false`, not an unchecked success. |
| `GET /status` | Returns public URL, version, instance, identity counts, active-space count, vault usage, cached audit-chain verdict and database/Valkey status, and `edition`: one `warn` line while the shared settings keep rules for everyone from an earlier version (their counts, never values), `[]` otherwise. It is not a check of connected agent installations. |

Use the etag returned by a settings read for read-modify-write clients. The administration routes accept
an omitted etag for explicit blind writes; this differs from the required etag of the dashboard's server saves.
An unreadable shared row may be replaced by such a blind write; the CLI exposes that recovery as the confirmed
`config reset`, not an ordinary read-modify-write. Do not remove an etag automatically to make a conflict succeed.
API-key settings can have been saved before a later approval step fails; after an uncertain administrative
mutation, read the resulting state rather than inferring rollback from an error or retrying the mutation blindly.

The log is ordered by `(ts DESC, id DESC)` with a fixed insertion ceiling for a traversal. Keep the original
filters when resuming; retention can still remove unread rows. A continuation is not a credential, snapshot or
integrity proof. `log_entry_too_large` and other bounded-read refusals return 422 with `reason`; they do not skip
the row. A transient read failure returns 503 with `Retry-After`. The CLI adds its documented retrieval and
output budgets; see `docs/src/content/docs/reference/server-administration.md`.

Invalid or ended administration tokens receive 401. Refusal-rate limits can return 429, and unavailable or
saturated credential handling can return 503, with `Retry-After`. Respect the header without blindly repeating
a mutation whose outcome is unknown. Administrative actions and refusals are recorded with bounded metadata;
if their required recording fails, the response is an error. The administration log is not a transcript of
secrets. Its full chain check still cannot detect a removed tail without a previously retained external head.
