import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ConnectedNotice, Me } from '../../api/types'
import { MeContext, type MeState } from '../../lib/me'
import { ConnectedNotice as Notice, SignInReason } from './Notices'

const CONNECTED: ConnectedNotice = {
  until: 4821,
  computers: [{ label: 'emily-laptop', connected: 1_790_000_000 }],
  codes: [{ made: 1_790_001_000, by_administrator: true }],
  more: 0,
}

function render(component: ReturnType<typeof h>, me: Partial<Me> | undefined): string {
  const value: MeState = { me: me as Me | undefined, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  return renderToStaticMarkup(h(MeContext.Provider, { value }, component)).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')
}

const notice = (via: 'sso' | 'code', connected: ConnectedNotice | null = CONNECTED): Partial<Me> => ({
  session: { via },
  edition: { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [], ...(connected ? { connected } : {}) },
})

describe('the notice of computers and codes', () => {
  it('lists each computer connected and each code made, without calling any of them new', () => {
    const text = render(h(Notice), notice('sso'))
    expect(text).toContain('Computers connected to your account')
    expect(text).toContain('emily-laptop, connected')
    expect(text).toContain('A code to connect a computer, made for you by an administrator')
    expect(text).not.toMatch(/\bnew\b/i)
  })

  it('says how many older codes of their own it leaves out', () => {
    expect(render(h(Notice), notice('sso', { ...CONNECTED, more: 5 }))).toContain('5 older codes made by you are not listed.')
    expect(render(h(Notice), notice('sso', { ...CONNECTED, more: 1 }))).toContain('1 older code made by you is not listed.')
    expect(render(h(Notice), notice('sso'))).not.toContain('not listed')
  })

  it('can be marked as seen from a session the company sign-in opened', () => {
    const text = render(h(Notice), notice('sso'))
    expect(text).toContain('Mark as seen')
    expect(text).not.toContain('goes by itself')
  })

  it('cannot be marked as seen from any other session, and says it goes by itself after 14 days', () => {
    for (const via of ['code', null] as const) {
      const text = render(h(Notice), via === null ? { ...notice('code'), session: null } : notice(via))
      expect(text).not.toContain('Mark as seen')
      expect(text).toContain('Otherwise it goes by itself 14 days after each entry.')
    }
  })

  it('is not there when the server tells nothing', () => {
    expect(render(h(Notice), notice('sso', null))).toBe('')
    expect(render(h(Notice), undefined)).toBe('')
  })
})

describe('why a sign-in did not happen, for a person who has a session', () => {
  afterEach(() => vi.unstubAllGlobals())
  const at = (search: string) => {
    vi.stubGlobal('window', { location: { search } })
    return render(h(SignInReason), undefined)
  }

  it('says a refused link of their company account, from the address the dashboard was opened with', () => {
    const text = at('?sign-in=not-provisioned')
    expect(text).toContain('This company account is not linked to an account on this server.')
    expect(text).toContain('Dismiss')
  })

  it('says the other reasons too, and any reason it does not know as a failure', () => {
    expect(at('?sign-in=not-allowed')).toContain('does not give you Latet')
    expect(at('?sign-in=whatever')).toContain('The sign-in could not be finished.')
  })

  it('says nothing when no sign-in was refused, nor where there is no address to read', () => {
    expect(at('')).toBe('')
    expect(at('?login=expired')).toBe('')
    vi.unstubAllGlobals()
    expect(render(h(SignInReason), undefined)).toBe('')
  })
})
