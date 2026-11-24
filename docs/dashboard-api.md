# Dashboard API contract (v1)

Served by the daemon on `http://127.0.0.1:<port>`. The UI lives at `/_latet/ui/` (static files,
unknown sub-paths fall back to `index.html`). The API lives at `/_latet/api/v1/`.

## Authentication
- `latet dashboard` asks the daemon for a one-time code and opens
  `/_latet/ui/login?code=<code>`; the daemon sets the HttpOnly, SameSite=Strict cookie
  `latet_session` and redirects to `/_latet/ui/`.
- Every API call sends that cookie (same origin, `credentials: "same-origin"`). State-changing
  calls (POST/PUT/DELETE) must also send the header `X-Latet-UI: 1`, and so must the two reads that record
  themselves on a server, `GET /ee/reports/export` and `GET /ee/audit/export`: without it they answer
  `403 {"error":"missing X-Latet-UI header"}` before anything is taken or written. The dashboard sends the header on
  every request.
- Without a valid session every API call answers `401 {"error":"unauthorized"}`: the UI then shows
  a screen telling the user to run `latet dashboard`.
- **On a server with Latet Enterprise's single sign-on** (E3): `GET /_latet/sso` answers `{login}` (404 when single
  sign-on is off, or does not serve: a trial that ended, a license without `sso`) for the sign-in page's button;
  `GET /_latet/sso/login` sends the browser to the company's identity provider (PKCE; what the sign-in needs waits in
  Valkey, sealed, for ten minutes; a `latet_sso` cookie, SameSite=Lax, binds it to this browser; past 100 sign-ins
  begun from one address (IPv4 address, IPv6 /64) and not yet finished, or past 1,000 from one IPv6 /48, 429, and past
  20,000 not yet finished on the server, 503, each with `retry-after` and a page of fixed words that moves to
  `/_latet/ui/?sign-in=unavailable`, nothing stored; a sign-in is finished when its callback comes back, signed in or
  not, and stops counting unfinished after ten minutes);
  `GET /_latet/sso/link`, begun from a person's own dashboard session (**Link my company account**), does the same and
  links the company account it comes back with to that person when its ID token names them as their account does;
  `GET /_latet/sso/callback`, on whichever instance it reaches, opens the `latet_session` and moves to `/_latet/ui/`, or
  redirects to `/_latet/ui/?sign-in=<reason>` (`expired`, `failed`, `not-allowed`, `not-provisioned`, `disabled`,
  `unavailable`, `trial-ended`: a trial that ended stops single sign-on, and the sign-in page shows the terminal's way
  alone). A session records how it was opened, which `/me.session.via` says: `sso` for this sign-in, `code` for the
  one-time code of `latet dashboard` (the Community's way, always there).
- **An API key's space** (E3): while a person who controls an API key (or an administrator) has it open, the dashboard
  sends each request to `/_latet/api/v1/ee/keys/{id}/space` + its path, and the key's own app answers it; the routes
  under `/ee/keys` stay the person's, and no Enterprise page or route is the key's (a request to one there answers 403).
  A refusal carrying `"space": "closed"` (403, 404, 409 or 503: control taken away, the key disabled or gone, a trial
  that ended, the license without `roles`, who may do what unreadable, the space not available, or 403 `code:
  "company-sign-in"` for an administrator who does not control the key, in a session the company's sign-in did not open
  while `company_changes` is `sso`) sends the dashboard
  back to the person's own space; the server marks what it closes, and no other refusal does. Signing out
  (`POST /logout`) is always the person's own.
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
  session: string            // the conversation's opaque keyed key ("hg:l1:..."), never an agent's session id
  agent: Agent
  agent_version: string | null
  upstream: string           // "anthropic" | "openai" | "chatgpt" | "openrouter" | ... | "custom" (one of yours)
  destination?: string       // the list's alone: a destination of yours by its name in your settings
  host: string | null        // where it went: a provider's public endpoint ("api.anthropic.com",
                             // "bedrock-runtime.eu-west-1.amazonaws.com"), "custom" for any other address
  signed: boolean            // Latet signed it for Amazon (never with which profile)
  path: string               // the route template it was admitted by ("/{destination}/v1/messages",
                             // "/bedrock/{region}/model/{model}/{operation}"), the plugin's check
                             // ("/plugin/final-check"), or an older record's area ("/gateway")
  protocol: string | null    // "anthropic" | "chat" | "responses" | "embeddings" | null
  mode: Mode
  transport: "http" | "ws"
  model: string | null       // as its provider's grammar admits it; null for anything else
  status: number | null      // HTTP status (101 for a WebSocket turn; 400 for one Latet stopped, as over HTTP)
  latency_ms: number | null  // whole exchange
  overhead_ms: number | null // time Latet added before forwarding; null for a request never sent
  tokens: { input: number; output: number; cache_read: number; cache_write: number } | null
  masked: number             // values replaced in this request
  new_masked: number         // of which in blocks never sent before in this session
  by_type: Record<string, number>   // e.g. {"EMAIL": 1, "GITHUB_PAT": 2}: Latet's detectors' types, OTHER for yours
  new_by_type: Record<string, number> // the same, counting only blocks new to the session
  by_class: Record<EntityClass, number> // values masked in this request, by class
  by_module: Record<Module, number> // the same, by the module that found them (missing in older records)
  restored: number           // values restored in the response
  flags: string[]            // "blocked", "leak", "uninspected:image", "degraded:pii", "unknown-route",
                             // "withheld" (the dump guard kept a tool output from the model)
                             // "outbound:<category>" ("web-search", "web-fetch", "remote-tool": a tool
                             // outside this computer got a stand-in: stopped
                             // by the Claude Code plugin, or run by the provider; its input is an
                             // "outbound_tool" slot)
  verified: boolean          // the egress check ran and found nothing in clear (false for an older Latet's row)
  block_reason: string | null // why a blocked request was stopped, by its category code ("refused",
                             // "masking-failed", "too-large", "credentials", "proof-incomplete", "evidence",
                             // "coverage-limit", "envelope-..."), every refusal flagged "blocked"; the words told
                             // to the agent are in ExchangeDetail.sensitive (full history only)
  launch: "launched" | "launched-key-missing" | "trusted" | "claimed" | null
                             // what its launch was decided as (P2-6a): its launch key verified; a session Latet
                             // launched without a key it can verify (decided as claimed); the plugin's binding of a
                             // hand-configured Claude Code; anything else, a claim; null where none was decided
                             // (the plugin's own checks, an older record). Never a key, a folder or a project
  coverage: "complete-accounting" | "legacy-uninspected" | "unknown"
                             // every part read (a Forward, or a Pass under the rules below); parts sent unread before
                             // 0.11; not recorded (an older record, a refusal, the plugin's own check). A WebSocket
                             // turn records it from its own decision, as a request over HTTP does
  passes: ("checked-only" | "new-member" | "image-remainder" | "sent-before" | "model-written"
           | "provider-echo" | "provider-encrypted" | "exception")[]
                             // what a Pass rested on besides a complete reading, by category: a value the final check
                             // read without masking it (D6, RC-REC-4); a member no request profile names, read whole and
                             // checked (0.15.0, never its name); an image's remainder after its text was read (M3);
                             // a text sent before or written by the model; a provider's opaque item, observed (an echo)
                             // or by the built-in rule (M2); a request exception (never its id). Empty for a Forward
  unknown_members: "check" | "refuse" | null
                             // the mode that decided a member no request profile names (0.15.0); null in an older
                             // record or one the plugin only observed
  profile: string | null     // the request profile its envelope was admitted by ("anthropic.messages.1")
  removed: { headers: number; members: number }  // what its profile removed (D4): a 401/403 that follows says so
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
  sensitive: { block_reason: string | null; ... } | null  // the full history's private view: the words
                             // Latet told the agent (types and lengths only); null in a metadata history
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
| GET | `/me` | | `{version, profile, port, data_dir, recording, tutorial, server, session, docs, edition}` (`tutorial`: null but in the tutorial's sandbox, below; `server`: null but on a Latet server, below; `session`: null but on a Latet server, where `{via: "sso" \| "code"}` says how this dashboard session was opened, with the company's single sign-on or with `latet dashboard`'s one-time code, a session that does not say being a `code` one; `docs`: whether this install serves its documentation at `/_latet/docs/`; `edition`: `{name: "community" \| "enterprise", license: LicenseSummary \| null, features: string[], pages: string[], roles: string[], actions: string[], policy: PolicySummary \| null, connected?: ConnectedNotice, computers?: ComputerOfYours[], linked?: boolean, approvals?: ApprovalRoutes}`, the Enterprise pages this person may open in `pages`, none without a license file, the `policy` page for whoever has `policy.read` on a server; `policy`: where the server's policy comes from and what is wrong, null without one; `connected`: `{until, computers, codes, more}`, below; `computers`: the computers connected to the person's own account now, listed for good (below; absent for an API key, in Community, while the license serves neither `roles` nor `sso`, and when it cannot be read); `linked`: whether the person's own account is linked to the company sign-in (never what links it), so that **Link my company account** is offered to those it is `false` for; absent for an API key, in Community, while the license does not serve `sso` and when it cannot be read; `approvals`: who decides on this identity's changes that lower protection and on its Live Reviewer's texts (Latet Enterprise's central approvals, below), absent in Community, on a computer and when it cannot be read; on a Latet server `license.users_in_use` is null for anyone without `license.manage`) |
| GET | `/exchanges?limit=100&before=<id>&session=<key>&agent=<agent>&flag=<flag>&project=<id>` | | `{items: ExchangeSummary[], next: string \| null}` |
| GET | `/exchanges/{id}` | | `ExchangeDetail` |
| DELETE | `/exchanges` | | `{ok: true}` (clears the recorded history; the vault is kept) |
| GET | `/sessions?limit=50` | | `{items: Session[]}` |
| GET | `/vault?q=&type=&class=&page=1&per_page=50` | | `{items: VaultEntity[], total, page, pages, per_page, types: {TYPE: count}}` (no originals; newest first, then by id; `per_page` 10-200; a page past the last is the last; `types` counts the whole vault). In an API key's space (E3), whose vault PostgreSQL holds and memory a working set of: the page, `total` and `types` are PostgreSQL's, and `q` finds a value by its id, its type, its value or its fake whole, or a word of `q` the vault finds them by (an address's local part, a value's longest word) where they hold `q`: never by reading the whole vault, so a part of such a word finds nothing; `q` is 200 characters at most there, and 400 when it is longer or its words name more than 1,000 values (`stored.SEARCHED_AT_MOST`), 503 when PostgreSQL does not answer; a page view counts and sorts the key's rows in PostgreSQL (none opened but the page's) |
| POST | `/vault/{id}/reveal` | | `{original}` (in an API key's space, as PostgreSQL holds it now: a value forgotten on any instance is 404) |
| DELETE | `/vault/{id}` | | `{ok: true}`. In an API key's space (E3): the entry read by its id from PostgreSQL (404 for none), then its value forgotten as `latet server keys forget` forgets it (the key's history rows and the local reviewer's answers that name it, a new generation of fakes, every instance told) with the administration log's `key.forgotten {values: 1, forgotten, unknown, source: "space"}`, by the person working there, in the same transaction (a log that does not take it forgets nothing: 503; so does a request that names no person working there (or the key itself), or a space whose server handed it no forget: 503 in fixed words, nothing forgotten, never forgotten as a person's own value is) |
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
| GET | `/ee/license` | | `LicenseStanding` (below), the server's license as a person on it is told it; the Enterprise Edition only, which is a server's (404 elsewhere); it answers in every state, a trial that ended included; `users_in_use` and `over_since` are told to whoever has `license.manage` (the administrators) and are null for anyone else (`latet server license show` says them too). The standing a `PUT` or a `DELETE` answers is the same, told to its maker as to any reader |
| PUT | `/ee/license` | `{license: <the license file's text>, policy_stops?: true}` | `LicenseStanding` (Latet Enterprise on a server, for `license.manage`: the license installed, its entry in the administration log by the person, every instance told); 400 `{error, code}` for a file that is no license, its reason in `code` (a `LicenseError` code); 409 `{error, policy?: "stops", terms?: "stop", approvals?: "stop"}` when the server's policy stops being in force with it (`policy`: the file lists no `locked-rules`), the synced terms stop being masked (`terms`: synced terms are kept and the file lists no `term-sync`) or central approvals stop (`approvals`: the file lists no `approvals` while changes or the Live Reviewer's texts go to the security team, or while it lists `roles`, changes are whoever's settings they are and the server has an API key, whose changes go to its other controllers), or several, its `error` naming each, nothing changed, unless the body says `policy_stops: true`, which confirms what stops (the entry carries `policy`, `terms` and `approvals` as the 409 did; what stops is weighed from the features what protects kept, a stored file that does not verify's included); 403 `{error, code: "trial-ended" \| "not-manageable"}` while the license cannot manage (a trial that ended, paused, invalid, or none: `latet server license install` then) and `{error, code: "company-sign-in"}` from a session that is not the company sign-in's (below) |
| DELETE | `/ee/license` | nothing, or `{policy_stops: true}` | `LicenseStanding` and `removed: boolean`; as `PUT` (a removal of a license that lists `locked-rules` while a policy is stored, `term-sync` while synced terms are kept, or `approvals` while the security team decides, is the 409) |
| GET | `/ee/policy` | | `{floors: PolicyFloor[], below: string[], problems: string[]}` (Latet Enterprise, on a server: the policy in force, none or one; every person reads it, as a person is told it: what is locked is named and no term, value, pattern or guidance of the company's is in it, below) |
| GET | `/ee/policy/text` | | `{etag: string \| null, policy: {etag, set_at, set_by, keys, brought, exceptions, notices, in_force, text} \| null, problems: string[]}` (Latet Enterprise on a server, for `policy.edit`: the stored policy with its text, as `latet server policy show` sees it, in every standing; `policy` null when none is stored, or when the server cannot read the one stored, its problem in `problems`; `etag`: the stored policy's, readable or not, what `PUT` sends back; nobody reads it while the license has no `roles`); 403 `{error, code: "company-sign-in"}` from a session that is not the company sign-in's (below: the text holds the company's terms and values); 503 `{error}` in fixed words when the server's database does not answer |
| PUT | `/ee/policy` | `{policy: <the policy file's text>, etag: <the stored policy's etag as GET /ee/policy/text gave it, null when none>, accept_notices?: true}` | `{etag, keys, exceptions, notices, problems}` (on a server, for `policy.edit`: the floor of every person and API key, in the administration log by the person; `problems`: the other instances not told now, or instances that do not apply the policy); 400 `{error: "not changed: …"}` for a text the server cannot read, or for a file with keys left out whole sent without `accept_notices: true`, the stored policy staying, and `{error}` for a body without `etag`; 409 `{error, code: "policy-changed"}` when the stored policy is no longer the one `etag` names (changed since its text was read, from the page or `latet server policy set`), nothing changed; 403 when the license does not allow it (`{error, feature}`, or `code: "trial-ended" \| "not-manageable"`) and `{error, code: "company-sign-in"}` from a session that is not the company sign-in's (below); 413 past twice a rules file's size |
| DELETE | `/ee/policy` | `{etag: <the stored policy's etag as GET /ee/policy/text gave it, null when none was stored>}` | `{removed}` (the policy removed with the etag it names, as the save names the policy it replaces); 400 `{error}` (the body it asks for) for no body or an etag that is neither a text of at most 128 characters nor null; 409 `{error, code: "policy-changed"}` when the stored policy is no longer the one `etag` names (changed since its text was read, from the page or `latet server policy set`), nothing removed; 403 as PUT. The Policy page's **Remove** sends it |
| GET | `/ee/people` | | `{people: Person[], enabled}` (Latet Enterprise on a server, for `people.manage`; `Person`: `{id, name, email, user_name, disabled, source: "local" \| "scim" \| "oidc", last_seen, linked, claimable, scim_managed, disabled_by: "local" \| "scim" \| null, groups: {id, name, source}[], roles: Role[], through: {role, group: {id, name}}[]}`: the roles given to the person and, in `through`, to their groups; `linked`: whether the company sign-in is linked to their account, never what links it; `claimable`: whether their first sign-in finds their account by its user name (an administrator or SCIM linked it, it has no subject yet and was not deleted at the provider), which `latet server people link --all` leaves be; `scim_managed`: SCIM sees and changes only those it manages; `disabled_by`: an administrator's disable is never lifted by SCIM; `enabled`: the people and API keys the license counts) |
| POST | `/ee/people` | `{name, email?}` | 201 `{person: {id, name}, code, command}` (a person added by hand, with a one-time code to connect a computer: `command` is `latet connect <the server's address>`, with `--fingerprint` for a self-signed certificate, and the code is shown apart); 409 for an e-mail an enabled person has |
| POST | `/ee/people/{id}/code` | | `{code, command, person}`; from the dashboard only for a person who has never connected a computer, and not disabled (409 otherwise: `latet server people code` makes it); recorded in the administration log, and told to the person at their next visit |
| POST | `/ee/people/{id}/disable`, `/ee/people/{id}/enable` | | `{ok, person, scim_tokens?}` (`scim_tokens`: the ids of the SCIM tokens the person made that still serve though they no longer administer, which a disable says and never ends); 409 for the last administrator, and for an e-mail an enabled person has |
| GET | `/ee/groups` | | `{groups: {id, name, source: "local" \| "scim" \| "oidc", external_id, members: {id, name, source}[], roles: Role[]}[]}` (for `people.manage`) |
| POST | `/ee/groups` | `{name}` | 201 `{group: {id, name, source, external_id}}`; 409 for a name taken, 400 for one that is not a name |
| PUT | `/ee/groups/{id}` | `{name}` | `{group}`; 409 for a group from the identity provider |
| DELETE | `/ee/groups/{id}` | | `{ok, scim_tokens?}`; 404 `{error}` when the group is gone, also when it was removed meanwhile; 409 for a SCIM group, or when it would leave no administrator |
| POST | `/ee/groups/{id}/members` | `{person}` | `{ok, person}` (`person`: the person's id, user name or e-mail; an e-mail only when one enabled person has it and it was given by hand or by SCIM: 409 otherwise, name them by their id; the answer says whom the name was taken for) |
| DELETE | `/ee/groups/{id}/members/{person}` | | `{ok, person, scim_tokens?}`; 409 for a membership made by the identity provider (change their groups there) or the last administrator |
| GET | `/ee/roles` | | `{grants: {subject: {kind: "person" \| "group", id, name}, role}[]}` |
| POST | `/ee/roles` | `{subject, role}` | `{roles, subject}` (`role`: `group-lead`, `security`, `auditor` or `admin`; the `user` role is everyone's: 400; `roles`: those the subject has now) |
| DELETE | `/ee/roles/{subject}/{role}` | | `{roles, subject, scim_tokens?}`; 404 when the subject does not have it, 409 when it would leave no administrator |
| GET | `/ee/keys` | | `{keys: {id, name, disabled, created, last_seen, controllers: {kind: "person" \| "group", id, name}[]}[], manage}` (always the person's own keys, in a key's space too: for `keys.manage`, every key with who controls it; for anyone else the keys they control, themselves or through a group, with no `controllers`, and `manage: false`) |
| POST | `/ee/keys` | `{name}` | 201 `{key, secret, address}` (for `keys.manage`; the secret is shown this once, and `address` is what a program uses in place of the provider's) |
| POST | `/ee/keys/{id}/rotate` | | `{secret, address}`: the key's token rotated as `latet server keys rotate` rotates it (its name, its permissions and whether it serves agents kept; the entry `key.rotated` names the token and the one it replaced), `address` null for a token that serves no agents; 409 when the key has several tokens (added with `latet server keys add-token`: rotated with `--token` there) |
| POST | `/ee/keys/{id}/disable`, `/ee/keys/{id}/enable` | | `{ok}` |
| POST | `/ee/keys/{id}/controllers` | `{subject}` | `{controllers, subject}` (a person or a group; who controls the key now, and whom the name was taken for) |
| DELETE | `/ee/keys/{id}/controllers/{subject}` | | `{controllers, subject}`; 404 when they do not control it |
| any | `/ee/keys/{id}/space/{rest}` | | The key's own `/_latet/api/v1/{rest}`, for a person who controls the key or may manage keys (through `keys.manage` alone, from a session the company's sign-in opened while `company_changes` is `sso`: else 403 `{error, code: "company-sign-in", space: "closed"}`, nothing read or recorded), with the key as the identity: a change there is in the administration log, by the person, its path with every parameter written `***` (but a value of the key's vault forgotten, `DELETE /vault/{id}`, which the key's app records itself as `key.forgotten` in the forget's own transaction); a stream open there ends when the person may no longer open it; 403, 404, 409 or 503 `{error, space: "closed"}` otherwise (403 `{error, code: "trial-ended", feature, space: "closed"}` once a trial ended) |
| GET | `/ee/sign-in` | | `{sso: SsoSettings \| null, etag: string \| null, company_changes: "sso" \| "any-session" \| null, redirect_uri, secret, secret_issuer_changed, scim: {url, tokens: {id, created, expires, last_used, made_by?, maker?}[]}}` (Latet Enterprise on a server with `sso`, for `sign-in.edit`, and for everyone while the license has no `roles`; read from PostgreSQL, not an instance's copy; `etag`: the SHA-256 of the sso section as the server holds it now, null while single sign-on is off, what a change made from this view names; `SsoSettings`: `{issuer, client_id, client_secret, scopes, user_name_claim, groups_claim, accounts: "scim" \| "group" \| "open", access_group, allowed_domains, groups}`, `client_secret` the saved secret's name and never the secret; `company_changes`: whether the changes that apply to everyone take a session the company sign-in opened, set with `latet server` alone; `secret`: whether the client secret is saved for the issuer, `secret_issuer_changed`: whether one is saved for another; `made_by` and `maker` (`{kind: "person", id, name, admin, disabled}` or `{kind: "administration", id}`) only for whoever may edit the sign-in) |
| PUT | `/ee/sign-in` | `{sso, etag?}` (`sso` null: off; `etag`: the `etag` of the view the change was made from, a text of at most 128 characters, or null for "none was set up"; the Sign-in page always sends it) | The same view, with the new `etag`; 400 `{error, details}` for settings the server cannot use, for a body that names `company_changes`, and for a malformed `etag`; 409 `{error, code: "sign-in-changed"}` when `etag` is not that of the settings now stored (changed since they were read, from the page or `latet server sign-in set`), nothing changed (without `etag`, as before: the settings are replaced); 409 when it would turn single sign-on off while changes that apply to everyone take the company sign-in, or once people signed in or while an account waits for its first sign-in (`latet server sign-in off` turns it off), when the issuer changes after people signed in, when `user_name_claim` changes after people signed in or while an account waits for its first sign-in (`latet server sign-in set` changes both), and when it would leave no administrator; recorded `sign-in.changed` with the names of the fields it changed, never their values |
| PUT, DELETE | `/ee/sign-in/secret` | `{secret}` | `{ok}`; the secret is kept sealed for the issuer it was saved for, and never comes back |
| POST | `/ee/sign-in/test` | `{sso?}` | `{ok, error, issuer, authorization_endpoint, token_endpoint, keys}` (whether the provider answers as single sign-on needs; its own words are in `error`, to an administrator alone; recorded `sign-in.tested` with its outcome before it is answered, never the issuer or the provider's words) |
| POST | `/ee/sign-in/scim-tokens` | `{days?}` | 201 `{id, token, url, expires}` (the token is shown this once; it always ends: `days` is a whole number of days, and `null` is refused, `latet server sign-in scim-token --never` makes one that never ends) |
| DELETE | `/ee/sign-in/scim-tokens/{id}` | | `{ok}` |
| POST | `/ee/sign-in/code` | | `{code, command}`: a one-time code for the person signed in to connect a computer, while single sign-on serves; 404 otherwise. 409 `{error, code: "computers-most", most}` while 20 computers or more are connected to their account (`COMPUTERS_MOST`; those an administrator connected count, an administrator's codes are never refused), 429 `{error, code: "codes-too-many", most}` with `retry-after` (whole seconds, until the oldest leaves the window) past 10 of their own codes in any hour (`CONNECT_CODES`, `CONNECT_CODE_WINDOW`); both counted in the code's transaction, the person's row held, and a refusal writes nothing |
| POST | `/ee/notices/told` | `{until}` | `{ok, told}`: the person saw what their dashboard told them up to `until` (`edition.connected.until`), and it is told no more; 403 from a session the company sign-in did not open |

Every route under `/ee/` is under a rule of `ee/access.py` (`RULES`): the feature the license must list and the action of
`ee/roles.py` a read and a change need; one under no rule is refused. Behind it each route weighs the license by its
kind: a route that manages Enterprise settings is read in every standing and changed only while the license can
manage (403 `code: "trial-ended"` or `"not-manageable"`); a route that lets people in answers until a trial ends
(403 `code: "trial-ended"`). A change that applies to everyone (a person, a group, a role, the keys and their control,
the sign-in settings, the license, the policy, the term sources and their secrets, a sync asked for, a term removed, a
code to connect a computer) is refused to a session `latet dashboard`'s
one-time code opened while single sign-on is set up and its settings say `company_changes: "sso"`, which is their
default: 403 `{error, code: "company-sign-in"}`, before the body is read. A read never is, but the policy's text
(`GET /ee/policy/text`), which holds the company's terms and values in clear, and, read as it is, the synced terms
gone from their source and the term sources (`GET /ee/term-sync/gone`, `GET /ee/term-sync/sources`, below). The
server's command line
reaches the same routes but `/ee/sign-in/code`, the Policy page's editor (`latet server policy` has its own routes)
and the key's space under `/_latet/server/v1/admin/ee/…` with its administration token.

```ts
type ConnectedNotice = {              // /me's edition.connected: absent when there is nothing to tell
  until: number                       // the newest entry told: what POST /ee/notices/told sends back
  computers: { label: string | null; connected: number }[]     // each computer connected as the person (null: disconnected since)
  codes: { made: number; by_administrator: boolean }[]         // each one-time code made for them
  more: number                        // the codes they made themselves left out beyond the newest 40
}
// Told for 14 days after each entry, or until the person says it seen from a session the company sign-in opened;
// without single sign-on it cannot be said seen and goes after 14 days. Never a token or a code.

type ComputerOfYours = {              // /me's edition.computers: each computer connected to the person's account now
  label: string                       // the name it was connected with ("" for none)
  connected: number                   // epoch seconds
  last_used: number | null            // its last call to the server; null: none yet
  by_administrator: boolean | null    // whether an administrator made the code it was connected with (null: not said)
}
// Newest first, always listed (the notice above goes after 14 days). The person's own; never a token or its hash.
```

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
  identity: { enabled: boolean; usernames: string[]; hostname: string | null; hostnames: string[]; full_name: string | null; full_name_from: "provider" | null; full_name_locked: boolean; emails: string[] }
  // full_name_from (read-only): "provider" when full_name is the name the company's identity provider gives (Latet
  // Enterprise: a person linked to it, under `exceptions: company`, their settings letting names through); null when
  // full_name is the person's own. full_name_locked (read-only): whether a PUT that changes full_name is refused
  // (409 {error, floor}): true whenever the provider names the person (linked, the company letting names through),
  // whichever name is in use, so also with full_name_from null when their own settings mask names and their own name
  // stays in use, masked, and is not theirs to change; false otherwise, always on a computer and in the Community
  // Edition. The dashboard's Rules page says it under the full name.
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
  unknown_members: "check" | "refuse"     // a request member no request profile names: read and checked, or refused
                                          // (0.15.0); your own value, what a save sends back: a company's policy that
                                          // locks `refuse` raises it in use (GET /ee/policy says so)
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

type LicenseSummary = {               // /me's edition.license: null without a license file
  state: "valid" | "expiring" | "grace" | "paused" | "trial-ended" | "invalid"   // trial-ended: settings read, not changed; what decides stops, never what protects
  licensee: string | null; users: number | null; users_in_use: number | null   // in use: told to whoever has `license.manage` on a server (the administrators), null for anyone else
  expires: string | null; days_left: number | null; trial: boolean            // days: YYYY-MM-DD, UTC; trial: the file's kind
  notices: ("expiring" | "grace" | "paused" | "trial-week" | "trial-day" | "trial-ended" | "users-near" | "users-over" | "invalid")[]
  pauses_on: string | null            // while a grace runs: the day reports, events and term sync pause (a trial has none); for anyone on a server without `license.manage`, only the day the license's end brings, never the users' grace's
}
type LicenseStanding = {              // GET /ee/license
  state: "none" | LicenseSummary["state"]
  license: { id: string; licensee: string; users: number; features: string[]; issued: string; expires: string; trial: boolean } | null
  users_in_use: number | null; days_left: number | null; over_since: number | null   // on a server, users_in_use and over_since are told to whoever has `license.manage` and are null for anyone else
  pauses_on: string | null      // for anyone on a server without `license.manage`, only the day the license's end brings: null when it would derive from the users' grace
  reason: string | null; reason_code: string | null; notices: LicenseSummary["notices"]   // reason_code: a LicenseError code
  source: "server" | null
}
// An Enterprise route of a later part answers 403 {error, feature} while the license does not switch its feature on;
// a route that reads what protects answers only GET and HEAD (405 to any other method, whatever the standing);
// a change of Enterprise settings (any method but GET and HEAD) answers 403 {error, code: "trial-ended", feature} once
// a trial ended, and 403 {error, code: "not-manageable", feature} while the license is paused or its file cannot be
// used (their GET still answers); what lets people in or decides answers 403 {error, code: "trial-ended", feature} to
// every method once a trial ended.
```

## Reports, administration log and SIEM (Enterprise, on a server, src/latet/ee/events/api.py)

Shown when `me.edition.pages` has `reports` (the Reports page and its SIEM view) or `audit` (the administration log),
which E3's roles decide (`reports.group`, `audit.read`; the SIEM outputs `reports.all` to read, `siem.edit` to change).
Counts only: never a value, a text, a fake, a path or a session key. The same routes serve `latet server reports|siem`
under `/_latet/server/v1/admin/ee/`, with two more of the administration's alone (`/ee/reports/settings` and
`/ee/siem/skip`). An export, a change and a test are recorded in the administration log (`reports.exported`,
`audit.exported`, `siem.changed`, `siem.secret.changed`, `siem.tested`; `reports.changed` and `siem.skipped` by
`latet server`; `siem.changed` names each output with its origin, never its path or query; a test's attempt is
recorded before the address is reached) and, while the sign-in settings say `company_changes: sso`, take a session
opened with the company sign-in (403 `company-sign-in`), weighed before anything is read or reached. An export is
given only while the roles its own transaction finds still allow what it shows (403 otherwise). A person has one report
or export under way at a time (429), and an instance one export until its answer is sent (429). What produces nothing while the license
does not answers 403 `{error, code: "not-producing", paused: {why: expired|users|trial-ended|unusable, expires}}`.
Refusals are in fixed words: they never quote what the request sent, an address or a secret.

| Method | Path | Body | Answer |
| --- | --- | --- | --- |
| GET | `/ee/reports?from=YYYY-MM-DD&to=&group=&project=&agent=&category=` | | `Report`: `period {from, to, days, step: day\|week}`, `previous`, `filters`, `people`, `totals {exchanges, stopped, masked, approvals: {outcome: n}, changes: {outcome: n}, apps: {operation: n}, uncounted}`, `before`, `series [{key, exchanges, stopped, masked}]`, `by_group`, `by_project`, `by_agent`, `by_key`, `by_person` (null by group only) `[{key, label, exchanges, stopped, masked}]`, `by_approver` (null by group only) `[{key, approvals, changes}]`, `by_category [{key, masked}]`, `stopped_by_reason [{key, count}]`, `options {groups [{id, name}], projects, agents, categories}`, `others` (what each list leaves out), `viewer {groups: string[]\|null, people, siem: edit\|read\|null}`. 400: dates or a filter; 403: not theirs, `not-producing`; 429 while another of the person's is made |
| GET | `/ee/reports/export?format=csv\|json&…` | | `{name, type, file}` (CSV with a BOM, a row a day for each group, kind, name, project and agent; JSON with `administration` for `audit.read`, or `administration_truncated: true` past 5,000 entries); 403 `missing X-Latet-UI header` without the header, `company-sign-in`, or roles changed while it was made; 413 past 500,000 rows; 429 while this instance makes another export, or the person another report or export |
| GET | `/ee/audit?from=&to=&action=&actor=` | | `{period, entries: [{id, ts, action, actor {id, name, kind: person\|key\|token\|scim\|server\|unknown\|null}, target {id, name}\|null, details, address}], actions, actors, truncated, addresses}` (the newest 5,000 of the period, `truncated` past them; `address` null, and `addresses` false, for whoever is no administrator); 403 without `audit.read` |
| GET | `/ee/audit/export?format=csv\|json&…` | | `{name, type, file}`: every entry of the period, oldest first; 403 `missing X-Latet-UI header` without the header, `company-sign-in`, or roles changed while it was made; 413 past 100,000 entries; 429 as a report's export |
| GET | `/ee/siem` | | `{outputs: [{name, kind, url, headers, format, secret, has_secret, secret_destination_changed, queued, dropped, last_ok, last_error, failing_since, blocked, held, collapsed}], etag, editable, paused}` (`secret`: its secret's name, never the secret; `held`: the queue waits at what its receiver refuses, which `latet server siem skip` passes; `collapsed`: the entries a gap marker at its head stands for); a reader gets the address's origin alone, the headers' values as `…` and no `etag` |
| PUT | `/ee/siem` | `{outputs: SiemOutput[], secrets?: {name: string \| null}, etag?}` | as GET; 400 `{error, details}` (a credential in place of `{secret}`, an address the server never reaches), 403 `{error, code: "by-command"}` (an output at `http://` or this computer changed, or a secret given for one, set or removed: `latet server siem set`'s and `latet server siem secret`'s), 409 `siem-changed`, 429 past ten destinations added or moved an hour by a person, 503 when they cannot be counted |
| PUT | `/ee/siem/secret` | `{name, secret: string \| null}` | `{ok}`, the secret sealed for where the output sends; 400; 403 `{error, code: "by-command"}` for an output at `http://` or this computer, whether the secret is set or removed (`latet server siem secret`'s alone); 404 |
| POST | `/ee/siem/test` | `{name}` | `{ok, detail}`; 400 (its address is one the server no longer reaches); 403 (an output at `http://` or this computer: `latet server siem test`'s); 404; 429 past one an output every 10 s, 30 an output and 30 a person an hour |
| GET, PUT | `/ee/reports/settings` (the administration's alone) | `{people?, retention_days?}` | `{people, retention_days}`; 400 |
| POST | `/ee/siem/skip` (the administration's alone) | `{name}` | `{ok, from, to, count, dropped}`; 404; 409 while another instance sends to it, or when its receiver refuses nothing at its head |

## Company approvals (Latet Enterprise, on a server, src/latet/ee/approvals_api.py)

```ts
// For the people who decide something (`company-approvals` in /me's edition.pages): the security team, an API key's
// controllers, the admins; 403 for anyone else; refused inside an API key's space. Any instance answers. A decision
// needs a session the company's sign-in opened while company_changes says sso (403 company-sign-in), and is refused
// once a trial ended (403 trial-ended), as a text and its images are (read to decide on them); the lists, the count
// and the history answer in every standing. The page asks the queue every 4 s while it is open (15 s in a background
// tab) and the count every 30 s otherwise (60 s), never both.
type Asker = { id: string; name: string; kind: 'person' | 'key' }
type Asked = { id: string; identity: Asker; group: string | null; route: 'security' | 'controllers'; created: number
               mine: boolean }                       // mine: you asked for it (withdraw it, never approve it: 403 yours)
type CompanyChange = Asked & { source: 'command' | 'file' | 'start' | 'dashboard'; agent: string | null
                               loosened: string[]; keys: string[]            // what it lowers, never a value; past 100
                                                                             // reasons, a last line "N more"
                               asked_by?: { id: string; name: string | null } } // who made it in an API key's space
type CompanyBatchRow = Asked & { agent: string; kinds: string[]; items: number } // never the text
// GET /ee/approvals → {changes: CompanyChange[], batches: CompanyBatchRow[], more: number}  (oldest first, 200 at most;
//   more: what else waits for you to decide past them, your own requests left out: listed as these are decided)
// GET /ee/approvals/count → {waiting: number}   (the menu's count while the page is closed; your own left out)
// GET /ee/approvals/batches/{id} → Batch & Asked & {version: string}   (the text, values included; 403 not-yours; 409
//   gone when it no longer waits; 403 company-sign-in from any session the company's sign-in did not open, whatever
//   company_changes says). version: a digest of how the text would leave (each masked text, and where its values
//   sit; never a value in clear), which changes whenever its space masks it again while it waits (its rules or vault
//   changed): an approval names the version it was read at
// GET /ee/approvals/batches/{id}/images/{item}?view=original|sent   (the same)
// GET /ee/approvals/history?limit=50 → {items: [{id, kind, route, identity, created, decided, state, by: {id, name} | null,
//   source?, loosened?, keys?, asked_by? (a change), agent?, kinds?, items? (a text)}]}   (newest first; never a text;
//   state: approved | rejected | stale | superseded | withdrawn | expired | abandoned | gone | approved-unapplied |
//   approved-changed (a text approved, withheld where it was sent: masked then, it was not what was approved);
//   by: null when nobody decided (withdrawn by whoever asked, expired, given up, gone, replaced), and for an end found
//   in a queue after the fact, whose decider the record did not hear)
// POST /ee/approvals/changes/{id} {action} → {ok, state}; 403 yours (your own approval) or not-yours (not, or no longer,
//   yours to decide); 409 gone or stale, approved-unapplied, or {error, floor: string[], code: below-floor} below the
//   company's floor (rejected)
// POST /ee/approvals/batches/{id} {action, masks?, images?, version?} → {ok, state}; an approval names `version` (400
//   without it; a rejection needs none); 403 (company-sign-in as its read, yours, not-yours); 409 changed when the
//   text was masked again since that version was read: nothing decided, and the page reads it again and shows it
//   before anything is approved. Found at the check before the decision is weighed, nothing is recorded; masked again
//   between that check and the decision (another instance), the administration log's entry stands as the decision
//   taken, as for a mask refused in the same window. 409 gone; 422 {error} for a mask refused (no code: its words name
//   the mask's problem). Where the text is sent, it leaves only while masking it with the rules of then gives the
//   version approved: a change of the rules or the vault between the approval and the sending withholds it (the agent
//   is told; sent again, it waits for the deciders again), and the history says approved-changed
// A decision whose administration-log entry cannot be written: 503 unrecorded, nothing decided; the space it waits in
//   not reached on this instance: 503 unreached; its settings not kept: 503 unkept; the company's floor, or the
//   asking account, unreadable: 503 floor-unavailable, account-unavailable; an approval its space could not take (its
//   settings changed on another instance meanwhile): 409 approved-unapplied, ended, asked again; an end the server does
//   not expect: 500 unexpected. Every refusal and every decision not taken is {error, code}: the page words it by its code.
// /me's edition.approvals: {changes: 'self' | 'security' | 'controllers', live_review: 'self' | 'security',
//   deciders: string[], deciders_more: number, text_deciders: string[], text_deciders_more: number}
```

## Term sync (Latet Enterprise, on a server, src/latet/ee/sync/api.py)

Shown when `me.edition.pages` has `term-sync`: for whoever `terms.manage` allows (the security role), while the
license lists `term-sync`; nobody reads it in the dashboard while the license has no `roles` (`ROLES_ONLY`: `latet
server terms` does). One rule of `ee/access.py` covers every route below (`/ee/term-sync`, `terms.manage` to read and
to change). The routes manage Enterprise settings: they are read in every standing, and changed only while the
license can manage (403 `code: "trial-ended"` or `"not-manageable"`: past its grace, too many users, a file that
cannot be used). Every change, while the sign-in settings say `company_changes: sso`, takes a session the company's
sign-in opened (403 `company-sign-in`, before the body is read), and so do the two reads that show what the company
holds: the terms gone from their source and the sources' details (addresses, tenants, DNs, filters, exclusions).
Each change and its entry in the administration log are one transaction, by the person (`terms.source.changed` with
the names of the sources added, removed and changed and of the fields changed; `terms.secret.changed` naming the
source; `terms.sync.asked`; `terms.removed` with counts by type): never a term, a secret, an address, a path, a DN or
a filter, in an entry or a refusal, whose words are fixed. The same routes serve `latet server terms` under
`/_latet/server/v1/admin/ee/` (below), where `remove` also takes `{source}`, `{all: true}` and `{unreadable: true}`.

| Method | Path | Body | Answer |
| --- | --- | --- | --- |
| GET | `/ee/term-sync` | | `{licensed, paused, masked, kept, most, gone, unreadable, behind: {loaded, store, seconds} \| null, problems, sources: [{name, kind, every_minutes, configured, terms, by_type, gone, asked, last: Run \| null, next}]}` (`Run`: `{source, kind, started, finished, outcome: ok\|failed, code, reason, found, added, gone, skipped}`, `reason` in Latet's words and codes, never a record's); never a term; `problems` as the sources' `PUT` says them, the secrets to set again aside (terms.manage) |
| GET | `/ee/term-sync/gone?source=` | | `{items: [{id, source, type, value, gone}], total}`: the terms no longer in their source, the latest first, at most 1,000 (`total` says how many in all); 400 for a source that is no source's name; 403 `company-sign-in` from a session the company's sign-in did not open while `company_changes: sso`; 503 while the sign-in settings cannot be read (terms.manage) |
| POST | `/ee/term-sync/run` | `{source?}` | 202 `{asked: [names]}`: run at once by whichever instance holds the lease; 404 for no such source; 403 `not-manageable` while the license pauses term sync (past its grace, too many users, a file that cannot be used) or `trial-ended` (terms.manage) |
| POST | `/ee/term-sync/remove` | `{ids}` | `{removed, by_type}`: only terms no longer in their source, by their ids (at most 1,000); 400 for any other body (a source, all, those no key opens: `latet server terms remove`'s); 403 `not-manageable` or `trial-ended` (terms.manage) |
| GET | `/ee/term-sync/sources` | | `{sources, text, etag, where}`: the section as JSON and as YAML, as PostgreSQL has it, never a secret (a source names its secret's place, `{secret}`); `etag` names these sources; `where`: for each source by name, `{addresses, bind}`, where it connects (`scheme://host:port` each, none for a file) and the DN a directory signs in as, which the secret dialog says before it asks for a secret; 403 `company-sign-in` as the gone list; 503 while the sign-in settings cannot be read (terms.manage) [D4] |
| PUT | `/ee/term-sync/sources` | `{text, etag}` or `{sources, etag}` | `{added, removed, changed, fields, problems, etag}` (`fields`: each changed source's changed fields, by name; `problems`: the secrets to set again for a source moved or made to read other data, the sources the instance that answers cannot read (LDAP without the `ldap` extra: it leaves them to the instances that can), and instances that mask no synced term); a source added, changed or taken out forgets its last run and a sync asked for of it, so that it runs at the next look as it is now; 400 with where and what kind of error, never what was given, and for a body without `etag` (the page always sends the one `GET` gave); 409 `sources-changed` when the sources changed since that `etag`; 403 `by-command` for a source at this computer (localhost, 127.0.0.1, [::1]) added, changed or taken out, moved elsewhere or made another kind under its name included (the page may change the others while it keeps that one exactly as it is); 413 past the section's size; 429 past 10 destinations added or moved an hour by the person; 503 when they cannot be counted (terms.manage) [D4] |
| PUT | `/ee/term-sync/secrets/{source}` | `{value, etag}` | `{saved}`: kept sealed for where the source goes and what it reads, and a sync of the source asked for in the same change (`terms.sync.asked`), so that one that failed `no-secret` runs at once; 400 for an empty secret or a body without `etag`; 404 for no such source; 409 for a source that reads with no secret, and `sources-changed` as the sources' `PUT`; 403 `by-command` for a source at this computer; the value never shown again (terms.manage) [D4] |
| DELETE | `/ee/term-sync/secrets/{source}` | `{etag}` | `{removed}` (a secret left by a source taken out too); 400 without `etag`; 404 when none is kept; 409 `sources-changed`; 403 `by-command` for a source at this computer (terms.manage) [D4] |

## Locked rules (Latet Enterprise)

On a server whose license lists `locked-rules`, the server's policy is a floor under every person's settings. The
dashboard reads it through `/me` (`edition.policy`) and `GET /ee/policy`; the server's administrators set it with
`latet server policy set`, and the security role (`policy.edit`) from the Policy page's editor (`GET /ee/policy/text`,
`PUT` and `DELETE /ee/policy`), with the same checks; every other person's Policy page only reads, and once a trial
ended nobody changes the policy from the dashboard.

```ts
type PolicySummary = {                // /me's edition.policy: null without a policy
  sources: { source: "managed" | "server"; origin: string; received: number; notices: number }[]   // notices: how many, never their words
  problems: { level: "warn" | "fail"; text: string }[]
}
type PolicyFloor = {                  // GET /ee/policy, as a person is told it
  source: "managed" | "server"; origin: string   // a server's address, or the file `latet policy check` read
  received: number                               // seconds: when it was set (the policy's etag is the administrators')
  brought: string[]                              // rules-file keys its profile locks without naming them (max: pii, id_regions, include_private_ips, data, images; balanced: pii)
  exceptions: "personal" | "company"             // whose "Not sensitive" values count
  rules: {                                       // a rules file naming only what it locks, without a value:
    latet_rules: 1; profile?: Profile; pii?: string[]; id_regions?: string[]; phone_regions?: string[]
    include_private_ips?: boolean; ner?: "auto" | "off"; data?: "normal" | "strict"; dump_guard?: number
    images?: "allow" | "mask" | "block"; ocr?: "auto" | "off"; policies?: Record<string, "allow" | "mask" | "redact">
    data_tools?: string[]; disabled_secret_rules?: string[]
    terms?: number; allow_values?: number; allow_patterns?: number; data_files?: number; data_hosts?: number   // counts
    recognizers?: string[]; fields?: string[]    // by name (fields: those that mask)
    fields_clear?: number                        // field rules that keep a field in clear: counted
    guidance?: true                              // that it has some, not its text
  }
  notices: number                                // settings in it that were not read: a count
}
```

- `below` names your own settings that are below the policy (raised while in use), as `protection.loosened` says them:
  settings, never a value. `problems`: why a policy is not in force or cannot be read, in fixed words.
- The route answers in every state, a license or none (no license: no floor). It reads nothing per request but what the
  last look left, and it is served in the dashboard's threads.
- A save that goes below the floor answers 409 `{error: "Set by your company, not changed: ...", floor: string[]}`
  from `PUT /config`, `POST /allow`, `POST /terms`, `POST /rules/import`, `PUT|DELETE /projects/{id}/personal`,
  `POST /changes/{id}` and `POST /_latet/admin/reload`: `floor` lists the reasons, which name settings and never a
  value. A change that arrives (the repository's `latet.yaml`, a start) is raised, never refused. While a server's
  policy cannot be read the routes that need the floor answer 503 with fixed words and `Retry-After`; while a person's
  account cannot be read for the name they are masked under, the same 503 with `{error: "your account on this server
  cannot be read now: try again"}`, which the dashboard shows as the server words it.
- `GET /projects/{id}`'s `personal` is your own entry as you set it; its `effective` is the rules in use, raised.

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
               data; data_tools; fields; dump_guard; images; unknown_members }
                                             // data: as it applies there, max's when unset; unknown_members: every
                                             // layer together (a project, or its latet.yaml, may only refuse)
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
  decides?: 'self' | 'security' | 'controllers'
                               // who decides: you (or missing), or someone else with Latet Enterprise
                               // (`live_review: security`: the company's security team); you may stop
                               // such a batch, never approve it: 403
  deciders?: string[]          // who decides, by name: never you; 20 at most, sorted (/me's
                               // `edition.approvals.text_deciders_more` counts the others); [] when
                               // nobody can decide on it yet (it still waits)
}
// GET /approvals → {batches: Batch[], viewers: number}       (oldest first; works with history off)
// GET /approvals/stream (SSE): `snapshot` {batches, viewers} first, then `pending` Batch,
//   `settled` {id, state: approved | rejected | abandoned | expired, by: string | null}, `viewers` {viewers}.
//   `by`: who decided, by name, when it was someone else (Latet Enterprise); null when it was you, and
//   for an end nobody decided (expired, abandoned, or ended where it was found in a queue).
//   `withheld` {id} (Latet Enterprise): a text the company's decider approved, withheld as it left, since masked then
//   it was not what they approved (the settings changed meanwhile); sent again, it waits for them again.
//   Each open stream counts as a dashboard watching (the agent tells you when none is open).
// POST /approvals/{id} {action: 'approve' | 'reject', masks?: [{item, start, end, category}]}
//   masks: offsets in that item's `original`; category one of the local reviewer's categories.
//   → {ok: true}; 409 when it no longer waits; 422 when a mask would not add protection (over a
//   value masked already, a fake in use, spaces around it, fewer than two characters); 403 {error}
//   to approve a batch someone else decides on (`decides` not 'self'; stopping it answers 200). No
//   field of the body names who decided.
// POST /approvals/approve-all {project?: string | null} → {approved: number}
//   every batch waiting for you, as Latet masked it, never one someone else decides on; with
//   `project`, only that project's (null: no project)
// The bridge's first answer to a text that waits (`mask`) is {pending, viewers, decides, deciders}.
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
  source: 'command' | 'file' | 'start' | 'dashboard'
                                              // a latet command; config.yaml edited; found at start; a
                                              // save in the dashboard someone else decides on (below)
  agent: string | null                        // the agent that ran the command (hermes, claude-code...)
  loosened: string[]                          // what it lowers, one sentence each, never a value
  keys: string[]                              // the top-level config.yaml keys it changes
  guidance: {where: string | null; before: string | null; after: string | null}[]
                                              // the reviewer's guidance it writes (where: null global,
                                              // else the project's name); the dashboard only, not the CLI
  destinations: {name: string; before: string | null; after: string | null}[]
                                              // destinations it moves (after: the new address) or
                                              // removes (after: null): where requests and keys would go
  decides?: 'self' | 'security' | 'controllers'
                                              // who decides: you (or missing), or someone else with
                                              // Latet Enterprise (the company's security team, or the
                                              // people who control the API key); you may undo such a
                                              // change, never approve it: 403
  deciders?: string[]                         // who decides, by name: never whoever asked; 20 at most,
                                              // sorted (/me's `edition.approvals.deciders_more` counts
                                              // the others); [] when nobody can decide on it yet
  asked_by?: string | null                    // in an API key's space, the person who made it there
                                              // (who never approves it); null otherwise
  exceptions: (RequestException & {how: string})[]  // the request exceptions it adds, widens, renews or makes
                                              // permanent (below), id null for a new one
}
// At most one waits: a newer change replaces it (`change_settled` with state superseded).
// GET /changes → {changes: SettingsChange[]}
// POST /changes/{id} {action: 'approve' | 'reject'} → {ok: true, state}
//   approve: it applies and is saved; reject: config.yaml goes back to the settings in use.
//   409 {error} when it no longer waits, or when your settings changed since (state stale: dropped).
// On GET /approvals/stream: the snapshot carries `changes` too; events `change` (SettingsChange)
// and `change_settled` {id, state: approved | rejected | stale | superseded | approved-unapplied,
// by: string | null}: `by` names who decided when it was someone else (Latet Enterprise), null when
// it was you or nobody; `approved-unapplied` follows an `approved` decided by someone else that your
// settings, changed meanwhile, did not take (nothing applied: ask for it again).
//
// With Latet Enterprise's central approvals on a server (`edition.approvals.changes` not 'self' in /me: the security
// team, or in an API key's space its controllers): a save in the dashboard that lowers protection (PUT /config, POST
// /allow, POST /terms, POST /rules/import, PUT|DELETE /projects/{id}/personal, DELETE /destinations/{name}) answers 409
// {error, reason: 'settings_waiting_for_others', pending: SettingsChange} and waits for them (in a key's space
// `pending.asked_by` is who made it); POST /changes/{id} {action: 'approve'} on a change someone else decides on
// answers 403 {error} (undo it: 200); the bridge's first answer to a text that waits says `decides` and `deciders`.
// These answers of the person's own routes carry words and a `reason`, no `code`; the deciders' routes, with a `code`
// on each answer, are under Company approvals.
//
// type ApprovalRoutes = {                    // /me's edition.approvals (Latet Enterprise, on a server)
//   changes: 'self' | 'security' | 'controllers'   // who decides on this identity's changes that lower protection
//   live_review: 'self' | 'security'                // who decides on its Live Reviewer's texts
//   deciders: string[]; deciders_more: number       // who decides on the changes (never the person working here,
//                                                   // in an API key's space), 20 at most, and how many more
//   text_deciders: string[]; text_deciders_more: number
//                                                   // who decides on the texts, 20 at most, and how many more
// }                                                // the lists are [] for 'self', and when nobody can decide yet
```

## Request exceptions (P2-7, D3, D4, R-75)

```ts
type RequestException = {
  id: string | null            // Latet's, set as you approve it; null while it waits
  destination: string; profile: string          // where: the route and the request profile it applies to
  scope: "identity" | "project"; project: string | null  // for whom; a project by its folder's name alone
  reason: "destination-credential" | "custom-header" | "query-parameter" | "other"
  components: { kind: "header" | "query" | "body"; name: string; role: "credential" | "checked";
                type: "string" | "number" | "boolean" | "null" | null }[]  // names, never a value
  days: number; permanent: boolean; description: string | null  // your own words, for you alone
  issuer: "owner" | "person" | "administration" | "security" | "controllers" | null  // who approved it: you on
                               // your computer, you on a server, a server's administration (for an API key), your
                               // company's security team or another of an API key's controllers (Latet Enterprise);
                               // null while it waits
  approver: string | null      // with security or controllers, the name of the person who approved it; null for
                               // controllers when a person approved it in the key's space, with no central approvals
  state: "pending" | "needs approval again (profile revised)" | "not approved by the company" | "expired"
         | "expiring" | "active" | "permanent" | "forbidden"  // expiring: 7 days or fewer left; forbidden:
                               // permanent, and the edition forbids it; not approved by the company: Latet
                               // Enterprise's `exceptions: company`, approved by you or an API key's controllers
  ends: string | null; days_left: number | null; granted: string | null  // ISO day; null for a permanent one
}
// GET /exceptions → {items: RequestException[], permanent_allowed: boolean}, judged by the decision's clock
//   (PostgreSQL's on a server).
// DELETE /exceptions/{id} → {ok, rules_version}: taken away at once (it raises protection); 404 when none has that id.
// An exception is added, widened, renewed or made permanent in config.yaml (or by a command): a loosening, it waits for
// you as a SettingsChange, whose `exceptions` lists each one with `how` ("added", "widened", "made permanent",
// "renewed") and the fields above, so that the page says before you approve what it lets through, and that one for the
// whole identity covers every request of yours (R-75).
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

- `GET /me` adds `server: {url, identity: {id, name, kind: "person" | "key"}} | null` (null on a computer) and
  `session: {via: "sso" | "code"} | null`: how this dashboard session was opened, with the company's single sign-on
  (Latet Enterprise) or with the one-time code `latet dashboard` asks for; a session that does not say is a `code` one.
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
| `GET /identities` | Optional `kind=person` or `kind=key`; returns `{identities}` including computer-token counts and tampered-token counts, and `named_by_id`: true for a person SCIM manages, whose name and e-mail a SCIM token sets, so that `latet server` names them by their id alone (since 0.15.2). |
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
| `GET /shared` | Returns the current shared `{settings, etag, kept_rules, edition}` from PostgreSQL: `settings` the keys a server needs to run (`SETTING_KEYS`: upstreams, llm_review, ocr, history, retention_days), `etag` the stored row's, `kept_rules` the rules for everyone an earlier version kept in it (`{}` when none), applied by no space, `edition` the server's edition (`community` or `enterprise`), whose words the CLI refuses a rule key in. An unreadable shared row receives 500. |
| `PUT /shared` | Shared settings keys at the top level with optional `etag` and `forget_kept_rules`; returns `{settings, etag, kept_rules, edition}`. A key left out keeps its value (since 2026-10-20: it used to reset to its default; `{}` changes nothing); a key that is no setting receives 400, counted and never quoted, nothing written. A rule key (`RULE_KEYS`) receives 400 with the edition's words (`rules_for_everyone`: `RULES_FOR_EVERYONE`, or `RULES_IN_THE_POLICY` with the Enterprise Edition, whose rules for everyone are the company's policy) and `details` naming the keys, nothing written. The kept rules are written back with the settings, unless `forget_kept_rules` is true, which deletes them. With an etag, a changed or unreadable row receives 409; oversized shared settings receive 413 without a write. |
| `GET /admin-tokens` | Returns `{tokens}` with id, label, timestamps, issuer, `this` and `tampered`; never a secret or hash. |
| `POST /admin-tokens` | `{label, days?}` or `{label, never: true}`; returns 201 `{id, token, expires}`. `days` is an integer from 1 to 3,650, mutually exclusive with `never`. The new token cannot outlive its issuer; an explicitly longer request receives 403. |
| `DELETE /admin-tokens/{token}` | The listed token id, not its secret; returns `{revoked: true}` or 404 if absent. |
| `GET /log` | Filters `since`, `until`, `actor`, `action`, `limit`, `cursor`; returns `{entries, next_cursor}`. Duplicate filter parameters or an invalid/mismatched cursor receive 400. |
| `GET /log/verify` | Returns `{ok, checked, broken_at, head}`; `head` is null or `{id, hash}`. A failed walk returns `ok: false`, not an unchecked success. |
| `GET /status` | Returns public URL, version, instance, identity counts, active-space count, vault usage, cached audit-chain verdict and database/Valkey status, and `edition`: one `warn` line while the shared settings keep rules for everyone from an earlier version (their counts, never values), `[]` otherwise, and `unknown_members`: `check` or `refuse`, how a request member no request profile names is read for every identity (the shared settings in force raised to the company's policy; an identity's own settings may refuse it too), `null` when it cannot be said now. It is not a check of connected agent installations. |
| `GET /ee/term-sync/sources` | Latet Enterprise: `{sources, text, etag}`, the term sources as PostgreSQL has them, as JSON and as YAML, never a secret (`latet server terms sources show`). |
| `PUT /ee/term-sync/sources` | `{sources, etag?}` or `{text, etag?}`; returns `{added, removed, changed, fields, problems, etag}` (`latet server terms sources set`). A source at this computer is set here alone, never from the dashboard. 400 with where and what kind of error, never what was given; 409 `sources-changed` when the sources changed since `etag`; 413 past the section's size. The entry `terms.source.changed` names sources and fields, never a value. |
| `PUT /ee/term-sync/secrets/{source}` | `{value, etag?}`; returns `{saved}`: the secret kept sealed for where the source goes and what it reads, and a sync of the source asked for (`terms.sync.asked`, before the secret's own entry) (`latet server terms secret`). 400 for an empty secret; 404; 409 for a source that reads with no secret, and `sources-changed`. |
| `DELETE /ee/term-sync/secrets/{source}` | Nothing, or `{etag}`; returns `{removed}`, a secret left by a source taken out too (`latet server terms secret --remove`). 404 when none is kept; 409 `sources-changed`. |

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
