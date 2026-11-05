import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, test } from 'vitest'
import type { ExceptionChange, RequestException, RequestStatus } from '../api/types'
import i18n from '../i18n'
import { expiringSoon, withoutRevoked } from '../lib/exceptions'
import { ExceptionList, ExceptionsAsked, ExceptionStatus } from './RequestExceptions'

// The request exceptions of D3, D4 and R-75 on the dashboard: the list (approved, expiring, expired, revoked, waiting,
// suspended by a profile revision, permanent), the warning before expiry, the change that asks for one with what it
// would let through and for whom, and what their uses are counted as with the history off (M1, D2). Mock data.

const DESCRIPTION = 'our gateway wants the team header, token TOPSECRET-991'

function item(over: Partial<RequestException> = {}): RequestException {
  return {
    id: 'x0123456789',
    destination: 'company',
    profile: 'anthropic.messages.1',
    scope: 'identity',
    project: null,
    reason: 'custom-header',
    components: [{ kind: 'header', name: 'x-company-team', role: 'checked', type: null }],
    days: 30,
    permanent: false,
    description: DESCRIPTION,
    state: 'active',
    ends: '2026-11-30',
    days_left: 26,
    granted: '2026-10-31T10:00:00+00:00',
    issuer: 'owner',
    ...over,
  }
}

function list(items: RequestException[], options: { server?: boolean } = {}): string {
  return renderToStaticMarkup(
    h(MemoryRouter, null, h(ExceptionList, { items, onRevoke: () => {}, busy: null, server: options.server ?? false })),
  )
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim()
}

const say = (key: string, options?: Record<string, unknown>) => i18n.t(key, options).replace(/\s+/g, ' ')

describe('the list', () => {
  test('an approved exception: what it names, where, for whom, until when, and a revoke button', () => {
    const html = list([item()])
    const shown = text(html)
    expect(shown).toContain('x-company-team')
    expect(shown).toContain('company')
    expect(shown).toContain('anthropic.messages.1')
    expect(shown).toContain(say('exceptions.scope.identity'))
    expect(shown).toContain(say('exceptions.state.activeLabel'))
    expect(html).toMatch(/<button[^>]*aria-label="[^"]*x0123456789/)
  })

  test('one expiring within 7 days: warned before it ends, with the days left', () => {
    const soon = item({ state: 'expiring', days_left: 3, ends: '2026-11-07' })
    expect(expiringSoon([item(), soon])).toEqual([soon])
    const html = list([item(), soon])
    expect(html).toMatch(/role="status"/)
    expect(text(html)).toContain(say('exceptions.expiryWarning', { count: 1 }))
    expect(text(html)).toContain(say('exceptions.state.expiring', { count: 3 }))
  })

  test('one expired: said with its day, no longer in force', () => {
    const shown = text(list([item({ state: 'expired', ends: '2026-10-01', days_left: 0 })]))
    expect(shown).toContain(say('exceptions.state.expiredLabel'))
  })

  test('revoked: taken out of the list, and only that one', () => {
    const other = item({ id: 'x00000000ab' })
    expect(withoutRevoked([item(), other], 'x0123456789')).toEqual([other])
  })

  test('waiting for approval: no revoke button, a way to the approval', () => {
    const html = list([item({ id: null, state: 'pending', ends: null, days_left: null, granted: null })])
    expect(text(html)).toContain(say('exceptions.state.pending'))
    expect(html).not.toContain('<button')
    expect(html).toContain('href="/approvals?tab=settings"')
  })

  test('suspended by a profile revision: needs approval again, and its profile is shown', () => {
    const shown = text(list([item({ state: 'needs approval again (profile revised)' })]))
    expect(shown).toContain(say('exceptions.state.revised'))
    expect(shown).toContain('anthropic.messages.1')
  })

  test('permanent: always marked so, with no end', () => {
    const html = list([item({ permanent: true, state: 'permanent', ends: null, days_left: null })])
    expect(html).toContain('data-permanent')
    expect(text(html)).toContain(say('exceptions.permanent'))
    const forbidden = list([item({ permanent: true, state: 'forbidden', ends: null, days_left: null })])
    expect(text(forbidden)).toContain(say('exceptions.state.forbidden'))
    // A permanent one the edition does not allow is still marked permanent (review Minor 5, U14).
    expect(forbidden).toContain('data-permanent')
  })

  test('a project\'s exception, by its name, for requests whose project Latet trusts', () => {
    const shown = text(list([item({ scope: 'project', project: 'shop' })]))
    expect(shown).toContain(say('exceptions.scope.project', { name: 'shop' }))
  })

  test('on a server: yours alone, no one else\'s apply to your requests', () => {
    expect(text(list([item()], { server: true }))).toContain(say('exceptions.yoursAlone'))
    expect(text(list([item()]))).not.toContain(say('exceptions.yoursAlone'))
  })

  test.each([
    ['owner', 'exceptions.issuer.owner'],
    ['person', 'exceptions.issuer.person'],
    ['administration', 'exceptions.issuer.administration'],
  ] as const)('says who approved it (%s), by its category', (issuer, key) => {
    const shown = text(list([item({ issuer })]))
    expect(i18n.exists(key)).toBe(true)
    expect(shown).toContain(say(key))
  })

  test('says nothing of who approved one that waits', () => {
    const shown = text(list([item({ id: null, state: 'pending', issuer: null, ends: null, days_left: null, granted: null })]))
    for (const key of ['exceptions.issuer.owner', 'exceptions.issuer.person', 'exceptions.issuer.administration']) {
      expect(shown).not.toContain(say(key))
    }
  })

  test('in every language', async () => {
    try {
      for (const lang of ['it', 'de', 'fr', 'es']) {
        await i18n.changeLanguage(lang)
        expect(text(list([item()]))).toContain(say('exceptions.scope.identity'))
        for (const key of [
          'exceptions.title',
          'exceptions.scope.identity',
          'exceptions.change.identityWarning',
          'exceptions.issuer.owner',
          'exceptions.issuer.administration',
        ]) {
          expect(i18n.t(key)).not.toBe(i18n.getFixedT('en')(key))
        }
      }
    } finally {
      await i18n.changeLanguage('en')
    }
  })
})

describe('the change that asks for one', () => {
  const asked = (over: Partial<ExceptionChange> = {}): ExceptionChange => {
    const { state: _s, ends: _e, days_left: _d, granted: _g, ...rest } = item({ id: null })
    return { ...rest, how: 'added', ...over }
  }
  const render = (items: ExceptionChange[]) => renderToStaticMarkup(h(ExceptionsAsked, { items }))

  test('says, before the approval, that one for the identity covers every request of yours (R-75)', () => {
    const shown = text(render([asked()]))
    expect(shown).toContain(say('exceptions.change.identityWarning'))
    expect(shown).toContain('x-company-team')
  })

  test('a project\'s says which project, and no identity-wide warning', () => {
    const shown = text(render([asked({ scope: 'project', project: 'shop' })]))
    expect(shown).toContain(say('exceptions.change.projectOnly', { name: 'shop' }))
    expect(shown).not.toContain(say('exceptions.change.identityWarning'))
  })

  test('a permanent one says it never expires, chosen on purpose', () => {
    expect(text(render([asked({ permanent: true, how: 'made permanent' })]))).toContain(say('exceptions.change.permanent'))
  })
})

describe('uses with the history off, and overflow', () => {
  const status = (over: Partial<RequestStatus['local']> = {}, server: RequestStatus['server'] = null): RequestStatus => ({
    schema: 1,
    local: {
      counts: [
        ['0123456789abcdef0123456789abcdef', 1, 2, 5, 5, 5, 0],
        ['fedcba9876543210fedcba9876543210', 1, 3, 2, 2, 2, 0],
        [2, 0, 0, 40, 40, 40, 0],
      ],
      overflow: 0,
      partial: false,
      removed_headers: 4,
      saturated: false,
      ...over,
    },
    server,
  })
  const render = (of: RequestStatus | null, recording: boolean) => renderToStaticMarkup(h(ExceptionStatus, { status: of, recording }))

  test('the uses of exceptions, counted in memory, cleared at a restart; with the history off, all that is kept', () => {
    const off = text(render(status(), false))
    expect(off).toContain(say('exceptions.status.uses', { count: 7 }))
    expect(off).toContain(say('exceptions.status.historyOff'))
    expect(text(render(status(), true))).not.toContain(say('exceptions.status.historyOff'))
  })

  test('overflow and a full table are said, never hidden', () => {
    const shown = text(render(status({ overflow: 3, saturated: true }), false))
    expect(shown).toContain(say('exceptions.status.overflow', { count: 3 }))
  })

  test('a server\'s sum is read instead of this instance\'s counts, never added to them (review Minor 5, U15)', () => {
    const counts: RequestStatus['local']['counts'] = [['00112233445566778899aabbccddeeff', 1, 2, 11, 11, 11, 0]]
    const summed = status({}, { available: true, complete: true, counts, overflow: 0, saturated: false })
    expect(text(render(summed, true))).toContain(say('exceptions.status.uses', { count: 11 }))
  })

  test('on a server, summed across its instances, or this one alone when the others cannot be read', () => {
    const summed = text(render(status({}, { available: true, complete: true, overflow: 0, saturated: false }), false))
    expect(summed).toContain(say('exceptions.status.summed'))
    const alone = text(render(status({}, { available: false, complete: false, overflow: null, saturated: false }), false))
    expect(alone).toContain(say('exceptions.status.instanceAlone'))
  })

  test('nothing to say when the status cannot be read', () => {
    expect(render(null, true)).toBe('')
  })
})

describe('no raw value', () => {
  test('the owner\'s words are shown as text, never in a tooltip or an accessibility name', () => {
    const html = list([item()])
    for (const attribute of html.matchAll(/(?:title|aria-label|aria-description)="([^"]*)"/g)) {
      expect(attribute[1]).not.toContain('TOPSECRET')
    }
  })
})
