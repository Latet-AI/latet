import { describe, expect, it } from 'vitest'
import { fullNameNote } from './identityName'

describe('the line under the full name in Rules', () => {
  it("says the provider's name is in use, which the person's settings cannot change", () => {
    expect(fullNameNote({ full_name_from: 'provider', full_name_locked: true })).toBe('rules.identity.fullNameProvider')
    // An older answer that tells where the name comes from and nothing of the lock: the name is the provider's still.
    expect(fullNameNote({ full_name_from: 'provider' })).toBe('rules.identity.fullNameProvider')
  })

  it("says the name cannot be changed here while the person's own is in use and the provider names them (names are masked)", () => {
    expect(fullNameNote({ full_name_from: null, full_name_locked: true })).toBe('rules.identity.fullNameLocked')
    expect(fullNameNote({ full_name_locked: true })).toBe('rules.identity.fullNameLocked')
  })

  it('says nothing of a name that is their own to change, nor when the server does not tell', () => {
    expect(fullNameNote({ full_name_from: null, full_name_locked: false })).toBeNull()
    expect(fullNameNote({ full_name_from: null })).toBeNull()
    expect(fullNameNote({})).toBeNull()
  })
})
