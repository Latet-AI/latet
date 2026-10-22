import { describe, expect, it } from 'vitest'
import type { ReviewRecord, Slot } from '../api/types'
import {
  canTrust,
  cleanGuidance,
  foundAfterSending,
  inClear,
  joinGuidance,
  lateTarget,
  needsHttps,
  readableFingerprint,
  reviewsOf,
  serverPlace,
} from './reviewer'

const slot = (block: string, masked: string): Slot => ({
  block,
  path: '$.messages[0].content',
  kind: 'tool_result',
  from_model: false,
  new: true,
  original: null,
  masked,
  replacements: [],
})

const review = (block: string, ts: number, findings: ReviewRecord['findings'], inline = true): ReviewRecord => ({
  id: `rv_${ts}`,
  ts,
  session: 's',
  project: null,
  block,
  kind: 'tool_result',
  inline,
  findings,
  failed: null,
  block_asked: false,
  seconds: 1.2,
})

describe('the reviewer in an exchange', () => {
  it('shows each finding with its text from the block, newest review first', () => {
    const slots = [slot('b1', 'il cliente Gianfranco Pavan ha chiamato'), slot('b2', 'ciao 👋 Marta Greco')]
    const reviews = [
      review('b1', 10, [{ category: 'person', masked: false, entity: null, at: [11, 27] }]),
      review('b2', 20, [{ category: 'person', masked: true, entity: 'e1', at: [7, 18] }]),
      review('other', 30, [{ category: 'health', masked: true, entity: 'e2', at: [0, 3] }]),
    ]
    const shown = reviewsOf(slots, reviews)
    expect(shown.map((s) => s.review.block)).toEqual(['b2', 'b1'])
    expect(shown[0]!.findings[0]!.text).toBe('Marta Greco') // offsets in code points, as Python counts them
    expect(shown[1]!.findings[0]!.text).toBe('Gianfranco Pavan')
  })

  it('shows a block reviewed twice once, with its newest review', () => {
    const slots = [slot('b1', 'Marta Greco')]
    const f = { category: 'person' as const, masked: false, entity: null, at: [0, 5] as [number, number] }
    const shown = reviewsOf(slots, [review('b1', 1, [f]), review('b1', 5, [f, f])])
    expect(shown).toHaveLength(1)
    expect(shown[0]!.review.ts).toBe(5)
  })

  it('counts what was found after sending', () => {
    const f = { category: 'person' as const, masked: true, entity: null, at: null }
    expect(foundAfterSending([review('a', 1, [f, f], false), review('b', 2, [f], true)])).toBe(2)
  })
})

describe('the notice of values found after sending', () => {
  const f = { category: 'person' as const, masked: true, entity: null, at: null }
  const late = [{ ...review('a', 5, [f], false), session: 'two' }, { ...review('b', 9, [f], false), session: 'one' }]

  it('leads to the newest exchange of the session of the newest finding', () => {
    const exchanges = [
      { id: 'x1', session: 'one', ts: 1 },
      { id: 'x3', session: 'one', ts: 3 },
      { id: 'x4', session: 'two', ts: 4 },
    ]
    expect(lateTarget(late, exchanges)).toEqual({ href: '/exchanges/x3', exchange: true })
  })

  it('leads to the session when none of its exchanges is listed', () => {
    expect(lateTarget(late, [])).toEqual({ href: '/live?session=one', exchange: false })
    expect(lateTarget([], [])).toBeNull()
  })
})

describe('where the reviewer server is', () => {
  it('tells this machine, the local network and the internet apart, as the daemon does', () => {
    expect(serverPlace('http://127.0.0.1:8080/v1')).toBe('here')
    expect(serverPlace('http://localhost:1234/v1')).toBe('here')
    expect(serverPlace('http://192.168.1.20:8080/v1')).toBe('network')
    expect(serverPlace('http://10.0.0.7/v1')).toBe('network')
    expect(serverPlace('http://gpu-box:8080/v1')).toBe('network')
    expect(serverPlace('http://bonsai.lan/v1')).toBe('network')
    expect(serverPlace('https://llm.example.com/v1')).toBe('internet')
    expect(serverPlace('not a url')).toBeNull()
  })

  it('asks for https on the internet only', () => {
    expect(needsHttps('http://llm.example.com/v1')).toBe(true)
    expect(needsHttps('https://llm.example.com/v1')).toBe(false)
    expect(needsHttps('http://192.168.1.20:8080/v1')).toBe(false)
  })
})

describe('a server reached unencrypted, or with a certificate of its own', () => {
  it('tells http on your network, and where a certificate can be trusted', () => {
    expect(inClear('http://gpu-box.lan:8080/v1')).toBe(true)
    expect(inClear('http://127.0.0.1:8080/v1')).toBe(false)
    expect(inClear('https://gpu-box.lan/v1')).toBe(false)
    expect(canTrust('https://192.168.1.20:8443/v1')).toBe(true)
    expect(canTrust('https://127.0.0.1:8443/v1')).toBe(false)
    expect(canTrust('http://gpu-box.lan/v1')).toBe(false)
  })

  it('prints a fingerprint the way openssl does', () => {
    expect(readableFingerprint('sha256:ab01' + '0'.repeat(60))).toMatch(/^AB:01:00:/)
  })
})

describe('your guidance to the reviewer', () => {
  it('is kept as the daemon keeps it: plain line ends, trimmed, empty is none', () => {
    expect(cleanGuidance('  Wards are confidential.\r\nSo are PRJ- codes.  ')).toBe('Wards are confidential.\nSo are PRJ- codes.')
    expect(cleanGuidance('   ')).toBeNull()
    expect(cleanGuidance(null)).toBeNull()
  })

  it("puts a project's after yours, with a blank line between", () => {
    expect(joinGuidance('Wards are confidential.', 'PRJ- codes are projects.')).toBe('Wards are confidential.\n\nPRJ- codes are projects.')
    expect(joinGuidance(null, ' PRJ- codes are projects. ')).toBe('PRJ- codes are projects.')
    expect(joinGuidance('', null)).toBeNull()
  })
})
