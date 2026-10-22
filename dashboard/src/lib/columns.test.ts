import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { OutputColumn, OutputShape, ProjectDetail, Slot } from '../api/types'
import { hasStrictValues, heldByProject, holdsStructure, initialChecks, resends, rulesWith, strictLayer, strictOn, whereChoices } from './columns'

const column = (name: string, proposal: OutputColumn['proposal'], extra: Partial<OutputColumn> = {}): OutputColumn => ({
  name,
  masked: 4,
  proposal,
  reason: proposal === 'keep' ? 'states' : proposal === 'mask' ? 'person' : 'unclear',
  detail: null,
  kept_now: false,
  everywhere: true,
  examples: [],
  ...extra,
})

const COLUMNS = [
  column('id', 'keep', { reason: 'ids' }),
  column('stato', 'keep'),
  column('cliente', 'mask'),
  column('importo', 'ask', { reason: 'amounts' }),
  column('creato_il', 'keep', { reason: 'dates', kept_now: true }),
  column('note_*', 'keep'),
]

describe('strict data mode, guided', () => {
  it('checks at first the columns proposed in clear, and no other', () => {
    expect([...initialChecks(COLUMNS)]).toEqual(['id', 'stato']) // kept in clear already, or a name with a star: not checked
  })

  it('puts the chosen columns first, each once, in place of rules of the same name', () => {
    const before = [{ name: 'note_*', type: 'NOTE' }, { name: 'STATO', type: 'STATE' }]
    expect(rulesWith(before, COLUMNS, new Set(['stato', 'id']))).toEqual([
      { name: 'id', mask: false }, // in the order the output has them
      { name: 'stato', mask: false },
      { name: 'note_*', type: 'NOTE' },
    ])
  })

  it('offers only Everywhere when the project is not loaded, and says why', () => {
    expect(whereChoices({ id: 'p1', name: 'fattura-app', loaded: true })).toEqual({ project: true, notLoaded: false })
    expect(whereChoices({ id: 'p1', name: 'fattura-app', loaded: false })).toEqual({ project: false, notLoaded: true })
    expect(whereChoices(null)).toEqual({ project: false, notLoaded: false })
  })

  it('says when the earlier messages go again with the columns in clear', () => {
    expect(resends('codex')).toBe(true)
    expect(resends('opencode')).toBe(true)
    expect(resends('gemini-cli')).toBe(true)
    expect(resends('tutorial-agent')).toBe(true) // the tutorial's agent sends the whole conversation too
    expect(resends('claude-code')).toBe(false)
  })

  it('names the columns a rule of the project keeps masked whatever the global rules say', () => {
    const columns = [column('stato', 'keep', { everywhere: false }), column('id', 'keep')]
    expect(heldByProject(columns, new Set(['stato', 'id']))).toEqual(['stato'])
    expect(heldByProject(columns, new Set(['id']))).toEqual([])
  })

  it('shows the columns only on a tool output with values strict data mode masked', () => {
    const replacement = { start: 0, end: 1, masked_start: 0, masked_end: 1, type: 'DATA', class: 'STRUCT' as const, rule: 'strict:stato', entity: null, known: true }
    const slot: Slot = { path: '$', kind: 'tool_result', from_model: false, new: true, original: 'x', masked: 'y', replacements: [{ ...replacement, module: 'data' }] }
    expect(hasStrictValues(slot)).toBe(true)
    expect(hasStrictValues({ ...slot, kind: 'user_text' })).toBe(false)
    expect(hasStrictValues({ ...slot, replacements: [{ ...replacement, module: 'personal' }] })).toBe(false)
  })

  it('reads strict data mode for an exchange from its project, else from the global rules', () => {
    const project = (data: 'strict' | 'normal') => ({ effective: { data } }) as ProjectDetail
    // An exchange of no project, or of a project Latet has not loaded since it started: the global rules.
    expect(strictOn('strict', null)).toBe(true)
    expect(strictOn('normal', null)).toBe(false)
    // An exchange of a project: the project's settings as they apply, whatever the global rules say.
    expect(strictOn('normal', project('strict'))).toBe(true)
    expect(strictOn('strict', project('normal'))).toBe(false)
    expect(strictOn(undefined, project('strict'))).toBe(true)
    // Not known while what decides is loading.
    expect(strictOn('strict', undefined)).toBeUndefined()
    expect(strictOn(undefined, null)).toBeUndefined()
  })

  it("sets strict data mode in the project's rules when the project's own settings say `data`, else in the global rules", () => {
    const project = (personal: ProjectDetail['personal'], repo: ProjectDetail['repo']['settings']) =>
      ({ id: 'p1', personal, repo: { settings: repo } }) as ProjectDetail
    expect(strictLayer(null)).toBe('global') // an exchange of no project, or of one not loaded
    expect(strictLayer(project(null, {}))).toBe('global')
    expect(strictLayer(project({ data: null, profile: 'max' }, { profile: 'max' }))).toBe('global') // a profile is not `data`
    expect(strictLayer(project({ data: 'normal' }, {}))).toBe('project')
    expect(strictLayer(project({ data: 'strict' }, {}))).toBe('project')
    expect(strictLayer(project(null, { data: 'strict' }))).toBe('project')
    // A repository file can only turn it on: `normal` there is ignored, and does not decide.
    expect(strictLayer(project(null, { data: 'normal' }))).toBe('global')
  })
})

describe('structure in a tool output, as the daemon reads it', () => {
  const block = (kind: Slot['kind']): Slot => ({ path: '$', kind, from_model: false, new: true, original: 'x', masked: 'x', replacements: [] })
  const outputs = (n: number): Slot[] => Array.from({ length: n }, () => block('tool_result'))
  // The light answer (`?shape=1`): the columns' names alone, and none for a tool strict data mode does not cover.
  const answer = (names: string[], { strict = false, applies = true } = {}): OutputShape => ({ columns: names, strict, applies })
  const asking = (answers: Record<number, OutputShape | Error>) => {
    const asked: number[] = []
    const columns = async (slot: number) => {
      asked.push(slot)
      const found = answers[slot] ?? answer([])
      if (found instanceof Error) throw found
      return found
    }
    return { asked, columns }
  }

  it('asks the most recent tool outputs first and stops at the first the daemon reads columns in', async () => {
    const { asked, columns } = asking({ 1: answer(['id']), 3: answer(['status']) })
    const slots = [block('system'), block('tool_result'), block('user_text'), block('tool_result'), block('tool_result')]
    expect(await holdsStructure(slots, columns)).toBe(true)
    expect(asked).toEqual([4, 3]) // never a block that is not a tool's output, nothing past the first found
  })

  it('asks the ten most recent tool outputs at most', async () => {
    const { asked, columns } = asking({ 0: answer(['status']) })
    expect(await holdsStructure(outputs(12), columns)).toBe(false) // the oldest two are never asked
    expect(asked).toEqual([11, 10, 9, 8, 7, 6, 5, 4, 3, 2])
  })

  it('counts only the outputs strict data mode covers (not a file read nor a web page)', async () => {
    const read = asking({ 1: answer(['id', 'status'], { applies: false }), 0: answer(['status']) })
    expect(await holdsStructure(outputs(2), read.columns)).toBe(true)
    expect(read.asked).toEqual([1, 0])
    const only = asking({ 0: answer(['status'], { applies: false }) })
    expect(await holdsStructure(outputs(1), only.columns)).toBe(false)
  })

  it('finds none when no tool output has columns', async () => {
    const { asked, columns } = asking({})
    expect(await holdsStructure(outputs(2), columns)).toBe(false)
    expect(asked).toEqual([1, 0])
    expect(await holdsStructure([block('user_text')], asking({}).columns)).toBe(false)
  })

  it('says nothing where strict data mode is on after all, or where the history keeps no texts', async () => {
    const strict = asking({ 1: answer(['status'], { strict: true }) })
    expect(await holdsStructure(outputs(2), strict.columns)).toBe(false)
    expect(strict.asked).toEqual([1])
    const noTexts = asking({ 1: new ApiError(409, 'history: metadata keeps no texts') })
    expect(await holdsStructure(outputs(2), noTexts.columns)).toBe(false)
    expect(noTexts.asked).toEqual([1])
  })

  it('goes on past a block the daemon no longer finds, and passes any other failure on', async () => {
    const gone = asking({ 1: new ApiError(404, 'not found: no tool output there'), 0: answer(['status']) })
    expect(await holdsStructure(outputs(2), gone.columns)).toBe(true)
    const broken = asking({ 0: new ApiError(500, 'boom') })
    await expect(holdsStructure(outputs(1), broken.columns)).rejects.toThrow('boom')
  })
})
