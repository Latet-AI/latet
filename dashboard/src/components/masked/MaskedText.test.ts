import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { Replacement } from '../../api/types'
import { blockMarks } from './marks'
import { MaskedText } from './MaskedText'
import { SelectableText } from './SelectableText'
import type { TextMark } from './types'

const ORIGINAL = 'Write to giulia.conti@pec-studio.it today'
const MASKED = 'Write to marta.greco@gmail.com today'

const email = (r: Partial<Replacement> = {}): Replacement => ({
  start: 9,
  end: 35,
  masked_start: 9,
  masked_end: 30,
  type: 'EMAIL',
  class: 'PII',
  rule: 'pii:email',
  module: 'personal',
  via: 'restore',
  entity: 'e1',
  known: true,
  ...r,
})

/** The attributes of each <mark> in a rendered text, by name. */
function marksIn(element: ReactElement): Record<string, string>[] {
  const html = renderToStaticMarkup(element)
  return [...html.matchAll(/<mark\b([^>]*)>/g)].map((match) =>
    Object.fromEntries([...(match[1] ?? '').matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1] ?? '', a[2] ?? ''])),
  )
}

const classesOf = (mark: Record<string, string> | undefined): string[] => (mark?.class ?? '').split(/\s+/)

describe('MaskedText tone', () => {
  it('a value in the original column takes the real tone and its fake the fake tone', () => {
    const marks = blockMarks(ORIGINAL, MASKED, [email()], 's')
    const [real] = marksIn(h(MaskedText, { text: ORIGINAL, marks: marks.original ?? [], tone: 'real' }))
    const [fake] = marksIn(h(MaskedText, { text: MASKED, marks: marks.sent, tone: 'fake' }))
    expect(classesOf(real)).toEqual(expect.arrayContaining(['mask', 'bg-real-low', 'text-real-ink']))
    expect(classesOf(fake)).toEqual(expect.arrayContaining(['mask', 'bg-fake-low', 'text-fake-ink']))
    expect(classesOf(real)).not.toContain('bg-fake-low')
    expect(classesOf(fake)).not.toContain('bg-real-low')
    // The entity class stays on the value: its bar under the text, and the class itself.
    expect([real?.['data-category'], fake?.['data-category']]).toEqual(['PII', 'PII'])
    expect([real?.['data-tone'], fake?.['data-tone']]).toEqual(['pii', 'pii'])
  })

  it('without a tone a value keeps the look of its class, as before', () => {
    const marks = blockMarks(ORIGINAL, MASKED, [email({ type: 'API_KEY', class: 'SECRET' })], 's')
    const [mark] = marksIn(h(MaskedText, { text: MASKED, marks: marks.sent }))
    expect(classesOf(mark)).toEqual(['mask'])
    expect(mark?.['data-tone']).toBe('secret')
    expect(mark?.['data-category']).toBe('SECRET')
  })

  it('a redacted marker, a value the vault lost and a value that stopped the request keep their own look', () => {
    // One-way redaction: the marker sent is a solid bar, not a fake; the original side can only show it, hatched.
    const redacted = blockMarks('key [REDACTED:EMAIL]', 'key [REDACTED:EMAIL]', [email({ start: 4, end: 20, masked_start: 4, masked_end: 20, entity: null, known: false })], 'r')
    const [bar] = marksIn(h(MaskedText, { text: 'key [REDACTED:EMAIL]', marks: redacted.sent, tone: 'fake' }))
    const [gone] = marksIn(h(MaskedText, { text: 'key [REDACTED:EMAIL]', marks: redacted.original ?? [], tone: 'real' }))
    expect(classesOf(bar)).toContain('mask-redacted')
    expect(classesOf(gone)).toContain('mask-gone')
    // Stopped: in clear on both sides, so neither a real value kept here nor a fake that left.
    const stopped = blockMarks('pw hunter2', 'pw hunter2', [], 'x', ['original', 'sent'], [
      { block: 'b0', start: 3, end: 10, masked_start: 3, masked_end: 10, type: 'PASSWORD', kind: 'secret', preview: 'hu…', new: true, class: 'SECRET', rule: 'secrets:password' },
    ])
    const [stopSent] = marksIn(h(MaskedText, { text: 'pw hunter2', marks: stopped.sent, tone: 'fake' }))
    const [stopOriginal] = marksIn(h(MaskedText, { text: 'pw hunter2', marks: stopped.original ?? [], tone: 'real' }))
    expect(classesOf(stopSent)).toContain('mask-stopped')
    expect(classesOf(stopOriginal)).toContain('mask-stopped')
    for (const mark of [bar, gone, stopSent, stopOriginal]) {
      expect(classesOf(mark)).not.toEqual(expect.arrayContaining(['bg-real-low']))
      expect(classesOf(mark)).not.toEqual(expect.arrayContaining(['bg-fake-low']))
    }
  })

  it('the original of a waiting text takes the real tone too (Approvals)', () => {
    const marks: TextMark[] = blockMarks(ORIGINAL, MASKED, [email()], 'a').original ?? []
    const [mark] = marksIn(
      h(SelectableText, {
        text: ORIGINAL,
        marks,
        suggestions: [],
        added: [],
        check: () => ({ ok: false, reason: 'empty' }) as const,
        onMask: () => undefined,
        onSuggestion: () => undefined,
        label: 'Original',
        tone: 'real',
      }),
    )
    expect(classesOf(mark)).toEqual(expect.arrayContaining(['bg-real-low', 'text-real-ink']))
    expect(mark?.['data-category']).toBe('PII')
  })
})
