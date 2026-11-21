import { describe, expect, it } from 'vitest'
import type { Me } from '../api/types'
import { asksPolicy } from './usePolicy'

const said = { edition: { policy: { sources: [], problems: [] } } } as unknown as Me
const none = { edition: { policy: null } } as unknown as Me

describe('whether the policy is asked', () => {
  it('is, in your own space, when /me says there is one or what is wrong with it', () => {
    expect(asksPolicy(said, null)).toBe(true)
    expect(asksPolicy(none, null)).toBe(false)
    expect(asksPolicy(undefined, null)).toBe(false)
    expect(asksPolicy({} as Me, null)).toBe(false) // an older answer: the Community Edition
  })

  it("is never in an API key's space, where every Enterprise route refuses a key", () => {
    expect(asksPolicy(said, 'u_billingcix')).toBe(false)
  })
})
