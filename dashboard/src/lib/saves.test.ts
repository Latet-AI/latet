import { describe, expect, it } from 'vitest'
import type { ConfigView } from '../api/types'
import { remadeFrom } from './saves'

const fresh = { etag: 'after', allow_values: ['jobs@acme.com'] } as unknown as ConfigView

describe('the Rules page after its save was kept', () => {
  it('is made again from the settings read after it while it is what it sent', () => {
    const sent = { allow_values: ['jobs@acme.com'] }
    expect(remadeFrom(fresh, sent, sent)).toBe(fresh)
  })

  it('keeps what it was made from, the old etag among it, when it changed while the save ran', () => {
    // Its next save is then refused as stale and the form made again: a draft never meets a newer etag, so a value the
    // administrators stopped letting through meanwhile is never saved back as the person's own.
    const sent = { allow_values: ['jobs@acme.com'] }
    const edited = { allow_values: ['jobs@acme.com', 'billing@acme.com'] }
    expect(remadeFrom(fresh, edited, sent)).toBeNull()
  })

  it('keeps what it was made from when the settings could not be read after the save', () => {
    const sent = { allow_values: [] }
    expect(remadeFrom(null, sent, sent)).toBeNull()
  })
})
