import { describe, expect, it } from 'vitest'
import type { ExchangeDetail, Leak, Slot } from '../api/types'
import { explainBlock, outboundTools, outboundValues, stoppedValues, waitedForTeam } from './blocks'

function detail(
  flags: string[],
  options: { leaks?: Leak[]; reason?: string; transport?: string; blocks?: string[] } = {},
): ExchangeDetail {
  return {
    summary: { flags, block_reason: options.reason ?? null, transport: options.transport ?? 'http' },
    request: { headers: [], slots: (options.blocks ?? []).map((block) => ({ block })), body_bytes: 0 },
    response: null,
    leaks: options.leaks ?? [],
  } as unknown as ExchangeDetail
}

const leak = (type: string, extra: Partial<Leak> = {}): Leak => ({ type, kind: 'system', preview: `${type}[5]`, new: true, ...extra })

describe('why a request was stopped', () => {
  it('is nothing for a request that left', () => {
    expect(explainBlock(detail([]))).toBeNull()
  })

  it("tells auto mode's reviewer apart, with the details it carried, each once and in order", () => {
    const why = explainBlock(
      detail(['blocked', 'leak', 'auto-mode-review'], {
        leaks: [leak('USERNAME', { detail: 'paths' }), leak('USERNAME', { detail: 'user_name' }), leak('USERNAME', { detail: 'paths' }), leak('URL_PASSWORD')],
      }),
    )
    expect(why).toMatchObject({ kind: 'review', details: ['user_name', 'paths'], others: ['URL_PASSWORD'] })
  })

  it("gives the agent's words from the private view, never the summary's category code as words", () => {
    const words = 'did not approve it in the Live Reviewer'
    const coded = { ...detail(['blocked'], { reason: 'refused' }), sensitive: { block_reason: words } }
    expect(explainBlock(coded)).toMatchObject({ kind: 'live', live: 'rejected', given: words })
    expect(explainBlock(detail(['blocked'], { reason: 'masking-failed' }))).toMatchObject({ kind: 'other', given: null })
    expect(explainBlock({ ...detail(['blocked'], { reason: 'too-large' }), sensitive: null })?.given).toBeNull()
  })

  it('tells the Claude Code plugin, an old conversation, and a leak through the gateway apart', () => {
    expect(explainBlock(detail(['blocked', 'leak'], { transport: 'plugin', leaks: [leak('EMAIL')] }))?.kind).toBe('plugin')
    expect(explainBlock(detail(['blocked', 'leak', 'history'], { transport: 'plugin', leaks: [leak('EMAIL')] }))?.kind).toBe('history')
    expect(explainBlock(detail(['blocked', 'leak'], { leaks: [leak('EMAIL')] }))?.kind).toBe('leak')
  })

  it("tells a value in what a tool takes by the reason given (gateway/mediate.py: TOOL_INPUT_WORDS)", () => {
    const reason =
      'sensitive values would leave in clear: EMAIL[21]; it is in the parameter schema of the tool mcp__crm__find, which Latet checks but cannot mask in this request: if it is not sensitive, choose Not sensitive in the dashboard (latet dashboard); otherwise turn off the MCP server or plugin that gives this tool'
    const why = explainBlock(detail(['blocked', 'leak'], { reason, leaks: [leak('EMAIL', { kind: 'tool_description' })] }))
    expect(why).toMatchObject({ kind: 'toolInput', given: reason })
    const plain = 'sensitive values would leave in clear: EMAIL[21]'
    expect(explainBlock(detail(['blocked', 'leak'], { reason: plain, leaks: [leak('EMAIL', { kind: 'tool_description' })] }))?.kind).toBe('leak')
  })

  it('tells what the max profile does not send', () => {
    expect(explainBlock(detail(['blocked', 'uninspected:image'])) ).toMatchObject({ kind: 'uninspected', uninspected: ['image'] })
  })

  it('tells images: block from the max profile by the reason given', () => {
    const reason = 'images: block is set, so requests that carry images or documents are not sent (image)'
    expect(explainBlock(detail(['blocked', 'uninspected:image'], { reason }))).toMatchObject({ kind: 'imagesSetting', uninspected: ['image'] })
    const mask = 'images: mask is set, so a request that carries what Latet cannot read and mask is not sent (image)'
    expect(explainBlock(detail(['blocked', 'uninspected:image'], { reason: mask }))).toMatchObject({ kind: 'imagesMask' })
  })

  it('tells the Live Reviewer and the local reviewer by the reason given', () => {
    const live = (reason: string) => explainBlock(detail(['blocked'], { reason }))
    expect(live('Latet did not send this request: you did not approve it in the Live Reviewer')).toMatchObject({ kind: 'live', live: 'rejected' })
    expect(live('Latet did not send this request: it waited ... longer than live_review.timeout_s')).toMatchObject({ live: 'expired' })
    expect(live('Latet did not send this request: the agent stopped waiting for it')).toMatchObject({ live: 'abandoned' })
    expect(live('the local reviewer could not read the user text (timeout), so it was not sent')?.kind).toBe('localReviewer')
    expect(live('masking failed')).toMatchObject({ kind: 'other', given: 'masking failed' })
  })

  it('tells a text your company’s security team stopped, or waited for, by the reason given (Latet Enterprise)', () => {
    // The daemon's words for a text the team decided on (gateway/approvals.py: REJECTED_BY_SECURITY,
    // EXPIRED_FOR_SECURITY); an abandoned one says nothing of who decided.
    const live = (reason: string) => explainBlock(detail(['blocked'], { reason }))
    const rejected = live("Latet did not send this request: your company's security team did not approve it in the Live Reviewer")
    expect(rejected).toMatchObject({ kind: 'live', live: 'rejected', team: true })
    const expired = live("Latet did not send this request: it waited for your company's security team longer than live_review.timeout_s")
    expect(expired).toMatchObject({ kind: 'live', live: 'expired', team: true })
    expect(live('Latet did not send this request: you did not approve it in the Live Reviewer')).toMatchObject({ team: false })
    expect(live('Latet did not send this request: the agent stopped waiting for it')).toMatchObject({ live: 'abandoned', team: false })
    expect(live('masking failed')).toMatchObject({ team: false })
  })

  it('says the team waited on an abandoned text when your texts go to them now, the reason saying nothing of it', () => {
    const live = (reason: string) => explainBlock(detail(['blocked'], { reason }))!
    const abandoned = live('Latet did not send this request: the agent stopped waiting for it')
    expect(waitedForTeam(abandoned, true)).toBe(true)
    expect(waitedForTeam(abandoned, false)).toBe(false)
    // A reason that names who decided wins over where texts go now.
    expect(waitedForTeam(live('Latet did not send this request: you did not approve it in the Live Reviewer'), true)).toBe(false)
    const team = live("Latet did not send this request: your company's security team did not approve it in the Live Reviewer")
    expect(waitedForTeam(team, false)).toBe(true)
  })

  it('tells a text the team approved that changed before it left, by the reason given (Latet Enterprise)', () => {
    // The daemon's words (gateway/approvals.py: APPROVED_CHANGED), after the gateway's "Latet did not send this request:".
    const reason =
      "Latet did not send this request: it would leave masked otherwise than your company's decider approved it (the rules changed meanwhile): it was not sent, and waits for them again when the agent sends it again"
    const changed = explainBlock(detail(['blocked'], { reason }))!
    expect(changed).toMatchObject({ kind: 'live', live: 'changed', team: true, given: reason })
    expect(waitedForTeam(changed, false)).toBe(true)
  })
})

describe('a tool outside this computer given a stand-in', () => {
  it('is its own kind of stop, with the tools named in the flags', () => {
    const why = explainBlock(detail(['blocked', 'outbound:WebSearch'], { transport: 'plugin' }))
    expect(why?.kind).toBe('outbound')
    expect(outboundTools(['outbound:web_search', 'leak'])).toEqual(['web_search'])
  })

  it('finds the real values behind the stand-ins, each once, counting code points', () => {
    const slot = {
      path: '$', kind: 'outbound_tool', from_model: true, new: true,
      original: '👋 Salvatore Sanfilippo and Salvatore Sanfilippo',
      masked: '👋 Sara Serra and Sara Serra',
      replacements: [
        { start: 2, end: 22, masked_start: 2, masked_end: 12, type: 'PERSON', known: true },
        { start: 27, end: 47, masked_start: 17, masked_end: 27, type: 'PERSON', known: true },
      ],
    } as unknown as Slot
    const other = { ...slot, kind: 'tool_input' } as Slot
    expect(outboundValues([slot, other])).toEqual([{ value: 'Salvatore Sanfilippo', sent: 'Sara Serra', type: 'PERSON' }])
  })
})

describe('the values that stopped a request', () => {
  const at = { block: 'b1', masked_start: 5, masked_end: 9, start: 7, end: 11 }

  it('are the new ones the daemon places in a block, and no others', () => {
    const stopped = stoppedValues([
      leak('GITHUB_PAT', at),
      leak('EMAIL', { ...at, new: false }), // sent before: flagged, it did not stop the request
      leak('PASSWORD', { ...at, allowed: true }), // let through to auto mode's reviewer
      leak('USERNAME', { ...at, detail: 'user_name' }), // auto mode's reviewer has choices of its own
      leak('IBAN'), // the history keeps no texts: nothing to point at
      leak('TOKEN', { block: 'b2', masked_start: 1, masked_end: 3 }), // no place on the original side
    ])
    expect(stopped.map((value) => value.type)).toEqual(['GITHUB_PAT'])
    expect(stopped[0]).toMatchObject(at)
  })

  it('are told once, however many times the same block was sent', () => {
    const twice = [leak('GITHUB_PAT', at), leak('GITHUB_PAT', at), leak('GITHUB_PAT', { ...at, block: 'b2' })]
    expect(stoppedValues(twice).map((value) => value.block)).toEqual(['b1', 'b2'])
  })

  it('are marked in the request only when the record still has the block they are in', () => {
    const flags = ['blocked', 'leak']
    expect(explainBlock(detail(flags, { leaks: [leak('GITHUB_PAT', at)], blocks: ['b1'] }))?.marked).toBe(true)
    expect(explainBlock(detail(flags, { leaks: [leak('GITHUB_PAT', at)], blocks: ['b9'] }))?.marked).toBe(false) // pruned
    expect(explainBlock(detail(flags, { leaks: [leak('GITHUB_PAT')], blocks: ['b1'] }))?.marked).toBe(false) // metadata only
  })
})
