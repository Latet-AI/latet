import { describe, expect, it } from 'vitest'
import type { ExchangeDetail, ExchangeSummary, Leak, Replacement, Slot } from '../../api/types'
import type { StoppedValue } from '../../lib/blocks'
import { blockMarks, pairMarks, requestMarks, RESPONSE_TEXT_SCOPE, toolScope } from './marks'

const span = (r: Partial<Replacement> & Pick<Replacement, 'start' | 'end' | 'masked_start' | 'masked_end'>): Replacement => ({
  type: 'EMAIL',
  class: 'PII',
  rule: 'pii:email',
  module: 'personal',
  via: 'restore',
  entity: 'e1',
  known: true,
  ...r,
})

describe('blockMarks', () => {
  it('marks both sides of a response from the daemon spans', () => {
    const masked = 'Write to marta.greco@gmail.com today'
    const restored = 'Write to giulia.conti@pec-studio.it today'
    const marks = blockMarks(restored, masked, [span({ start: 9, end: 35, masked_start: 9, masked_end: 30 })], 'r', [
      'agent',
      'model',
    ])
    const model = marks.sent[0]!
    const agent = marks.original![0]!
    expect(masked.slice(model.start, model.end)).toBe('marta.greco@gmail.com')
    expect(restored.slice(agent.start, agent.end)).toBe('giulia.conti@pec-studio.it')
    expect([model.info.side, agent.info.side]).toEqual(['model', 'agent'])
    expect(agent.info.original).toBe('giulia.conti@pec-studio.it')
    expect(model.info.pair).toBe(agent.info.pair)
    // Restored values still say which module and rule masked them.
    expect([agent.info.module, agent.info.rule, agent.info.via]).toEqual(['personal', 'pii:email', 'restore'])
  })

  it('keeps module, rule and how for values masked by the Claude Code plugin', () => {
    const masked = 'key SURR1'
    const plugin = span({ start: 4, end: 9, masked_start: 4, masked_end: 9, type: 'GITHUB_PAT', class: 'SECRET', rule: 'gitleaks:github-pat', module: 'secrets', via: 'plugin' })
    const [value] = blockMarks('key ghp_1', masked, [plugin], 's').values
    expect([value!.module, value!.rule, value!.via]).toEqual(['secrets', 'gitleaks:github-pat', 'plugin'])
  })

  it('reads code point offsets after an emoji', () => {
    const masked = '🚀 marta.greco@gmail.com'
    const marks = blockMarks(null, masked, [span({ start: 2, end: 23, masked_start: 2, masked_end: 23 })], 's')
    const mark = marks.sent[0]!
    expect(masked.slice(mark.start, mark.end)).toBe('marta.greco@gmail.com')
    expect(marks.original).toBeNull()
  })

  it('numbers values in the order they appear and keeps what the vault lost', () => {
    const masked = 'b=SURR2 a=SURR1'
    const marks = blockMarks('b=SURR2 a=REAL1', masked, [
      span({ start: 10, end: 15, masked_start: 10, masked_end: 15, entity: 'e1' }),
      span({ start: 2, end: 7, masked_start: 2, masked_end: 7, entity: 'e2', known: false }),
    ], 's')
    expect(marks.values.map((v) => [v.footnote, v.sent, v.known, v.original])).toEqual([
      [1, 'SURR2', false, null],
      [2, 'SURR1', true, 'REAL1'],
    ])
  })
})

describe('blockMarks with the values that stopped the request', () => {
  const stopped = (r: Partial<StoppedValue> & Pick<StoppedValue, 'start' | 'end' | 'masked_start' | 'masked_end'>): StoppedValue => ({
    type: 'GITHUB_PAT',
    kind: 'user_text',
    preview: 'GITHUB_PAT[10]',
    new: true,
    block: 'b1',
    class: 'SECRET',
    rule: 'gitleaks:github-pat',
    ...r,
  })

  it('marks the value on both sides beside the masked ones, each at its own offsets', () => {
    const masked = 'mail SURR.example, key ghp_SECRET'
    const original = 'mail marta.greco@gmail.com, key ghp_SECRET'
    const email = span({ start: 5, end: 26, masked_start: 5, masked_end: 17 })
    const leak = stopped({ masked_start: masked.indexOf('ghp_'), masked_end: masked.length, start: original.indexOf('ghp_'), end: original.length })
    const marks = blockMarks(original, masked, [email], 's', ['original', 'sent'], [leak])

    expect(marks.values.map((v) => v.type)).toEqual(['EMAIL']) // a stopped value is no masked value: not in the table
    const sentStopped = marks.sent.find((m) => m.info.stopped)!
    const originalStopped = marks.original!.find((m) => m.info.stopped)!
    expect(masked.slice(sentStopped.start, sentStopped.end)).toBe('ghp_SECRET')
    expect(original.slice(originalStopped.start, originalStopped.end)).toBe('ghp_SECRET')
    expect(marks.sent.map((m) => masked.slice(m.start, m.end))).toEqual(['SURR.example', 'ghp_SECRET'])
    expect(marks.original!.map((m) => original.slice(m.start, m.end))).toEqual(['marta.greco@gmail.com', 'ghp_SECRET'])
    // One value, two panes: they highlight each other. What Not sensitive adds is the value as the check read it.
    expect(sentStopped.info.pair).toBe(originalStopped.info.pair)
    expect([sentStopped.info.side, originalStopped.info.side]).toEqual(['sent', 'original'])
    expect(sentStopped.info).toMatchObject({ type: 'GITHUB_PAT', cls: 'SECRET', rule: 'gitleaks:github-pat', footnote: null, entity: null })
    expect(originalStopped.info.original).toBe('ghp_SECRET')
  })

  it('reads code point offsets after an emoji', () => {
    const text = '🚀 key ghp_SECRET'
    const leak = stopped({ masked_start: 6, masked_end: 16, start: 6, end: 16 })
    const marks = blockMarks(text, text, [], 's', ['original', 'sent'], [leak])
    const mark = marks.sent[0]!
    expect(text.slice(mark.start, mark.end)).toBe('ghp_SECRET')
    expect(mark.info.original).toBe('ghp_SECRET')
  })

  it('still marks what was sent when the original is unknown', () => {
    const masked = 'key ghp_SECRET'
    const marks = blockMarks(null, masked, [], 's', ['original', 'sent'], [stopped({ masked_start: 4, masked_end: 14, start: 4, end: 14 })])
    expect(marks.original).toBeNull()
    expect(masked.slice(marks.sent[0]!.start, marks.sent[0]!.end)).toBe('ghp_SECRET')
  })

  it('has nothing to add for a block with no such value', () => {
    const marks = blockMarks('a', 'a', [], 's')
    expect([marks.sent, marks.original, marks.values]).toEqual([[], [], []])
  })

  it('marks values that overlap once, over their union, and names every text Not sensitive must allow', () => {
    // A PEM key is read twice: whole by one rule, and by its body inside it by another.
    const masked = 'key:\n---BEGIN---\nBODY\n---END---\nthanks'
    const whole = { masked_start: masked.indexOf('---BEGIN'), masked_end: masked.indexOf('\nthanks') }
    const body = { masked_start: masked.indexOf('\nBODY'), masked_end: masked.indexOf('---END') }
    const outer = stopped({ ...whole, start: whole.masked_start, end: whole.masked_end })
    const inner = stopped({ ...body, start: body.masked_start, end: body.masked_end, rule: 'private-key:pem' })
    for (const order of [[outer, inner], [inner, outer]]) {
      const marks = blockMarks(masked, masked, [], 's', ['original', 'sent'], order)
      expect(marks.sent).toHaveLength(1) // the inner one would be skipped under the outer: nothing to click
      expect(marks.original).toHaveLength(1)
      const [mark] = marks.sent
      expect(masked.slice(mark!.start, mark!.end)).toBe(masked.slice(whole.masked_start, whole.masked_end))
      expect(mark!.info.texts).toEqual([masked.slice(whole.masked_start, whole.masked_end), masked.slice(body.masked_start, body.masked_end)])
      expect(mark!.info.sent).toBe(masked.slice(whole.masked_start, whole.masked_end)) // what the popover shows
      expect(marks.original![0]!.info.pair).toBe(mark!.info.pair)
      expect(mark!.info).toMatchObject({ type: 'GITHUB_PAT', rule: 'gitleaks:github-pat', stopped: true }) // the outer one speaks
    }
  })

  it('joins values that cross or chain, and keeps apart those that only touch or are far', () => {
    const text = 'abcdefghijklmnopqrstuvwxyz0123456789ABCD'
    const at = (masked_start: number, masked_end: number) => stopped({ masked_start, masked_end, start: masked_start, end: masked_end })
    // [0,10) and [8,20) cross, [18,30) crosses the second: one group. [30,35) only touches it. [36,38) is alone.
    const marks = blockMarks(text, text, [], 's', ['original', 'sent'], [at(36, 38), at(18, 30), at(0, 10), at(30, 35), at(8, 20)])
    expect(marks.sent.map((m) => [m.start, m.end])).toEqual([[0, 30], [30, 35], [36, 38]])
    expect(marks.sent.map((m) => m.info.texts)).toEqual([
      ['abcdefghij', 'ijklmnopqrst', 'stuvwxyz0123'],
      ['45678'],
      ['AB'],
    ])
    expect(new Set(marks.sent.map((m) => m.info.pair)).size).toBe(3)
    expect(marks.original!.map((m) => [m.start, m.end])).toEqual([[0, 30], [30, 35], [36, 38]])
  })

  it('keeps two values of the same text apart when they do not overlap', () => {
    const masked = 'key ghp_SECRET and key ghp_SECRET'
    const first = masked.indexOf('ghp_')
    const second = masked.lastIndexOf('ghp_')
    const marks = blockMarks(masked, masked, [], 's', ['original', 'sent'], [
      stopped({ masked_start: first, masked_end: first + 10, start: first, end: first + 10 }),
      stopped({ masked_start: second, masked_end: second + 10, start: second, end: second + 10 }),
    ])
    expect(marks.sent.map((m) => m.info.texts)).toEqual([['ghp_SECRET'], ['ghp_SECRET']])
  })

  it('counts the union on the original side from where each value is there', () => {
    const masked = 'mail SURR.example, key --KEY-BODY--KEY--'
    const original = 'mail marta.greco@gmail.com, key --KEY-BODY--KEY--'
    const email = span({ start: 5, end: 26, masked_start: 5, masked_end: 17 })
    const from = masked.indexOf('--KEY-BODY')
    const whole = stopped({ masked_start: from, masked_end: masked.length, start: original.indexOf('--KEY-BODY'), end: original.length })
    const inside = stopped({ masked_start: from + 2, masked_end: from + 10, start: original.indexOf('--KEY-BODY') + 2, end: original.indexOf('--KEY-BODY') + 10 })
    const marks = blockMarks(original, masked, [email], 's', ['original', 'sent'], [whole, inside])
    const stoppedMarks = marks.original!.filter((m) => m.info.stopped)
    expect(stoppedMarks).toHaveLength(1)
    expect(original.slice(stoppedMarks[0]!.start, stoppedMarks[0]!.end)).toBe('--KEY-BODY--KEY--')
    expect(marks.sent.filter((m) => m.info.stopped).map((m) => masked.slice(m.start, m.end))).toEqual(['--KEY-BODY--KEY--'])
  })
})

describe('requestMarks', () => {
  const summary = (flags: string[]): ExchangeSummary => ({
    id: 'ex_9',
    ts: 0,
    session: 's',
    agent: 'tutorial-agent',
    agent_version: null,
    upstream: 'anthropic',
    path: '/v1/messages',
    protocol: 'anthropic',
    mode: 'verify',
    transport: 'http',
    model: null,
    status: 400,
    latency_ms: null,
    overhead_ms: null,
    tokens: null,
    masked: 0,
    new_masked: 0,
    by_type: {},
    new_by_type: {},
    restored: 0,
    flags,
    verified: false,
  })
  const slot = (block: string, text: string, replacements: Replacement[] = []): Slot => ({
    block,
    path: '$',
    kind: 'user_text',
    from_model: false,
    new: true,
    original: text,
    masked: text,
    replacements,
  })
  const leak: Leak = {
    type: 'GITHUB_PAT',
    kind: 'user_text',
    preview: 'GITHUB_PAT[10]',
    new: true,
    block: 'b2',
    class: 'SECRET',
    rule: 'gitleaks:github-pat',
    masked_start: 4,
    masked_end: 14,
    start: 4,
    end: 14,
  }
  const detail = (flags: string[]): ExchangeDetail => ({
    summary: summary(flags),
    request: { headers: [], slots: [slot('b1', 'hello'), slot('b2', 'key ghp_SECRET')], body_bytes: 0 },
    response: null,
    leaks: [leak],
  })

  it('scopes the marks of each block by the exchange and the block, as the exchange page does', () => {
    const [first, second] = requestMarks(detail(['blocked', 'leak']))
    expect(first!.sent).toEqual([])
    expect(second!.sent.map((m) => m.info.pair)).toEqual(['ex_9:1:stopped:0'])
    expect(second!.original!.map((m) => m.info.pair)).toEqual(['ex_9:1:stopped:0'])
  })

  it('marks a stopped value only in the block it is in, and only in a request that was stopped', () => {
    const [first, second] = requestMarks(detail(['blocked']))
    expect(first!.sent).toEqual([])
    expect(second!.sent).toHaveLength(1)
    const sent = requestMarks(detail([]))
    expect(sent[1]!.sent).toEqual([]) // it left: the leak is a record, not a value to click
  })
})

describe('the marks of a response', () => {
  it('has the scopes the response view gives them', () => {
    expect([RESPONSE_TEXT_SCOPE, toolScope(0), toolScope(3)]).toEqual(['response', 'tool0', 'tool3'])
  })

  it("takes the daemon's spans when there are some, and says the two sides line up", () => {
    const masked = 'mail SURR.example'
    const restored = 'mail marta.greco@gmail.com'
    const spans = [span({ start: 5, end: 26, masked_start: 5, masked_end: 17 })]
    const marks = pairMarks(masked, restored, spans, [], 'response')
    expect(marks.aligned).toBe(true)
    expect(marks.model.map((m) => m.info.side)).toEqual(['model'])
    expect(restored.slice(marks.agent[0]!.start, marks.agent[0]!.end)).toBe('marta.greco@gmail.com')
  })
})
