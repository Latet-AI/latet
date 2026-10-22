import { describe, expect, it } from 'vitest'
import { hostOf, signedBy, unused } from './destinations'

const destinations = [
  { name: 'llm-acme-com', url: 'https://llm.acme.com', agent: 'claude' as const, last_used: 1700000000, in_use: true },
  { name: 'azure-acme', url: 'https://acme.openai.azure.com/openai/v1', agent: 'codex' as const, last_used: null, in_use: false },
]

describe('destinations', () => {
  it('names the ones no agent points at any more', () => {
    expect(unused(destinations).map((d) => d.name)).toEqual(['azure-acme'])
  })

  it('tells who signed a request for Amazon', () => {
    expect(signedBy('aws-profile:work')).toEqual({ by: 'profile', profile: 'work' })
    expect(signedBy('aws-session')).toEqual({ by: 'session' })
    expect(signedBy(null)).toBeNull()
  })

  it('shows an address by its host', () => {
    expect(hostOf('https://acme.openai.azure.com/openai/v1')).toBe('acme.openai.azure.com')
    expect(hostOf('not an address')).toBe('not an address')
  })
})
