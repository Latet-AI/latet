import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, test } from 'vitest'
import type { ExchangeDetail, ExchangeSummary } from '../../api/types'
import i18n from '../../i18n'
import { parseFlag } from '../../lib/flags'
import { coverageOutcome, projectBinding, refusalCategory, remedyFor, sessionLaunch, showsVerified } from '../../lib/coverage'
import { Verdict } from '../../pages/ExchangePage'
import { CoverageList, LAUNCH_LABEL, PASS_LABEL } from './CoverageList'

// The request-coverage plan's P2-7: what the exchange page says of what Latet read of a request and what let it
// through, from the history's safe summary alone. Mock data; the page as it loads is in scripts/ui-checks.mjs.

const SECRET = 'sk-ant-api03-RAWVALUE-7731'

function summary(over: Partial<ExchangeSummary> = {}): ExchangeSummary {
  return {
    id: 'ex_1',
    ts: 1_800_000_000,
    session: 'hg:l1:abc',
    agent: 'claude-code',
    agent_version: '2.1.312',
    upstream: 'anthropic',
    path: '/{destination}/v1/messages',
    protocol: 'anthropic',
    mode: 'rewrite',
    transport: 'http',
    model: 'claude-sonnet-4-5',
    status: 200,
    latency_ms: 900,
    overhead_ms: 12,
    tokens: null,
    masked: 2,
    new_masked: 1,
    by_type: { EMAIL: 2 },
    new_by_type: { EMAIL: 1 },
    restored: 2,
    flags: [],
    verified: true,
    block_reason: null,
    project: null,
    coverage: 'complete-accounting',
    launch: null,
    passes: [],
    profile: 'anthropic.messages.1',
    removed: { headers: 0, members: 0 },
    ...over,
  }
}

function render(of: ExchangeSummary): string {
  return renderToStaticMarkup(h(MemoryRouter, null, h(CoverageList, { summary: of })))
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim()
}

const say = (key: string, options?: Record<string, unknown>) => i18n.t(key, options).replace(/\s+/g, ' ')

describe('a request stopped', () => {
  test('before it was sent says nothing of it reached the provider', () => {
    const shown = text(render(summary({ flags: ['blocked'], block_reason: 'envelope-route', overhead_ms: null, status: 400 })))
    expect(i18n.exists('coverage.blocked.before')).toBe(true)
    expect(shown).toContain(say('coverage.blocked.before'))
    expect(shown).not.toContain(say('coverage.blocked.attempted'))
  })

  test('after an attempt never says nothing was sent: an earlier attempt may have reached the provider', () => {
    const shown = text(render(summary({ flags: ['blocked'], block_reason: 'credentials', overhead_ms: 40, status: 400 })))
    expect(shown).toContain(say('coverage.blocked.attempted'))
    expect(shown).not.toContain(say('coverage.blocked.before'))
  })

  test('is said in the reader\'s language from its safe reason code', async () => {
    try {
      for (const lang of ['it', 'de', 'fr', 'es']) {
        await i18n.changeLanguage(lang)
        const shown = text(render(summary({ flags: ['blocked'], block_reason: 'envelope-header-unknown', overhead_ms: null })))
        expect(shown).toContain(say('coverage.refusal.header'))
        expect(i18n.t('coverage.refusal.header')).not.toBe(i18n.getFixedT('en')('coverage.refusal.header'))
      }
    } finally {
      await i18n.changeLanguage('en')
    }
  })

  test('offers an exact exception only for a header or a query parameter no profile names', () => {
    for (const code of ['envelope-header-unknown', 'envelope-query-unknown']) {
      expect(remedyFor(refusalCategory(code))).toBe('exception')
      expect(text(render(summary({ flags: ['blocked'], block_reason: code, overhead_ms: null })))).toContain(say('coverage.remedy.exception'))
    }
    expect(remedyFor(refusalCategory('envelope-exception-expired'))).toBe('renew')
  })

  test.each([
    ['credentials', 'auth'],
    ['envelope-credential', 'auth'],
    ['egress-refused', 'egress'],
    ['envelope-escape', 'parse'],
    ['proof-incomplete', 'parse'],
    ['coverage-limit', 'limit'],
    ['too-large', 'limit'],
    ['busy', 'limit'],
    ['envelope-route', 'route'],
    ['envelope-sensitive', 'sensitive'],
  ])('never offers an exception for %s (%s)', (code, category) => {
    expect(refusalCategory(code)).toBe(category)
    expect(remedyFor(refusalCategory(code))).not.toBe('exception')
    const shown = text(render(summary({ flags: ['blocked'], block_reason: code, overhead_ms: null })))
    expect(shown).not.toContain(say('coverage.remedy.exception'))
    expect(shown).not.toContain(say('coverage.remedy.renew'))
  })
})

describe('a body member no profile names (root, concern 5)', () => {
  const unknown = () => summary({ flags: ['blocked'], block_reason: 'body-unknown-member', overhead_ms: null, status: 400 })

  test('is said from its own code, and points to how an exact exception is approved, offering no action', () => {
    expect(refusalCategory('body-unknown-member')).toBe('body-member')
    expect(remedyFor('body-member')).toBe('exception-docs')
    const html = render(unknown())
    expect(text(html)).toContain(say('coverage.refusal.body-member'))
    expect(text(html)).toContain(say('coverage.remedy.exception-docs'))
    expect(html).toMatch(/href="[^"]*getting-started\/upgrading-to-0-11\/#approving-an-exact-exception"/)
    expect(html).not.toContain('<button')
  })

  test('in every language, to the same section of the upgrade page', async () => {
    const anchors: Record<string, string> = {
      it: 'approvare-uneccezione-esatta',
      de: 'eine-genaue-ausnahme-genehmigen',
      fr: 'approuver-une-exception-exacte',
      es: 'aprobar-una-excepción-exacta',
    }
    try {
      for (const lang of ['it', 'de', 'fr', 'es']) {
        await i18n.changeLanguage(lang)
        const html = render(unknown())
        expect(text(html)).toContain(say('coverage.refusal.body-member'))
        expect(i18n.t('coverage.refusal.body-member')).not.toBe(i18n.getFixedT('en')('coverage.refusal.body-member'))
        expect(decodeURIComponent(html)).toContain(`${lang}/getting-started/upgrading-to-0-11/#${anchors[lang]}`)
      }
    } finally {
      await i18n.changeLanguage('en')
    }
  })
})

describe('coverage not recorded (root, concern 3)', () => {
  test('is said of an older record alone: a WebSocket turn records its own', () => {
    const turn = summary({ transport: 'ws', coverage: 'complete-accounting' })
    expect(coverageOutcome(turn)).toEqual({ kind: 'read' })
    expect(say('coverage.outcome.unknown')).not.toMatch(/WebSocket/)
  })

  test('a check of the plugin\'s, which sends nothing, says nothing of coverage', () => {
    const check = summary({ transport: 'plugin', coverage: 'unknown', flags: [] })
    const shown = text(render(check))
    expect(shown).not.toContain(say('coverage.outcome.unknown'))
  })
})

describe('a request let through', () => {
  test('every part read: a green line, and the verdict may say Verified', () => {
    const plain = summary()
    expect(coverageOutcome(plain)).toEqual({ kind: 'read' })
    expect(text(render(plain))).toContain(say('coverage.outcome.read'))
    expect(showsVerified(plain)).toBe(true)
  })

  test('masked, with a provider\'s opaque item passed unread: named, and never a green Verified', () => {
    const opaque = summary({ masked: 3, passes: ['provider-echo'] })
    const shown = text(render(opaque))
    expect(shown).toContain(say(PASS_LABEL['provider-echo']))
    expect(shown).toContain(say('coverage.outcome.unread'))
    expect(showsVerified(opaque)).toBe(false)
    const verdict = text(renderVerdict(opaque))
    expect(verdict).not.toContain(say('exchange.verdict.verified'))
    // The final check ran and found nothing in clear: never said to have failed, only that part passed unread.
    expect(verdict).toContain(say('exchange.verdict.partlyUnread'))
    expect(verdict).not.toContain(say('exchange.verdict.notVerified'))
    expect(renderVerdict(opaque)).not.toContain('text-good-ink')
  })

  test.each([['provider-encrypted'], ['image-remainder']] as const)('allowed by rule (%s): said so, never Verified', (pass) => {
    const ruled = summary({ passes: [pass] })
    const shown = text(render(ruled))
    expect(say(PASS_LABEL[pass])).toMatch(new RegExp(say('coverage.allowedByRule')))
    expect(shown).toContain(say(PASS_LABEL[pass]))
    expect(showsVerified(ruled)).toBe(false)
  })

  test('checked only: shown apart from what was read and masked, and nothing left unread', () => {
    const checked = summary({ passes: ['checked-only'] })
    const shown = text(render(checked))
    expect(shown).toContain(say(PASS_LABEL['checked-only']))
    expect(shown).not.toContain(say('coverage.outcome.read'))
    expect(shown).not.toContain(say('coverage.outcome.unread'))
  })

  test('an exception named by category, never by what it let through', () => {
    const shown = text(render(summary({ passes: ['exception'] })))
    expect(shown).toContain(say(PASS_LABEL.exception))
  })

  test('an old record: coverage unknown, said so', () => {
    for (const old of [summary({ coverage: undefined, passes: undefined }), summary({ coverage: 'unknown' })]) {
      expect(text(render(old))).toContain(say('coverage.outcome.unknown'))
    }
    // Nothing writes it any more: an older record's coverage is said as unknown, never as read (review I-3).
    expect(text(render(summary({ coverage: 'legacy-uninspected' })))).toContain(say('coverage.outcome.unknown'))
    expect(showsVerified(summary({ coverage: 'legacy-uninspected' }))).toBe(false)
  })

  test('an image Latet could not inspect is never a green Verified', () => {
    expect(showsVerified(summary({ flags: ['uninspected:image'] }))).toBe(false)
  })
})

describe('launch and project', () => {
  test('a session launched by Latet, its key verified', () => {
    const shown = text(render(summary({ launch: 'launched', project: { id: 'p1', name: 'shop' } })))
    expect(shown).toContain(say(LAUNCH_LABEL.launched))
    expect(shown).toContain(say('coverage.project.trusted', { name: 'shop' }))
  })

  test('"launched by Latet, key missing": said with what it means, and the project as claimed', () => {
    const shown = text(render(summary({ launch: 'launched-key-missing', project: { id: 'p1', name: 'shop' } })))
    expect(say(LAUNCH_LABEL['launched-key-missing'])).toBe('Launched by Latet, key missing')
    expect(shown).toContain('Launched by Latet, key missing')
    expect(shown).toContain(say('coverage.launch.keyMissingHelp'))
    expect(shown).toContain(say('coverage.project.claimed', { name: 'shop' }))
  })

  test('a claimed project is no longer shown as an unknown flag', () => {
    const flag = parseFlag('project-claimed')
    expect(flag.key).toBe('project-claimed')
    expect(i18n.exists('flags.project-claimed.label') && i18n.exists('flags.project-claimed.help')).toBe(true)
    const shown = text(render(summary({ flags: ['project-claimed'], launch: 'claimed', project: { id: 'p1', name: 'shop' } })))
    expect(shown).toContain(say('coverage.project.claimed', { name: 'shop' }))
    expect(shown).not.toContain(say('coverage.project.trusted', { name: 'shop' }))
  })

  test('a session is shown by its least trusted launch', () => {
    expect(sessionLaunch([summary({ launch: 'launched' }), summary({ launch: 'launched-key-missing' })])).toBe('launched-key-missing')
    expect(sessionLaunch([summary({ launch: 'launched' }), summary({ launch: null })])).toBe('launched')
    expect(sessionLaunch([summary({ launch: undefined })])).toBeNull()
  })
})

describe('what its profile removed', () => {
  test('headers and body members, counted', () => {
    const shown = text(render(summary({ removed: { headers: 2, members: 1 } })))
    expect(shown).toContain(say('coverage.removed.headers', { count: 2 }))
    expect(shown).toContain(say('coverage.removed.members', { count: 1 }))
    expect(shown).not.toContain(say('coverage.removed.authAfter', { status: 200 }))
  })

  test('a 401 or 403 that follows a removed header says Latet removed it', () => {
    for (const status of [401, 403]) {
      const html = render(summary({ status, removed: { headers: 1, members: 0 } }))
      expect(html).toContain('data-diagnostic')
      expect(text(html)).toContain(say('coverage.removed.authAfter', { status }))
    }
    expect(render(summary({ status: 401, removed: { headers: 0, members: 2 } }))).not.toContain('data-diagnostic')
  })

  test('nothing is said of a request that never left', () => {
    expect(text(render(summary({ flags: ['blocked'], block_reason: 'envelope-route', removed: { headers: 3, members: 0 } })))).not.toContain(
      say('coverage.removed.headers', { count: 3 }),
    )
  })
})

describe('no raw value', () => {
  test('in the text, the accessibility names or the tooltips, whatever a summary was given', () => {
    const leaky = {
      ...summary({ flags: ['blocked', 'project-claimed'], block_reason: 'envelope-header-unknown', overhead_ms: null }),
      header_value: SECRET,
      sensitive: { block_reason: `header x-team: ${SECRET}` },
    } as unknown as ExchangeSummary
    const html = render(leaky)
    expect(html).not.toContain(SECRET)
    for (const attribute of html.matchAll(/(?:title|aria-label|aria-description)="([^"]*)"/g)) {
      expect(attribute[1]).not.toContain('RAWVALUE')
    }
  })
})

function renderVerdict(of: ExchangeSummary): string {
  const detail: ExchangeDetail = { summary: of, request: { headers: [], slots: [], body_bytes: 10 }, response: null, leaks: [] }
  return renderToStaticMarkup(h(MemoryRouter, null, h(Verdict, { detail })))
}

describe('review round 3', () => {
  test('I-3: an older record, its coverage unknown, is never a green Verified, and says so in its own words', () => {
    for (const old of [summary({ coverage: 'unknown' }), summary({ coverage: undefined, passes: undefined })]) {
      expect(showsVerified(old)).toBe(false)
      const verdict = renderVerdict(old)
      expect(verdict).not.toContain('text-good-ink')
      expect(text(verdict)).toContain(say('exchange.verdict.notRecorded'))
      expect(text(verdict)).not.toContain(say('exchange.verdict.verified'))
      expect(text(render(old))).toContain(say('coverage.outcome.unknown'))
    }
    expect(showsVerified(summary({ coverage: 'legacy-uninspected' }))).toBe(false)
  })

  test("I-3: a check of the Claude Code plugin's is no green Verified either", () => {
    expect(showsVerified(summary({ transport: 'plugin', coverage: 'unknown' }))).toBe(false)
  })

  test('I-6: a part passed under an exception is shown as not read, never as a green Verified', () => {
    const excepted = summary({ passes: ['exception'] })
    expect(showsVerified(excepted)).toBe(false)
    expect(coverageOutcome(excepted)).toEqual({ kind: 'passed', passes: ['exception'], unread: true })
    expect(text(render(excepted))).toContain(say('coverage.outcome.unread'))
    expect(renderVerdict(excepted)).not.toContain('text-good-ink')
  })

  test('I-8: a member no exception can reach is said, and offered nothing', () => {
    const closed = summary({ flags: ['blocked'], block_reason: 'body-unknown-member-no-exception', overhead_ms: null })
    expect(refusalCategory('body-unknown-member-no-exception')).toBe('body-member-closed')
    expect(remedyFor('body-member-closed')).toBe('none')
    const html = render(closed)
    expect(text(html)).toContain(say('coverage.refusal.body-member-closed'))
    expect(text(html)).not.toContain(say('coverage.remedy.exception-docs'))
    expect(html).not.toContain('approving-an-exact-exception')
  })

  test('I-8: on a WebSocket no exception is offered, whatever the code (F-2)', () => {
    for (const code of ['body-unknown-member', 'envelope-header-unknown', 'envelope-query-unknown']) {
      const turn = summary({ transport: 'ws', flags: ['blocked'], block_reason: code, overhead_ms: null })
      const html = render(turn)
      expect(text(html)).not.toContain(say('coverage.remedy.exception'))
      expect(text(html)).not.toContain(say('coverage.remedy.exception-docs'))
      expect(html).not.toContain('approving-an-exact-exception')
      expect(text(html)).toContain(say('coverage.remedy.websocket'))
    }
  })

  test.each(['route', 'resource', 'sensitive', 'auth', 'egress', 'parse', 'limit', 'state', 'masking', 'other', 'body-member-closed'] as const)(
    'Minor 5: never an exception as the remedy for %s',
    (category) => {
      expect(remedyFor(category)).not.toBe('exception')
      expect(remedyFor(category)).not.toBe('exception-docs')
    },
  )

  test('Minor 5 (U9): a claimed project stays claimed whatever the launch says', () => {
    expect(projectBinding(summary({ project: { id: 'p', name: 'shop' }, launch: 'trusted', flags: ['project-claimed'] }))).toBe('claimed')
    expect(projectBinding(summary({ project: { id: 'p', name: 'shop' }, launch: 'launched', flags: ['project-claimed'] }))).toBe('claimed')
  })
})

describe('re-review R-2', () => {
  test('an older record its final check did not confirm keeps the amber Not verified, its coverage still said', () => {
    for (const old of [
      summary({ coverage: 'unknown', verified: false, flags: ['project-off'] }),
      summary({ coverage: undefined, passes: undefined, verified: false }),
    ]) {
      const verdict = text(renderVerdict(old))
      expect(verdict).toContain(say('exchange.verdict.notVerified'))
      expect(verdict).not.toContain(say('exchange.verdict.notRecorded'))
      expect(text(render(old))).toContain(say('coverage.outcome.unknown'))
    }
  })

  test('one it did confirm says so in neutral words, never green', () => {
    const verdict = renderVerdict(summary({ coverage: 'unknown', verified: true }))
    expect(text(verdict)).toContain(say('exchange.verdict.notRecorded'))
    expect(verdict).not.toContain('text-good-ink')
  })
})
