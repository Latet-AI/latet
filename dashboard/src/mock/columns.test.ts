import { describe, expect, it } from 'vitest'
import type { Slot } from '../api/types'
import { mockColumns, mockColumnShape } from './columns'

const output = (original: string): Slot => ({ path: '$', kind: 'tool_result', from_model: false, new: true, original, masked: original, replacements: [] })

describe('the mock of the columns of a tool output', () => {
  it('gives the light answer (?shape=1) as the daemon does: the names alone, none for a tool strict data mode leaves alone', () => {
    const full = mockColumns(output(JSON.stringify([{ id: 1, email: 'a@b.it', status: 'open' }])), null, false, undefined, false)
    expect(mockColumnShape(full)).toEqual({ columns: ['id', 'email', 'status'], strict: false, applies: true })
    expect(mockColumnShape({ ...full, applies: false })).toEqual({ columns: [], strict: false, applies: false })
    expect(mockColumnShape({ ...full, strict: true })).toMatchObject({ strict: true })
  })
})
