import { describe, expect, it } from 'vitest'
import { closesSpace, spaceUrl } from './space'

const BASE = '/_latet/api/v1'

describe('the space a request goes to', () => {
  it('is your own when no API key is open', () => {
    expect(spaceUrl(BASE, null, '/exchanges?limit=5')).toBe('/_latet/api/v1/exchanges?limit=5')
  })

  it("is the key's while one is open", () => {
    expect(spaceUrl(BASE, 'u_billingcix', '/config')).toBe('/_latet/api/v1/ee/keys/u_billingcix/space/config')
  })

  it('keeps the routes about the keys your own', () => {
    expect(spaceUrl(BASE, 'u_billingcix', '/ee/keys')).toBe('/_latet/api/v1/ee/keys')
    expect(spaceUrl(BASE, 'u_billingcix', '/ee/keys/u_nightlyimp/controllers')).toBe('/_latet/api/v1/ee/keys/u_nightlyimp/controllers')
    expect(spaceUrl(BASE, 'u_billingcix', '/ee/keysafe')).toBe('/_latet/api/v1/ee/keys/u_billingcix/space/ee/keysafe')
  })

  it('keeps signing out yours: it ends your session, and is no change in the space of a key', () => {
    expect(spaceUrl(BASE, 'u_billingcix', '/logout')).toBe('/_latet/api/v1/logout')
    expect(spaceUrl(BASE, 'u_billingcix', '/logout/x')).toBe('/_latet/api/v1/ee/keys/u_billingcix/space/logout/x')
  })

  it('names the key by its encoded id, never as a path of its own', () => {
    expect(spaceUrl(BASE, '../u_x', '/config')).toBe('/_latet/api/v1/ee/keys/..%2Fu_x/space/config')
  })

  it('knows when the server closed the space to you', () => {
    expect(closesSpace(403, { error: 'you do not control this API key', space: 'closed' })).toBe(true)
    expect(closesSpace(403, { error: 'your roles on this server do not allow people.manage' })).toBe(false)
    expect(closesSpace(500, { space: 'closed' })).toBe(false)
  })

  it('knows each way the server says the space is closed, a trial that ended and a server that cannot say included', () => {
    expect(closesSpace(404, { error: 'there is no such API key on this server', space: 'closed' })).toBe(true)
    expect(closesSpace(409, { error: 'this API key is disabled', space: 'closed' })).toBe(true)
    expect(closesSpace(403, { error: 'the trial ended', code: 'trial-ended', feature: 'roles', space: 'closed' })).toBe(true)
    // What the gate in front of the space closes: the license without roles, who may do what unreadable.
    expect(closesSpace(403, { error: 'this needs Latet Enterprise with roles', space: 'closed' })).toBe(true)
    expect(closesSpace(503, { error: 'who may do what on this server cannot be read now: try again', space: 'closed' })).toBe(true)
    expect(closesSpace(200, { space: 'closed' })).toBe(false)
    expect(closesSpace(401, { error: 'unauthorized', space: 'closed' })).toBe(false) // the session's end: the transport says it
  })

  it('closes on the marker alone, never on the words of a refusal', () => {
    for (const error of [
      'this needs Latet Enterprise with roles',
      'who may do what on this server cannot be read now: try again',
      'cross-site request',
      'origin not allowed',
      'missing X-Latet-UI header',
    ]) {
      expect(closesSpace(403, { error })).toBe(false)
      expect(closesSpace(503, { error })).toBe(false)
    }
  })

  it("leaves the key's own refusals alone, whatever their status", () => {
    expect(closesSpace(403, { error: 'an API key has no computer' })).toBe(false)
    expect(closesSpace(404, { error: 'not found' })).toBe(false)
    expect(closesSpace(409, { error: 'Set by your company, not changed', floor: ['profile'] })).toBe(false)
    expect(closesSpace(503, { error: 'the company policy cannot be read here now' })).toBe(false)
    expect(closesSpace(403, { error: 'an API key space reaches none of the Enterprise pages' })).toBe(false)
    expect(closesSpace(403, 'not an object')).toBe(false)
    expect(closesSpace(403, null)).toBe(false)
  })
})
