import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Me } from '../../api/types'
import { MeContext, type MeState } from '../../lib/me'
import type { LicenseStanding } from '../types'

// The page as it renders for a standing the daemon answered: the users line and the day a pause would start are said
// only where the daemon tells them (on a server, a person is told neither).
const answer: { standing: LicenseStanding } = { standing: undefined as unknown as LicenseStanding }
vi.mock('../../lib/useAsync', () => ({
  useAsync: () => ({ data: answer.standing, error: undefined, loading: false, stale: false, reload: () => undefined, mutate: () => undefined }),
}))

const { LicensePage } = await import('./LicensePage')

const BASE: LicenseStanding = {
  state: 'grace',
  license: { id: 'L-2026-0042', licensee: 'Acme Corp', users: 300, features: ['reports'], issued: '2026-03-01', expires: '2026-10-01', trial: false },
  users_in_use: null,
  days_left: -11,
  over_since: null,
  pauses_on: null,
  reason: null,
  reason_code: null,
  notices: [],
  source: 'server',
}

function page(standing: LicenseStanding): string {
  answer.standing = standing
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(LicensePage)))
  return html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')
}

describe('the License page', () => {
  it('says neither the users in use nor a pause day that the daemon does not tell', () => {
    const text = page(BASE)
    expect(text).toContain('Users bought')
    expect(text).not.toContain('Users in use')
    expect(text).toContain('The grace period is running')
    expect(text).not.toContain('November')
  })

  it('says both where the daemon tells them', () => {
    const text = page({ ...BASE, users_in_use: 312, pauses_on: '2026-11-06', notices: ['users-over'] })
    expect(text).toContain('Users in use')
    expect(text).toContain('312')
    expect(text).toContain('Everything keeps working until November 6, 2026')
  })

  it('shows no control that changes the license, and the command its administrators run', () => {
    answer.standing = BASE
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(LicensePage)))
    expect(html).not.toContain('type="file"')
    const text = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')
    for (const words of ['Install a license file', 'Install a new file', 'Remove']) expect(text).not.toContain(words)
    expect(text).toContain('latet server license install')
  })
})

// Whoever manages the license changes it here, while it can manage; in any other standing, and for everyone else, the
// server's command line does.
function pageFor(standing: LicenseStanding, actions: string[]): { html: string; text: string } {
  answer.standing = standing
  const me = { edition: { actions } } as unknown as Me
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(MeContext.Provider, { value }, h(LicensePage)))).replace(/<dialog[\s\S]*?<\/dialog>/g, '')
  return { html, text: html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ') }
}

describe('the License page of whoever manages the license', () => {
  const install = '>Install a license file<'
  const remove = '>Remove the license<'

  it('installs a license file and removes the license while it can manage, and says what a license without roles does', () => {
    for (const state of ['valid', 'expiring', 'grace'] as const) {
      const { html, text } = pageFor({ ...BASE, state }, ['license.manage'])
      expect(html, state).toContain(install)
      expect(html, state).toContain(remove)
      expect(html, state).toContain('type="file"')
      expect(text).toContain('A license that does not list roles opens the Enterprise pages to every person on this server, to read.')
      expect(text).not.toContain('On a server, its administrators install the license from the command line')
    }
  })

  it("sends them to the server's command line in any standing the server refuses a change in", () => {
    for (const state of ['paused', 'trial-ended', 'invalid', 'none'] as const) {
      const { html, text } = pageFor({ ...BASE, state }, ['license.manage'])
      expect(html, state).not.toContain(install)
      expect(html, state).not.toContain('type="file"')
      expect(text, state).toContain('In this state the license is installed or removed from the command line, not from this page:')
      expect(text, state).toContain('latet server license install')
    }
  })

  it('shows a person who does not manage the license no control, whatever the standing', () => {
    for (const actions of [[], ['people.manage', 'audit.read']]) {
      const { html, text } = pageFor({ ...BASE, state: 'valid' }, actions)
      expect(html).not.toContain(install)
      expect(html).not.toContain(remove)
      expect(text).toContain('On a server, its administrators install the license from the command line:')
    }
  })
})
