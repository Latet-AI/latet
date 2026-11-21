import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { refusalOf, reveal } from './changes'

const COMPANY = 'sign in with your company account to change what applies to everyone'
const company = () => new ApiError(403, COMPANY, [], { error: COMPANY, code: 'company-sign-in' })
const changed = () => new ApiError(409, 'x', [], { error: 'x', code: 'sign-in-changed' })

describe('what a refused change of an Enterprise page does next', () => {
  it('leads to the company sign-in when that is what the server asks for, whatever else the page is ready for', () => {
    expect(refusalOf(company(), false)).toBe('company')
    expect(refusalOf(company(), true)).toBe('company')
  })

  it('reads the page again, saying someone else changed it, only where the page is ready to', () => {
    expect(refusalOf(new ApiError(409, 'fixed', [], { code: 'siem-changed' }), true)).toBe('changed')
    expect(refusalOf(new ApiError(409, 'fixed', [], { code: 'siem-changed' }), false)).toBe('toast')
    expect(refusalOf(changed(), true)).toBe('changed')
    // A page that cannot read again says the server's reason in a toast as before.
    expect(refusalOf(changed(), false)).toBe('toast')
  })

  it("says any other refusal in a toast, a company sign-in that is not the server's marker included", () => {
    expect(refusalOf(new ApiError(409, 'this is the last administrator of this server'), true)).toBe('toast')
    expect(refusalOf(new ApiError(403, COMPANY), true)).toBe('toast') // the words alone are no marker
    expect(refusalOf(new ApiError(409, 'x', [], { error: 'x', code: 'policy-changed' }), true)).toBe('toast')
    expect(refusalOf(new Error('offline'), true)).toBe('toast')
  })
})

describe('bringing a banner to the person', () => {
  it('scrolls it into view only as far as needed, then focuses it without scrolling again', () => {
    const calls: string[] = []
    const banner = {
      scrollIntoView: vi.fn((options?: ScrollIntoViewOptions) => calls.push(`scroll ${options?.block}`)),
      focus: vi.fn((options?: FocusOptions) => calls.push(`focus ${options?.preventScroll}`)),
    }
    reveal(banner)
    expect(calls).toEqual(['scroll nearest', 'focus true'])
  })

  it('does nothing before the banner is on the page', () => {
    expect(() => reveal(null)).not.toThrow()
  })
})
