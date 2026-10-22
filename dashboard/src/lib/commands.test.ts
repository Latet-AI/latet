import { describe, expect, it } from 'vitest'

import { fixCommand } from './commands'

describe('fixCommand', () => {
  it('offers the command that routes an installed agent through Latet', () => {
    expect(fixCommand({ id: 'codex', installed: true, status: 'not applied' })).toBe('latet agents apply codex')
    expect(fixCommand({ id: 'codex', installed: true, status: 'partial' })).toBe('latet agents apply codex')
  })

  it('offers none for an agent that is through Latet, one run with its own command, or one not installed', () => {
    expect(fixCommand({ id: 'claude', installed: true, status: 'applied' })).toBeNull()
    expect(fixCommand({ id: 'gemini', installed: true, status: 'wrapper' })).toBeNull()
    expect(fixCommand({ id: 'opencode', installed: false, status: 'not installed' })).toBeNull()
  })
})
