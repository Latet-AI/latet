import { createElement as h, Fragment, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, NetworkError } from '../api/client'
import { FlagBadge, TransportStatus } from '../components/badges'
import { FlowStrip } from '../components/exchange/FlowStrip'
import { ClassLegend } from '../components/exchange/Legend'
import { SlotView } from '../components/exchange/SlotView'
import { OutboundCard, WhyBlocked } from '../components/exchange/WhyBlocked'
import { ReviewerEditor } from '../components/ReviewerEditor'
import { Shell } from '../components/Shell'
import { TutorialElsewhere } from '../components/tutorial/EndTutorial'
import { ErrorState, errorText } from '../components/ui'
import i18n from '../i18n'
import { parseFlags } from '../lib/flags'
import { MeContext, type MeState } from '../lib/me'
import { IntegrationsPage } from '../pages/IntegrationsPage'
import { VaultPage } from '../pages/VaultPage'
import { computer, exchange, reviewer, server, slot, span, tutorial } from './eop35-fixtures'

// Expose only the content selected by real tooltip consumers. This is not evidence that a portal
// opens, remains accessible, or runs an effect; those checks are in the finite browser plan.
vi.mock('../components/Floating', async (original) => ({
  ...await original<typeof import('../components/Floating')>(),
  Tooltip: ({ content, children }: { content: ReactNode; children: ReactNode }) => h(Fragment, null, children, content),
}))

function render(children: ReactNode, me: MeState['me'], unreachable = false): string {
  const value: MeState = {
    me, unreachable, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined,
  }
  return renderToStaticMarkup(h(MemoryRouter, null, h(MeContext.Provider, { value }, children)))
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim()
}

// Semantic boundary checks, not exact-sentence snapshots: universal truthful wording is allowed.
// Client restart/apply advice is checked separately, so this guard is only used for storage,
// processing and stopped-request statements which cannot truthfully locate data on the browser PC.
function noBrowserLocationClaim(html: string): void {
  const words = text(html)
  expect(words.length).toBeGreaterThan(15)
  expect(words).not.toMatch(/(?:on|from|outside|leave|left|leaves|leaving)\s+(?:(?:this|your|the)\s+)?(?:computer|machine)/i)
  expect(words).not.toMatch(/never\s+(?:reached|reaches)\s+the\s+network/i)
  expect(words).not.toMatch(/\b(?:this|your)\s+(?:computer|machine)['’]s|on your own server/i)
}

beforeEach(async () => { await i18n.changeLanguage('en') })

describe.each(['person', 'key'] as const)('EOP35 actual consumers: server %s', (kind) => {
  const me = server(kind)

  it('renders an administrator-owned reviewer without endpoint, key or trust inputs', () => {
    const html = render(h(ReviewerEditor, { value: reviewer, onChange: () => undefined }), me)
    expect(html).toContain('data-server-reviewer')
    expect(html).toContain(reviewer.url)
    expect(html).not.toContain('type="password"')
    expect(html).not.toContain(`value="${reviewer.url}"`)
    noBrowserLocationClaim(html)
    expect(text(html)).not.toMatch(/another machine of yours|operating system.{0,20}credential store/i)
    // No key-read absence claim here: SSR does not run ReviewerKeyField's effect.
  })

  it('does not say the reviewer has a key: the server answers the same whether its administrator set one or not', () => {
    const words = text(render(h(ReviewerEditor, { value: reviewer, onChange: () => undefined }), me))
    expect(words).toContain(i18n.t('reviewer.key.server'))
    expect(i18n.t('reviewer.key.server')).toMatch(/^If the reviewer needs a key\b/)
  })

  it.each(['blocked', 'image', 'outbound:WebSearch', 'uninspected:image'])('selects truthful %s tooltip content', (raw) => {
    const flag = parseFlags([raw])[0]!
    const html = render(h(FlagBadge, { flag }), me)
    expect(text(html).length).toBeGreaterThan(20)
    noBrowserLocationClaim(html)
    if (raw === 'uninspected:image') expect(text(html)).not.toMatch(/sent as they are/i)
  })

  it.each(['IDENTITY', 'LLM', 'NER'] as const)('selects truthful %s class help at the Legend consumer', (cls) => {
    noBrowserLocationClaim(render(h(ClassLegend, { values: [span(cls)] }), me))
  })

  it('does not turn a plugin stop into a zero-network claim', () => {
    const detail = exchange(['blocked', 'leak'], null, 'plugin')
    noBrowserLocationClaim(render(h(TransportStatus, { summary: detail.summary }), me))
    noBrowserLocationClaim(render(h(WhyBlocked, { detail }), me))
  })

  it('describes the reported connected-agent identity in the auto-mode review card', () => {
    const detail = exchange(['blocked', 'leak', 'auto-mode-review'])
    detail.leaks = [{ type: 'USERNAME', kind: 'system', preview: 'USERNAME[5]', new: true, detail: 'host_name' }]
    const html = render(h(WhyBlocked, { detail }), me)
    expect(html).toContain('why-title')
    noBrowserLocationClaim(html)
  })

  it.each([
    ['history', ['blocked', 'leak', 'history'], null, 'plugin'],
    ['leak', ['blocked', 'leak'], null, 'http'],
    ['uninspected', ['blocked', 'uninspected:image'], null, 'http'],
    ['imagesSetting', ['blocked', 'uninspected:image'], 'images: block is set', 'http'],
    ['imagesMask', ['blocked', 'uninspected:image'], 'images: mask is set', 'http'],
    ['localReviewer', ['blocked'], 'the local reviewer could not read the user text', 'http'],
    ['live', ['blocked'], 'Latet did not send this request: you did not approve it in the Live Reviewer', 'http'],
    ['liveExpired', ['blocked'], 'Latet did not send this request: it waited longer than live_review.timeout_s', 'http'],
    ['liveAbandoned', ['blocked'], 'Latet did not send this request: the agent stopped waiting for it', 'http'],
    ['other', ['blocked'], 'masking failed', 'http'],
  ] as const)('keeps the %s WhyCard within the provider-forwarding boundary', (_name, flags, reason, transport) => {
    const html = render(h(WhyBlocked, { detail: exchange([...flags], reason, transport) }), me)
    expect(html).toContain('why-title')
    noBrowserLocationClaim(html)
    expect(text(html)).not.toMatch(/latet llm (?:status|install)|images:\s*allow/i)
  })

  it.each([true, false])('renders the separate outbound card (blocked=%s) truthfully', (blocked) => {
    const detail = exchange(['blocked', 'outbound:WebSearch'], null, 'plugin')
    noBrowserLocationClaim(render(h(OutboundCard, { detail, blocked }), me))
  })

  it('labels the original pane and recorded flow without claiming browser-local storage', () => {
    const pane = render(h(SlotView, { slot, index: 0, marks: { original: [], sent: [], values: [] }, footnotes: false }), me)
    expect(pane).toContain('Example text')
    noBrowserLocationClaim(pane)
    noBrowserLocationClaim(render(h(FlowStrip, { detail: exchange(['blocked']) }), me))
  })

  it('gives the actual Vault header a server storage boundary even before its listing arrives', () => {
    const html = render(h(VaultPage), me)
    expect(html).toContain(i18n.t('vault.title'))
    noBrowserLocationClaim(html)
  })

  it('does not describe the Integrations header as a scan of this computer', () => {
    noBrowserLocationClaim(render(h(IntegrationsPage), me))
  })

  it('does not label the server Shell a local privacy firewall', () => {
    expect(text(render(h(Shell, null, h('main')), me))).not.toMatch(/local privacy firewall/i)
  })

  it('retains known server reviewer restrictions after an ordinary reload failure', () => {
    const html = render(h(ReviewerEditor, { value: reviewer, onChange: () => undefined }), me, true)
    expect(html).toContain('data-server-reviewer')
    expect(html).not.toContain('type="password"')
  })

  it('directs the separate local tutorial to a connected computer explicitly', () => {
    const html = render(h(TutorialElsewhere), me)
    expect(text(html)).toMatch(/(?:your(?: own)?|connected) computer/i)
    expect(text(html)).toContain('latet tutorial')
  })
})

describe('EOP35 unknown and computer branches', () => {
  it.each([false, true])('defers local reviewer controls when /me has no successful answer (unreachable=%s)', (unreachable) => {
    const html = render(h(ReviewerEditor, { value: reviewer, onChange: () => undefined }), undefined, unreachable)
    expect(html).not.toContain('type="password"')
    expect(html).not.toContain(`value="${reviewer.url}"`)
    expect(text(html)).not.toMatch(/credential store|another machine of yours/i)
  })

  it.each([
    ['the reviewer', () => h(ReviewerEditor, { value: reviewer, onChange: () => undefined })],
    ['the Vault header', () => h(VaultPage)],
    ['the Integrations page', () => h(IntegrationsPage)],
  ] as const)('says that %s loads while /me is on its way, and to retry only once it failed', (_name, element) => {
    const retry = i18n.t('deployment.pending')
    const loading = text(render(element(), undefined, false))
    expect(loading).toContain(i18n.t('common.loading'))
    expect(loading).not.toContain(retry)
    expect(text(render(element(), undefined, true))).toContain(retry)
  })

  it('keeps the provider-less Shell/ErrorState fallback of a server page free of a computer\'s commands, as RouteError needs', () => {
    // The production RouteError is private and creates its router on import. Render its exact
    // Shell/ErrorState composition without importing App or inventing a provider in that fallback.
    vi.stubGlobal('location', { hostname: 'latet.acme.lan' })
    try {
      const html = renderToStaticMarkup(h(MemoryRouter, null,
        h(Shell, null, h(ErrorState, { error: new NetworkError('synthetic offline') }))))
      expect(html).toContain('role="alert"')
      expect(text(html)).not.toMatch(/local privacy firewall|latet (?:start|status)/i)
      expect(text(html)).toContain(i18n.t('errors.networkServer'))
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps local endpoint and key controls for a successful computer /me descriptor', () => {
    const html = render(h(ReviewerEditor, { value: reviewer, onChange: () => undefined }), computer)
    expect(html).not.toContain('data-server-reviewer')
    expect(html).toContain('type="password"')
    expect(html).toContain(`value="${reviewer.url}"`)
  })

  it('retains the sandbox navigation for a successful tutorial descriptor', () => {
    const html = render(h(Shell, null, h('main')), tutorial)
    expect(html).toContain('href="/tutorial"')
    expect(html).not.toContain('data-server-reviewer')
  })
})

// A computer's dashboard answers only on this computer's own names (the daemon's Host allowlist), so the words for
// a failure that comes before any `/me` answer (or with none to hand: a toast, an inline error) follow the page's
// address: a computer's say to check the daemon, a server's to whoever manages the service.
describe('what a failure says, by the address of the page', () => {
  const translate = i18n.t.bind(i18n)
  const offline = new NetworkError('synthetic offline')
  const gateway = new ApiError(502, 'Bad Gateway')

  afterEach(() => { vi.unstubAllGlobals() })

  it.each(['127.0.0.1', 'localhost', '[::1]'])('tells a person on their own computer (%s) to check the daemon', (hostname) => {
    vi.stubGlobal('location', { hostname })
    expect(errorText(translate, offline)).toBe(i18n.t('errors.network'))
    expect(errorText(translate, offline)).toMatch(/`latet status`.*`latet start`/)
    expect(errorText(translate, gateway)).toBe('The daemon answered 502: Bad Gateway')
    const html = render(h(Shell, null, h('main')), undefined, true)
    expect(text(html)).toContain(i18n.t('shell.unreachable'))
    expect(text(html)).not.toContain(i18n.t('shell.unreachableServer'))
  })

  it('tells a person on a server\'s page to try again, then to ask whoever manages the service', () => {
    vi.stubGlobal('location', { hostname: 'latet.acme.lan' })
    expect(errorText(translate, offline)).toBe(i18n.t('errors.networkServer'))
    expect(errorText(translate, offline)).not.toMatch(/latet (?:start|status)|daemon/i)
    expect(errorText(translate, gateway)).toBe('The Latet server answered 502: Bad Gateway')
    const html = render(h(Shell, null, h('main')), undefined, true)
    expect(text(html)).toContain(i18n.t('shell.unreachableServer'))
    expect(text(html)).not.toMatch(/daemon/i)
  })

  it('says a computer\'s words where there is no page at all (the tests\' renderer)', () => {
    expect(errorText(translate, offline)).toBe(i18n.t('errors.network'))
  })
})
