import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { Batch, SettingsChange } from '../api/types'
import {
  applyEvent,
  approveBody,
  batchKinds,
  changeOrigin,
  checkSelection,
  decidedBy,
  decidersText,
  inProject,
  moreDeciders,
  othersDecide,
  pendingOf,
  projectsWaiting,
  EMPTY_QUEUE,
  findValues,
  heldSaveWords,
  liveReviewWords,
  openSuggestions,
  placeholder,
  previewSent,
  selectedBatch,
  settingsEmptyWords,
  shortSession,
  tabsLabel,
  waitingCount,
  withoutChange,
  yourBatches,
  type AddedMask,
} from './approvals'

const batch = (id: string, created: number, kinds: Batch['items'][number]['kind'][] = ['user_text']): Batch => ({
  id,
  session: 'cc:3f2a9c1e-6b7d-4e21',
  project: null,
  agent: 'claude-code',
  created,
  expires: null,
  items: kinds.map((kind) => ({
    kind,
    path: null,
    tool: null,
    original: 'x',
    masked: 'x',
    replacements: [],
    suggestions: [],
    failed: null,
  })),
})

describe('the queue', () => {
  it('keeps the batches oldest first, each once, and drops those settled', () => {
    let state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [batch('b', 20), batch('a', 10)], changes: [], viewers: 1 })
    expect(state.loaded).toBe(true)
    expect(state.batches.map((b) => b.id)).toEqual(['a', 'b'])
    state = applyEvent(state, { type: 'pending', batch: batch('c', 15) })
    state = applyEvent(state, { type: 'pending', batch: batch('c', 15) })
    expect(state.batches.map((b) => b.id)).toEqual(['a', 'c', 'b'])
    state = applyEvent(state, { type: 'settled', id: 'c', state: 'approved' })
    expect(state.batches.map((b) => b.id)).toEqual(['a', 'b'])
    expect(applyEvent(state, { type: 'viewers', viewers: 3 }).viewers).toBe(3)
  })

  it('leaves the queue as it is when a text approved by the team is withheld as it leaves (Latet Enterprise)', () => {
    const state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [batch('a', 10)], changes: [], viewers: 1 })
    expect(applyEvent(state, { type: 'withheld', id: 'gone-already' })).toBe(state) // settled before: nothing to drop
    expect(applyEvent(state, { type: 'withheld', id: 'a' }).batches).toEqual([])
  })

  it('shows a batch masked again in its place, and none that no longer waits', () => {
    let state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [batch('a', 10), batch('b', 20)], changes: [], viewers: 1 })
    const again = batch('a', 10)
    again.items[0] = { ...again.items[0]!, original: 'mail ann@acme.com', masked: 'mail ann@acme.com' }
    state = applyEvent(state, { type: 'updated', batch: again })
    expect(state.batches.map((b) => b.id)).toEqual(['a', 'b'])
    expect(state.batches[0]!.items[0]!.masked).toBe('mail ann@acme.com')
    // Decided meanwhile: it does not come back.
    state = applyEvent(state, { type: 'settled', id: 'b', state: 'approved' })
    expect(applyEvent(state, { type: 'updated', batch: batch('b', 20) })).toBe(state)
  })

  it('shows the oldest batch, the one chosen, or the one that took its place', () => {
    const list = [batch('a', 1), batch('b', 2), batch('c', 3)]
    expect(selectedBatch(list, null)?.id).toBe('a')
    expect(selectedBatch(list, { id: 'b', index: 1 })?.id).toBe('b')
    expect(selectedBatch([list[0]!, list[2]!], { id: 'b', index: 1 })?.id).toBe('c') // b left: the next
    expect(selectedBatch([list[0]!], { id: 'c', index: 2 })?.id).toBe('a') // c was the last
    expect(selectedBatch([], { id: 'a', index: 0 })).toBeNull()
  })

  it('lists the kinds of a batch once each', () => {
    expect(batchKinds(batch('a', 1, ['tool_result', 'user_text', 'tool_result']))).toEqual(['tool_result', 'user_text'])
  })

  it('shortens a session key', () => {
    expect(shortSession('cc:3f2a9c1e-6b7d-4e21-9a0f')).toBe('cc:3f2a9c1e…')
    expect(shortSession('conv:abc')).toBe('conv:abc')
  })
})

const change = (id: string, created: number, patch: Partial<SettingsChange> = {}): SettingsChange => ({
  id,
  created,
  source: 'command',
  agent: 'hermes',
  loosened: ['values never to mask: 1 added'],
  keys: ['allow_values'],
  ...patch,
})

describe('a change to your settings', () => {
  it('comes with the snapshot, the newest one only, and counts among what waits', () => {
    const state = applyEvent(EMPTY_QUEUE, {
      type: 'snapshot',
      batches: [batch('a', 10)],
      changes: [change('c2', 20), change('c1', 5)],
      viewers: 1,
    })
    expect(state.changes.map((c) => c.id)).toEqual(['c2'])
    expect(waitingCount(state)).toBe(2)
    expect(waitingCount(EMPTY_QUEUE)).toBe(0)
  })

  it('waits once, and leaves when settled', () => {
    let state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [], changes: [], viewers: 1 })
    state = applyEvent(state, { type: 'change', change: change('c1', 10) })
    const again = applyEvent(state, { type: 'change', change: change('c1', 10) })
    expect(again).toBe(state)
    expect(state.changes.map((c) => c.id)).toEqual(['c1'])
    state = applyEvent(state, { type: 'change_settled', id: 'c1', state: 'approved' })
    expect(state.changes).toEqual([])
    expect(applyEvent(state, { type: 'change_settled', id: 'gone', state: 'stale' })).toBe(state)
  })

  it('is replaced by a newer one, whichever event comes first', () => {
    let state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [], changes: [change('c1', 10)], viewers: 1 })
    // The daemon settles the older one as superseded, then sends the new one.
    state = applyEvent(state, { type: 'change_settled', id: 'c1', state: 'superseded' })
    state = applyEvent(state, { type: 'change', change: change('c2', 20) })
    expect(state.changes.map((c) => c.id)).toEqual(['c2'])
    // Had the new one come first, it still replaces the older, and the late settle changes nothing.
    state = applyEvent(state, { type: 'change', change: change('c3', 30) })
    expect(state.changes.map((c) => c.id)).toEqual(['c3'])
    expect(applyEvent(state, { type: 'change_settled', id: 'c2', state: 'superseded' }).changes.map((c) => c.id)).toEqual(['c3'])
  })

  it('leaves the batches alone', () => {
    let state = applyEvent(EMPTY_QUEUE, { type: 'snapshot', batches: [batch('a', 10)], changes: [change('c1', 5)], viewers: 1 })
    state = applyEvent(state, { type: 'settled', id: 'c1', state: 'approved' }) // a batch's id, not a change's
    expect(state.changes.map((c) => c.id)).toEqual(['c1'])
    state = withoutChange(state, 'c1')
    expect(state.batches.map((b) => b.id)).toEqual(['a'])
    expect(state.changes).toEqual([])
  })

  it('says where it came from', () => {
    const t = (key: string, options?: Record<string, unknown>) => (options ? `${key} ${JSON.stringify(options)}` : key)
    expect(changeOrigin(t, { source: 'command', agent: 'hermes' })).toBe('approvals.change.source.command {"agent":"hermes"}')
    expect(changeOrigin(t, { source: 'command', agent: 'claude-code' })).toBe('approvals.change.source.command {"agent":"agents.claude-code"}')
    expect(changeOrigin(t, { source: 'command', agent: null })).toBe('approvals.change.source.commandNoAgent')
    expect(changeOrigin(t, { source: 'file', agent: null })).toBe('approvals.change.source.file')
    expect(changeOrigin(t, { source: 'start', agent: null })).toBe('approvals.change.source.start')
  })

  it('names no config.yaml on a Latet server', () => {
    const t = (key: string) => key
    expect(changeOrigin(t, { source: 'file', agent: null }, true)).toBe('approvals.change.source.serverFile')
    expect(changeOrigin(t, { source: 'start', agent: null }, true)).toBe('approvals.change.source.serverStart')
    expect(changeOrigin(t, { source: 'command', agent: null }, true)).toBe('approvals.change.source.commandNoAgent')
  })
})

describe('what someone else decides on', () => {
  const team = { decides: 'security' as const, deciders: ['Mark Ellis'] }

  it('does not count as waiting for you', () => {
    const state = applyEvent(EMPTY_QUEUE, {
      type: 'snapshot',
      batches: [{ ...batch('a', 10), ...team }, batch('b', 20)],
      changes: [change('c1', 5, team)],
      viewers: 1,
    })
    expect(waitingCount(state)).toBe(1)
    expect(othersDecide(state.changes[0]!)).toBe(true)
    expect(othersDecide(batch('b', 20))).toBe(false)
    expect(othersDecide({ decides: 'controllers' })).toBe(true)
    expect(othersDecide({ decides: 'self' })).toBe(false)
  })

  it('names who decides, or says nobody can yet', () => {
    const t = (key: string) => key
    const list = (items: readonly string[]) => items.join(' and ')
    expect(decidersText(t, list, ['Daniel Hayes', 'Mark Ellis'])).toBe('Daniel Hayes and Mark Ellis')
    expect(decidersText(t, list, [])).toBe('approvals.whoDecides.nobody')
    expect(decidersText(t, list, undefined)).toBe('approvals.whoDecides.nobody')
  })

  it('says how many more decide past the names the server gives', () => {
    // The server names 20 at most (MOST_NAMED) and counts the others (/me's deciders_more): the count is said, translated.
    const t = (key: string, options?: Record<string, unknown>) => (options ? `${key} ${JSON.stringify(options)}` : key)
    const list = (items: readonly string[]) => items.join(', ')
    expect(decidersText(t, list, ['Daniel Hayes', 'Mark Ellis'], 7)).toBe('Daniel Hayes, Mark Ellis, approvals.whoDecides.more {"count":7}')
    expect(decidersText(t, list, ['Daniel Hayes'], 0)).toBe('Daniel Hayes')
    // A count with no name is no one to ask.
    expect(decidersText(t, list, [], 3)).toBe('approvals.whoDecides.nobody')
  })

  it('takes /me’s count for an item whose names fill the server’s list', () => {
    // An item names 20 at most and carries no count: a full list takes the count /me gives for the same route.
    const twenty = Array.from({ length: 20 }, (_, i) => `Person ${i}`)
    expect(moreDeciders(twenty, 7)).toBe(7)
    expect(moreDeciders(twenty.slice(1), 7)).toBe(0) // fewer than 20: the whole team is named
    expect(moreDeciders(twenty, undefined)).toBe(0) // an older server's /me
    expect(moreDeciders(undefined, 7)).toBe(0)
  })

  it('finds the change a save made, waiting for the team', () => {
    const waiting = change('c9', 5, { ...team, source: 'dashboard' })
    expect(pendingOf(new ApiError(409, 'it lowers protection', [], { error: 'x', pending: waiting }))?.id).toBe('c9')
    expect(pendingOf(new ApiError(409, 'not in the tutorial’s sandbox', [], { error: 'x' }))).toBeNull()
    expect(pendingOf(new ApiError(400, 'invalid configuration', [], { pending: waiting }))).toBeNull()
    expect(pendingOf(new Error('boom'))).toBeNull()
  })

  it('tells who decided on your change when it was someone else, and nothing for your own decisions', () => {
    const settled = (state: 'approved' | 'rejected' | 'stale' | 'superseded' | 'approved-unapplied', by: string | null) =>
      decidedBy({ type: 'change_settled', id: 'c1', state, by })
    expect(settled('approved', 'Mark Ellis')).toEqual({ key: 'approvals.whoDecides.approvedBy', tone: 'ok' })
    expect(settled('rejected', 'Mark Ellis')).toEqual({ key: 'approvals.whoDecides.rejectedBy', tone: 'info' })
    // R60: approved, and the settings did not take it; or approved after they moved on (stale): nothing applied.
    expect(settled('approved-unapplied', 'Mark Ellis')).toEqual({ key: 'approvals.whoDecides.unappliedBy', tone: 'fail' })
    expect(settled('stale', 'Mark Ellis')).toEqual({ key: 'approvals.whoDecides.unappliedBy', tone: 'fail' })
    // Your own decisions, a newer change, and ends nobody decided name nobody.
    expect(settled('approved', null)).toBeNull()
    expect(settled('rejected', null)).toBeNull()
    expect(settled('superseded', null)).toBeNull()
    expect(decidedBy({ type: 'change_settled', id: 'c1', state: 'approved' })).toBeNull()
  })

  it('counts on the Live page only the texts waiting for you', () => {
    // The security team's texts do not wait for your approval: the banner that says so leaves them out.
    const batches = [{ ...batch('a', 10), ...team }, batch('b', 20), batch('c', 30)]
    expect(yourBatches(batches).map((b) => b.id)).toEqual(['b', 'c'])
    expect(yourBatches([{ ...batch('a', 10), ...team }])).toEqual([])
  })

  it('says where the Live Reviewer sends the texts: to you, or to your company’s security team', () => {
    const team = liveReviewWords({ live_review: 'security' })
    expect(team).toEqual({
      team: true,
      description: 'liveReview.descriptionSecurity',
      enabled: 'liveReview.enabledSecurity',
      kinds: 'liveReview.kindsSecurity',
      timeoutHelp: 'liveReview.timeoutHelpSecurity',
      switchOn: 'liveReview.switchOnSecurity',
      turnedOn: 'liveReview.turnedOnSecurity',
      page: 'approvals.descriptionSecurity',
      empty: 'approvals.empty.bodySecurity',
      off: 'approvals.off.bodySecurity',
      queue: 'approvals.queue.labelSecurity',
      projectHelp: 'projects.live.helpSecurity',
    })
    const yours = {
      team: false,
      description: 'liveReview.description',
      enabled: 'liveReview.enabled',
      kinds: 'liveReview.kinds',
      timeoutHelp: 'liveReview.timeoutHelp',
      switchOn: 'liveReview.switchOn',
      turnedOn: 'liveReview.turnedOn',
      page: 'approvals.description',
      empty: 'approvals.empty.body',
      off: 'approvals.off.body',
      queue: 'approvals.queue.label',
      projectHelp: 'projects.live.help',
    }
    // Yours on the Community Edition, on a computer, and wherever /me says nothing of it.
    expect(liveReviewWords({ live_review: 'self' })).toEqual(yours)
    expect(liveReviewWords(null)).toEqual(yours)
    expect(liveReviewWords(undefined)).toEqual(yours)
  })

  it('says who a change waits for while none does: you, your company’s security team, or an API key’s controllers', () => {
    expect(settingsEmptyWords({ changes: 'security' })).toBe('approvals.settingsEmpty.bodySecurity')
    expect(settingsEmptyWords({ changes: 'controllers' })).toBe('approvals.settingsEmpty.bodyControllers')
    expect(settingsEmptyWords({ changes: 'self' })).toBe('approvals.settingsEmpty.body')
    expect(settingsEmptyWords(null)).toBe('approvals.settingsEmpty.body')
    expect(settingsEmptyWords(undefined)).toBe('approvals.settingsEmpty.body')
  })

  it('names the tabs of Approvals for what waits on a decision when someone else decides on any of it', () => {
    expect(tabsLabel({ changes: 'self', live_review: 'self' })).toBe('approvals.tabs.label')
    expect(tabsLabel({ changes: 'security', live_review: 'self' })).toBe('approvals.tabs.labelOthers')
    expect(tabsLabel({ changes: 'self', live_review: 'security' })).toBe('approvals.tabs.labelOthers')
    expect(tabsLabel({ changes: 'controllers', live_review: 'self' })).toBe('approvals.tabs.labelOthers')
    expect(tabsLabel(null)).toBe('approvals.tabs.label')
    expect(tabsLabel(undefined)).toBe('approvals.tabs.label')
  })

  it('says a change found on a server runs as you approved it until someone else decides, when it is theirs', () => {
    const t = (key: string) => key
    const others = { decides: 'security' as const, agent: null }
    expect(changeOrigin(t, { ...others, source: 'file' }, true)).toBe('approvals.change.source.serverFileOthers')
    expect(changeOrigin(t, { ...others, source: 'start' }, true)).toBe('approvals.change.source.serverStartOthers')
    expect(changeOrigin(t, { decides: 'self', source: 'start', agent: null }, true)).toBe('approvals.change.source.serverStart')
  })

  it('says a held save reset the form only when the settings in use could be read again', () => {
    expect(heldSaveWords({ decides: 'security' }, true)).toBe('approvals.whoDecides.sent')
    expect(heldSaveWords({ decides: 'controllers' }, true)).toBe('approvals.whoDecides.sentControllers')
    // Not read again: the form still holds the change, and only its waiting is said.
    expect(heldSaveWords({ decides: 'security' }, false)).toBe('errors.settingsWaitingForOthers')
    expect(heldSaveWords({ decides: 'controllers' }, false)).toBe('errors.settingsWaitingForOthers')
  })

  it('says a save in the dashboard is where a change came from', () => {
    const t = (key: string) => key
    expect(changeOrigin(t, { source: 'dashboard', agent: null })).toBe('approvals.change.source.dashboard')
  })
})

describe('the queue of one project', () => {
  const batches = [{ project: 'p1' }, { project: null }, { project: 'p2' }, { project: 'p1' }]

  it('lists the projects waiting once, no project included', () => {
    expect(projectsWaiting(batches)).toEqual(['p1', null, 'p2'])
  })

  it('shows one project, those of no project, or all of them', () => {
    expect(inProject(batches, 'p1')).toEqual([batches[0], batches[3]])
    expect(inProject(batches, null)).toEqual([batches[1]])
    expect(inProject(batches, undefined)).toBe(batches)
    expect(inProject(batches, 'gone')).toBe(batches) // nothing waits there any more
  })
})

describe('a selection to mask', () => {
  const text = 'Call Giulia Ferri at 3 pm'

  it('leaves out the spaces around it', () => {
    expect(checkSelection(text, 4, 18, [], [])).toEqual({ ok: true, start: 5, end: 17, value: 'Giulia Ferri' })
  })

  it('refuses one character, nothing, or text masked already', () => {
    expect(checkSelection(text, 21, 22, [], [])).toEqual({ ok: false, reason: 'short' })
    expect(checkSelection(text, 4, 5, [], [])).toEqual({ ok: false, reason: 'empty' })
    expect(checkSelection(text, 5, 17, [{ start: 12, end: 17 }], [])).toEqual({ ok: false, reason: 'masked' })
    expect(checkSelection(text, 5, 11, [], [{ start: 10, end: 17 }])).toEqual({ ok: false, reason: 'added' })
  })

  it('takes whole words, as the daemon masks them, and something with letters or digits', () => {
    expect(checkSelection(text, 7, 14, [], [])).toEqual({ ok: true, start: 5, end: 17, value: 'Giulia Ferri' })
    expect(checkSelection('ok -- fine', 2, 6, [], [])).toEqual({ ok: false, reason: 'noword' })
    expect(checkSelection('keep __ as is', 5, 7, [], [])).toEqual({ ok: false, reason: 'noword' })
  })

  it('counts code points, as the daemon does', () => {
    const emoji = '👋 ciao Marta Greco'
    // "Marta Greco" starts at UTF-16 offset 8 and code point 7.
    expect(checkSelection(emoji, 8, 19, [], [])).toEqual({ ok: true, start: 7, end: 18, value: 'Marta Greco' })
    // A selection never cuts a character in half.
    expect(checkSelection(emoji, 1, 7, [], [])).toMatchObject({ ok: true, start: 0, value: '👋 ciao' })
  })
})

describe('the preview of what will be sent', () => {
  it('replaces every occurrence of a value outside the fakes, and moves the marks after it', () => {
    const masked = 'Giulia wrote to user_8f2k: Giulia agrees'
    const marks = [{ start: 16, end: 25, id: 'fake' }]
    const preview = previewSent(masked, marks, [{ value: 'Giulia', category: 'person', key: 0 }])
    expect(preview.text).toBe(`${placeholder('person')} wrote to user_8f2k: ${placeholder('person')} agrees`)
    expect(preview.added.map((a) => preview.text.slice(a.start, a.end))).toEqual(['⟦PERSON⟧', '⟦PERSON⟧'])
    expect(preview.text.slice(preview.marks[0]!.start, preview.marks[0]!.end)).toBe('user_8f2k')
    expect(preview.marks[0]!.id).toBe('fake')
  })

  it('leaves a value found inside a fake alone, and prefers the longer of two values', () => {
    expect(findValues('Ferri Giulia Ferri', [{ value: 'Ferri', key: 'a' }], [{ start: 0, end: 5 }])).toEqual([{ start: 13, end: 18, key: 'a' }])
    const found = findValues('Giulia Ferri', [{ value: 'Giulia', key: 'short' }, { value: 'Giulia Ferri', key: 'long' }], [])
    expect(found).toEqual([{ start: 0, end: 12, key: 'long' }])
  })

  it('is the masked text itself when nothing was added', () => {
    expect(previewSent('abc', [], [])).toEqual({ text: 'abc', marks: [], added: [] })
  })
})

describe('the suggestions of the local reviewer', () => {
  const item = {
    suggestions: [
      { start: 5, end: 17, category: 'person' },
      { start: 21, end: 25, category: 'unknown' },
    ],
  }

  it('keeps those of a known category that you have not masked yet', () => {
    expect(openSuggestions(item, [])).toEqual([{ start: 5, end: 17, category: 'person' }])
    expect(openSuggestions(item, [{ start: 5, end: 11 }])).toEqual([])
  })
})

describe('the approval', () => {
  it('sends the masks with their item, offsets, value and category: the daemon finds the value again in a text shown anew', () => {
    const masks: AddedMask[] = [{ item: 1, start: 5, end: 17, value: 'Giulia Ferri', category: 'person', always: true }]
    expect(approveBody(masks)).toEqual({
      action: 'approve',
      masks: [{ item: 1, start: 5, end: 17, value: 'Giulia Ferri', category: 'person' }],
    })
    expect(approveBody([])).toEqual({ action: 'approve' })
  })
})
