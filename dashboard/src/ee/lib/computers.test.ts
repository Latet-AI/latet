import { describe, expect, it } from 'vitest'
import { computerLines } from './computers'

const NOW = 1_800_000_000

describe('computerLines', () => {
  it('lists the computers newest first, each with who made its code and when it was last used', () => {
    const lines = computerLines([
      { label: 'emily-laptop', connected: NOW - 7200, last_used: NOW - 60, by_administrator: true },
      { label: 'emily-desktop', connected: NOW - 600, last_used: null, by_administrator: false },
      { label: '  ', connected: NOW - 86400, last_used: NOW - 3600, by_administrator: null },
    ])
    expect(lines.map((line) => [line.label, line.connected, line.lastUsed, line.how, line.used])).toEqual([
      ['emily-desktop', NOW - 600, null, 'ee.computers.connectedByYou', 'ee.computers.neverUsed'],
      ['emily-laptop', NOW - 7200, NOW - 60, 'ee.computers.connectedByAdministrator', 'ee.computers.lastUsed'],
      [null, NOW - 86400, NOW - 3600, 'ee.computers.connected', 'ee.computers.lastUsed'], // no name; who made its code not said
    ])
    expect(new Set(lines.map((line) => line.key)).size).toBe(3)
  })

  it('lists nothing for no computer', () => {
    expect(computerLines([])).toEqual([])
  })
})
