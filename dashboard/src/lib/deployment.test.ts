import { describe, expect, it } from 'vitest'
import { computer, server } from '../test/eop35-fixtures'
import { deploymentKey, pendingKey } from './deployment'

// Words that depend on where Latet runs wait for `/me`: while it loads they say so, and only once it failed do they
// say to retry the connection.
describe('the words that wait for /me', () => {
  it('say that the page loads while the first answer is on its way', () => {
    expect(deploymentKey(undefined, 'vault.description')).toBe('common.loading')
    expect(deploymentKey(undefined, 'vault.description', false)).toBe('common.loading')
    expect(pendingKey(false)).toBe('common.loading')
  })

  it('say to retry the connection once the answer failed', () => {
    expect(deploymentKey(undefined, 'vault.description', true)).toBe('deployment.pending')
    expect(pendingKey(true)).toBe('deployment.pending')
  })

  it('keep the neutral tagline either way', () => {
    expect(deploymentKey(undefined, 'app.tagline', false)).toBe('deployment.unknownTagline')
    expect(deploymentKey(undefined, 'app.tagline', true)).toBe('deployment.unknownTagline')
  })

  it('follow the answer once there is one, failed reloads aside', () => {
    expect(deploymentKey(computer, 'vault.description', true)).toBe('vault.description')
    expect(deploymentKey(server('person'), 'vault.description', true)).toBe('deployment.vault')
    expect(deploymentKey(undefined, 'common.retry', true)).toBe('common.retry') // a key with no server words of its own
  })
})
