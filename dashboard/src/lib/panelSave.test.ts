import { afterEach, describe, expect, it } from 'vitest'
import { ApiError, httpTransport, setTransport, type Method, type Transport } from '../api/client'
import type { ProjectDetail, Term } from '../api/types'
import { initialConfig } from '../mock/fixtures'
import {
  afterConflict,
  globalIo,
  keepUnsaved,
  personalIo,
  putChange,
  removeChange,
  RepoNotWritable,
  repoIo,
  rowChange,
  saveOne,
  updateChange,
  type Change,
  type ListAccess,
  type SaveIo,
} from './panelSave'
import { termRows } from './ruleTable'

const term = (value: string, type = 'CLIENT'): Term => ({ value, type, case_sensitive: false, whole_word: true })

interface Settings {
  terms: Term[]
  profile: string
}

/** The terms of the settings, as a table of the global rules shows them. */
const TERMS: ListAccess<Settings, Term> = {
  get: (settings) => settings.terms,
  set: (settings, terms) => ({ ...settings, terms }),
  rows: (terms) => termRows({ global: terms }),
}

const STALE = () => new ApiError(409, 'stale', [], { error: 'stale', reason: 'settings_stale' })
const PENDING = () => new ApiError(409, 'pending', [], { reason: 'settings_saved_change_pending', saved: true })

/** Settings read in turn, one per read, and what each write was sent; a write answers what `answers` says in turn. */
function fakeIo(reads: Settings[], answers: (Error | null)[] = []) {
  const writes: { settings: Settings; tag: string }[] = []
  let read = 0
  const io: SaveIo<Settings> = {
    read: async () => {
      const settings = reads[Math.min(read, reads.length - 1)]!
      read++
      return { settings: structuredClone(settings), tag: `tag-${read}` }
    },
    write: async (settings, tag) => {
      writes.push({ settings, tag })
      const answer = answers[writes.length - 1] ?? null
      if (answer) throw answer
      return { version: 7 }
    },
  }
  return { io, writes, reads: () => read }
}

const found: Settings = { profile: 'balanced', terms: [term('Project Aurora', 'PROJECT'), term('Whitfield & Co')] }
const auroraKey = TERMS.rows(found.terms)[0]!.key
const edit: Change<Settings> = putChange(TERMS, auroraKey, term('Project Aurora North', 'PROJECT'))

describe('saveOne', () => {
  it('saveOne saves the change on the settings as they are now', async () => {
    // Another term was added meanwhile, and the profile changed: the change is made on them, nothing of theirs lost.
    const now: Settings = { profile: 'max', terms: [...found.terms, term('Orion Labs')] }
    const { io, writes } = fakeIo([now])
    expect(await saveOne(io, found, edit)).toEqual({ kind: 'saved', version: 7 })
    expect(writes).toEqual([
      { settings: { profile: 'max', terms: [term('Project Aurora North', 'PROJECT'), term('Whitfield & Co'), term('Orion Labs')] }, tag: 'tag-1' },
    ])
  })

  it('saveOne retries once on a stale etag when the row is unchanged', async () => {
    const later: Settings = { ...found, terms: [...found.terms, term('Fernsby Studio')] }
    const { io, writes, reads } = fakeIo([found, later], [STALE()])
    expect(await saveOne(io, found, edit)).toEqual({ kind: 'saved', version: 7 })
    expect(reads()).toBe(2)
    expect(writes).toHaveLength(2)
    expect(writes[1]).toEqual({ settings: { ...later, terms: [term('Project Aurora North', 'PROJECT'), term('Whitfield & Co'), term('Fernsby Studio')] }, tag: 'tag-2' })
  })

  it('saveOne says the settings keep changing when the retry is stale too', async () => {
    const { io, writes } = fakeIo([found], [STALE(), STALE()])
    const outcome = await saveOne(io, found, edit)
    expect(outcome.kind).toBe('error')
    expect(writes).toHaveLength(2)
  })

  it('saveOne reports a conflict when the row changed', async () => {
    const changed: Settings = { ...found, terms: [{ ...term('Project Aurora', 'PROJECT'), case_sensitive: true }, term('Whitfield & Co')] }
    const { io, writes } = fakeIo([changed])
    expect(await saveOne(io, found, edit)).toEqual({ kind: 'conflict', settings: changed })
    expect(writes).toEqual([])
    // Changed between the stale write and the read after it: a conflict too, and no second write.
    const again = fakeIo([found, changed], [STALE()])
    expect(await saveOne(again.io, found, edit)).toEqual({ kind: 'conflict', settings: changed })
    expect(again.writes).toHaveLength(1)
  })

  it('saveOne says pending when the change waits for approval', async () => {
    const { io, writes } = fakeIo([found], [PENDING()])
    expect(await saveOne(io, found, edit)).toEqual({ kind: 'pending' })
    expect(writes).toHaveLength(1)
  })

  it('saveOne gives any other refusal as it came', async () => {
    const refused = new ApiError(400, 'invalid configuration', [{ loc: ['terms', 0], msg: 'too long' }])
    const { io } = fakeIo([found], [refused])
    expect(await saveOne(io, found, edit)).toEqual({ kind: 'error', error: refused })
  })
})

describe('the change a panel saves', () => {
  it('adds a row only while no row of its key is there', () => {
    const add = putChange(TERMS, null, term('Orion Labs'))
    expect(add.apply(found).terms.map((t) => t.value)).toEqual(['Project Aurora', 'Whitfield & Co', 'Orion Labs'])
    expect(add.unchanged(found, found)).toBe(true)
    expect(add.unchanged(found, { ...found, terms: [...found.terms, term('Orion Labs')] })).toBe(false)
    // The same value with another type is another term.
    expect(add.unchanged(found, { ...found, terms: [...found.terms, term('Orion Labs', 'OTHER')] })).toBe(true)
  })

  it('replaces its row, unless it changed, went, or the new identity is taken meanwhile', () => {
    expect(edit.unchanged(found, structuredClone(found))).toBe(true)
    expect(edit.unchanged(found, { ...found, terms: [term('Whitfield & Co')] })).toBe(false)
    const rename = putChange(TERMS, auroraKey, term('Orion Labs'))
    expect(rename.unchanged(found, { ...found, terms: [...found.terms, term('Orion Labs')] })).toBe(false)
  })

  it('removes exactly the rows selected, unless one of them changed meanwhile', () => {
    const rows = TERMS.rows(found.terms)
    const remove = removeChange(TERMS, new Set([rows[1]!.key]))
    const now = { ...found, terms: [...found.terms, term('Orion Labs')] }
    expect(remove.apply(now).terms.map((t) => t.value)).toEqual(['Project Aurora', 'Orion Labs'])
    expect(remove.unchanged(found, now)).toBe(true)
    expect(remove.unchanged(found, { ...found, terms: [found.terms[0]!, { ...term('Whitfield & Co'), whole_word: false }] })).toBe(false)
    // Given another type meanwhile, it is another term: the removal leaves it.
    const retyped = { ...found, terms: [found.terms[0]!, term('Whitfield & Co', 'PROJECT')] }
    expect(remove.apply(retyped).terms).toEqual(retyped.terms)
  })

  it('updates the rows selected one by one', () => {
    const rows = TERMS.rows(found.terms)
    const wholeWord = updateChange(TERMS, new Set([rows[0]!.key]), (t) => ({ ...t, whole_word: false }))
    expect(wholeWord.apply(found).terms.map((t) => t.whole_word)).toEqual([false, true])
    expect(wholeWord.unchanged(found, found)).toBe(true)
  })

  it('compares its row with the row as the panel found it, not with the settings the page read since', () => {
    // The panel opened on Project Aurora as in `found`; a selection changed the row since, and the page read it again:
    // the settings the page holds now are the changed ones, and the panel's save must not take them as found.
    const changed = { ...found, terms: [{ ...term('Project Aurora', 'PROJECT'), whole_word: false }, term('Whitfield & Co')] }
    const save = putChange(TERMS, auroraKey, term('Project Aurora North', 'PROJECT'), found.terms[0])
    expect(save.unchanged(changed, changed)).toBe(false)
    expect(save.unchanged(changed, found)).toBe(true)
  })
})

describe('rowChange', () => {
  const row = term('Orion Labs')

  it('says what an open panel does when the page reads its row again', () => {
    expect(rowChange(row, structuredClone(row), false)).toBe('same')
    expect(rowChange(row, { ...row, type: 'PROJECT' }, false)).toBe('refresh')
    expect(rowChange(row, undefined, false)).toBe('close')
    expect(rowChange(row, { ...row, type: 'PROJECT' }, true)).toBe('changed')
    expect(rowChange(row, undefined, true)).toBe('gone')
  })

  it('says what a save refused because its row changed shows: a new row keeps what was typed', () => {
    expect(afterConflict(true, row)).toBe('taken')
    expect(afterConflict(true, undefined)).toBe('taken')
    expect(afterConflict(false, row)).toBe('changed')
    expect(afterConflict(false, undefined)).toBe('gone')
  })
})

describe('keepUnsaved', () => {
  const base = { profile: 'balanced', history: 'full', terms: ['a'] }

  it('keepUnsaved keeps an edit whose field did not change meanwhile', () => {
    const draft = { ...base, profile: 'max' }
    const fresh = { ...base, terms: ['a', 'b'] }
    expect(keepUnsaved(base, draft, fresh, ['profile', 'history', 'terms'])).toEqual({ kept: { ...fresh, profile: 'max' }, lost: [] })
  })

  it('keepUnsaved drops and names an edit whose field changed meanwhile', () => {
    const draft = { ...base, profile: 'max', history: 'off' }
    const fresh = { ...base, profile: 'secrets' }
    expect(keepUnsaved(base, draft, fresh, ['profile', 'history', 'terms'])).toEqual({ kept: { ...fresh, history: 'off' }, lost: ['profile'] })
  })

  it('takes a field saved as the draft has it as saved: neither kept nor lost', () => {
    const draft = { ...base, profile: 'max' }
    expect(keepUnsaved(base, draft, { ...base, profile: 'max' }, ['profile'])).toEqual({ kept: { ...base, profile: 'max' }, lost: [] })
  })

  it('leaves alone a field not named', () => {
    const draft = { ...base, history: 'off' }
    expect(keepUnsaved(base, draft, base, ['profile'])).toEqual({ kept: base, lost: [] })
  })
})

describe('the settings a panel saves', () => {
  afterEach(() => setTransport(httpTransport))

  /** A daemon answering `project` and the settings' etag, and recording what is sent. */
  function daemon(project: ProjectDetail) {
    const sent: { method: Method; path: string; body: unknown }[] = []
    const transport: Transport = {
      ...httpTransport,
      mock: true,
      request: async (method, path, body) => {
        sent.push({ method, path, body })
        if (method === 'GET' && path === '/config') return { etag: 'etag-9' }
        if (method === 'GET' && path === `/projects/${project.id}`) return project
        return { ok: true, rules_version: 3 }
      },
    }
    setTransport(transport)
    return sent
  }

  const project = {
    id: 'p1',
    repo: { settings: { terms: [term('Project Falcon', 'PROJECT')] }, tag: 'file-1', writable: true },
    personal: { allow_values: ['billing-dev'] },
  } as unknown as ProjectDetail

  it('of the global rules: read with their etag, written as the save bar writes them', async () => {
    const sent: { method: Method; path: string; body: unknown }[] = []
    setTransport({
      ...httpTransport,
      mock: true,
      request: async (method, path, body) => {
        sent.push({ method, path, body })
        return method === 'GET' ? { ...initialConfig, etag: 'etag-4' } : { ok: true, rules_version: 5 }
      },
    })
    const io = globalIo()
    const now = await io.read()
    expect(now.tag).toBe('etag-4')
    expect(await io.write({ ...now.settings, terms: [term('Orion Labs')] }, now.tag)).toEqual({ version: 5 })
    const body = sent.at(-1)?.body as Record<string, unknown>
    expect(sent.at(-1)?.method).toBe('PUT')
    expect(body.etag).toBe('etag-4')
    expect(body.terms).toEqual([term('Orion Labs')])
    expect(body.allow_values).toEqual(initialConfig.allow_values)
    for (const readOnly of ['pii_available', 'recognizer_status', 'checksums', 'ner_status', 'data_effective']) expect(body).not.toHaveProperty(readOnly)
  })

  it('of your own for a project: read with the etag of the settings, written without empty lists', async () => {
    const sent = daemon(project)
    const io = personalIo('p1')
    const now = await io.read()
    expect(now).toEqual({ settings: { allow_values: ['billing-dev'] }, tag: 'etag-9' })
    await io.write({ ...now.settings, terms: [] }, now.tag)
    expect(sent.at(-1)).toEqual({ method: 'PUT', path: '/projects/p1/personal', body: { allow_values: ['billing-dev'], etag: 'etag-9' } })
  })

  it("of a project's latet.yaml: read with its tag, refused where the file cannot be written", async () => {
    const sent = daemon(project)
    const io = repoIo('p1')
    const now = await io.read()
    expect(now).toEqual({ settings: project.repo.settings, tag: 'file-1' })
    await io.write(now.settings, now.tag)
    expect(sent.at(-1)).toEqual({ method: 'PUT', path: '/projects/p1/repo', body: { settings: project.repo.settings, tag: 'file-1' } })
    expect(io.stale?.(new ApiError(409, 'the file changed since it was read'))).toBe(true)
    expect(io.stale?.(new ApiError(409, 'unreadable', [], { reason: 'repo_file_unreadable' }))).toBe(false)

    daemon({ ...project, repo: { ...project.repo, writable: false } })
    const readOnly = repoIo('p1')
    const read = await readOnly.read()
    await expect(readOnly.write(read.settings, read.tag)).rejects.toBeInstanceOf(RepoNotWritable)
  })
})
