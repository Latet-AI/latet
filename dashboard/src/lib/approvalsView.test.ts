import { describe, expect, it } from 'vitest'
import { approvalsTabNamed, DECIDE_AFTER_MS, initialApprovalsTab, mayDecide, queuePosition } from './approvalsView'

describe('the tab Approvals opens on', () => {
  it('is Texts while a text waits, whether or not a change to your settings waits too', () => {
    expect(initialApprovalsTab(['b1', 'b2'], ['c1'])).toBe('texts')
    expect(initialApprovalsTab(['b1'], [])).toBe('texts')
  })

  it('is Settings when no text waits and a change does', () => {
    expect(initialApprovalsTab([], ['c1'])).toBe('settings')
  })

  it('is Texts when nothing waits', () => {
    expect(initialApprovalsTab([], [])).toBe('texts')
  })
})

describe('a link to a tab of Approvals', () => {
  it('names a tab by its hash', () => {
    expect(approvalsTabNamed('#settings')).toBe('settings')
    expect(approvalsTabNamed('#texts')).toBe('texts')
  })

  it('names none with no hash or another one', () => {
    expect(approvalsTabNamed('')).toBeNull()
    expect(approvalsTabNamed('#')).toBeNull()
    expect(approvalsTabNamed('#live-review')).toBeNull()
    expect(approvalsTabNamed('settings')).toBeNull()
  })
})

describe('one text at a time (a narrow window)', () => {
  const queue = ['a', 'b', 'c']

  it('says which text shows of how many, with the one before it and the one after it', () => {
    expect(queuePosition(queue, 'a')).toEqual({ number: 1, count: 3, previous: null, next: 'b' })
    expect(queuePosition(queue, 'b')).toEqual({ number: 2, count: 3, previous: 'a', next: 'c' })
    expect(queuePosition(queue, 'c')).toEqual({ number: 3, count: 3, previous: 'b', next: null })
  })

  it('has nothing before or after a lone text', () => {
    expect(queuePosition(['a'], 'a')).toEqual({ number: 1, count: 1, previous: null, next: null })
  })
})

describe('a decision on a text just shown', () => {
  it('waits 400 ms: the second click of a double click, or a double tap, lands on the next text as it shows', () => {
    expect(DECIDE_AFTER_MS).toBe(400)
    const shown = { id: 'b2', at: 1000 }
    expect(mayDecide(shown, 'b2', 1000)).toBe(false)
    expect(mayDecide(shown, 'b2', 1399)).toBe(false)
    expect(mayDecide(shown, 'b2', 1400)).toBe(true)
    expect(mayDecide(shown, 'b2', 9000)).toBe(true)
  })

  it('decides only the text shown: none before one shows, nor another', () => {
    expect(mayDecide(null, 'b2', 9000)).toBe(false)
    expect(mayDecide({ id: 'b1', at: 0 }, 'b2', 9000)).toBe(false)
  })
})
