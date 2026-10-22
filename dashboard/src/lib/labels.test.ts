import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import { parseFlag } from './flags'
import { AGENT_LABEL, AGENTS, classCounts, filterAgents, flagDetailKey, inferClass, MODULE_LABEL, MODULES, moduleCounts, typeClassesFor, type ViewClass } from './labels'

const guess = (type: string): ViewClass => inferClass(type, new Set(['PROJECT']))

describe('classCounts', () => {
  it("uses the daemon's by_class, mapping lost classes to UNKNOWN", () => {
    const summary = { by_type: { EMAIL: 2, PASSWORD: 1 }, by_class: { PII: 2, SECRET: 1, '?': 1 } }
    expect(classCounts(summary, guess)).toEqual({ counts: { PII: 2, SECRET: 1, UNKNOWN: 1 }, guessed: false })
  })

  it('falls back to a guess from the types for records without by_class', () => {
    const summary = { by_type: { EMAIL: 2, PROJECT: 1, GITHUB_PAT: 1, CRYPTO_WALLET: 1 } }
    expect(classCounts(summary, guess)).toEqual({ counts: { PII: 3, CUSTOM: 1, SECRET: 1 }, guessed: true })
  })
})

describe('typeClassesFor', () => {
  it('keeps the per-type guess when it adds up to by_class', () => {
    const classOf = typeClassesFor({ by_type: { EMAIL: 2, PASSWORD: 1 }, by_class: { PII: 2, SECRET: 1 } }, guess)
    expect([classOf('EMAIL'), classOf('PASSWORD')]).toEqual(['PII', 'SECRET'])
  })

  it('colors every type with the only class when the guess disagrees', () => {
    // PERSON is guessed as identity, but here the local model found it.
    const classOf = typeClassesFor({ by_type: { PERSON: 2, CITY: 1 }, by_class: { NER: 3 } }, guess)
    expect([classOf('PERSON'), classOf('CITY')]).toEqual(['NER', 'NER'])
  })

  it('leaves types uncolored rather than guess wrong', () => {
    const classOf = typeClassesFor({ by_type: { EMAIL: 1, PERSON: 1 }, by_class: { IDENTITY: 1, NER: 1 } }, guess)
    expect(classOf('EMAIL')).toBeUndefined()
  })
})

describe('moduleCounts', () => {
  it('lists the modules that masked something, in their order', () => {
    expect(moduleCounts({ by_module: { personal: 2, secrets: 1, terms: 0 } })).toEqual([
      ['secrets', 1],
      ['personal', 2],
    ])
    expect(moduleCounts({})).toEqual([]) // an older record
  })

  it('has a label for every module', () => {
    for (const m of MODULES) expect(MODULE_LABEL[m]).toBe(`modules.${m}`)
  })
})

describe('the agents', () => {
  it('has a label for the pretend agent of the tutorial, before the others', () => {
    expect(AGENT_LABEL['tutorial-agent']).toBe('agents.tutorial-agent')
    expect(AGENTS.at(-1)).toBe('other')
  })

  it('names Gemini CLI', () => {
    expect(AGENT_LABEL['gemini-cli']).toBe('agents.gemini-cli')
  })

  it('offers the pretend agent to a filter only inside its sandbox', () => {
    expect(filterAgents(false)).toEqual(['claude-code', 'codex', 'opencode', 'gemini-cli', 'other'])
    expect(filterAgents(true)).toEqual(['claude-code', 'codex', 'opencode', 'gemini-cli', 'tutorial-agent', 'other'])
  })
})

describe('flagDetailKey', () => {
  const said = (raw: string) => {
    const key = flagDetailKey(parseFlag(raw))
    return key === null ? null : i18n.t(key)
  }

  it("says in the reader's words what a request carried that Latet could not read, and what became of an image", () => {
    expect(said('uninspected:image')).toBe('an image')
    expect(said('uninspected:document')).toBe('a document')
    expect(said('uninspected:input_file')).toBe('a file')
    expect(said('uninspected:input_audio')).toBe('audio')
    expect(said('image:masked')).toBe('masked')
    expect(said('image:withheld')).toBe('withheld')
  })

  it("leaves a detail it does not know, and the details that name something, as the daemon wrote them", () => {
    expect(flagDetailKey(parseFlag('uninspected:container_upload'))).toBeNull()
    expect(flagDetailKey(parseFlag('degraded:pii'))).toBeNull()
    expect(flagDetailKey(parseFlag('outbound:WebSearch'))).toBeNull()
    expect(flagDetailKey(parseFlag('blocked'))).toBeNull()
  })
})
