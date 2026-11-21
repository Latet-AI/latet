import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { ConnectedNotice, Me } from '../../api/types'
import { canMarkSeen, markSeen, noticeLines } from './notices'

const me = (session: Me['session']): Me => ({ session }) as unknown as Me

describe('the notice of computers connected and codes made', () => {
  it('can be marked as seen from a session the company sign-in opened, and from no other', () => {
    expect(canMarkSeen(me({ via: 'sso' }))).toBe(true)
    expect(canMarkSeen(me({ via: 'code' }))).toBe(false)
    expect(canMarkSeen(me(null))).toBe(false)
    expect(canMarkSeen(me(undefined))).toBe(false) // a server's older answer: a code session
    expect(canMarkSeen(undefined)).toBe(false)
  })

  it('lists every computer and every code, newest first, none called new', () => {
    const connected: ConnectedNotice = {
      until: 4821,
      computers: [
        { label: 'emily-laptop', connected: 1000 },
        { label: null, connected: 3000 },
      ],
      codes: [
        { made: 2000, by_administrator: true },
        { made: 500, by_administrator: false },
      ],
      more: 0,
    }
    const lines = noticeLines(connected)
    expect(lines.map((line) => line.text)).toEqual([
      'ee.notice.computerGone',
      'ee.notice.codeByAdministrator',
      'ee.notice.computer',
      'ee.notice.codeByYou',
    ])
    expect(lines.map((line) => line.when)).toEqual([3000, 2000, 1000, 500])
    expect(lines.find((line) => line.text === 'ee.notice.computer')?.label).toBe('emily-laptop')
    expect(new Set(lines.map((line) => line.key)).size).toBe(4)
  })
})

describe('marking the notice as seen', () => {
  it("tells the server what the person saw, up to the newest entry shown, then reads /me again", async () => {
    const steps: string[] = []
    const told = async (until: number) => {
      steps.push(`told ${until}`)
      return { ok: true, told: true }
    }
    expect(await markSeen(4821, told, () => steps.push('reload'))).toEqual({ ok: true })
    // The newest entry the notice named, not a later one: an entry made since is not said seen. /me is read after the
    // server took it, so that the notice goes from the page with what it told.
    expect(steps).toEqual(['told 4821', 'reload'])
  })

  it('reads nothing again, and gives the failure back, when the server refused', async () => {
    const refused = new ApiError(403, 'what your dashboard tells you is said seen from a session opened with the company sign-in')
    let reloaded = false
    const came = await markSeen(
      4821,
      async () => Promise.reject(refused),
      () => {
        reloaded = true
      },
    )
    expect(came).toEqual({ ok: false, error: refused }) // the page says it in a toast
    expect(reloaded).toBe(false)
  })

  it('is not a success when the server could not be reached', async () => {
    const offline = new Error('offline')
    expect(await markSeen(1, async () => Promise.reject(offline), () => undefined)).toEqual({ ok: false, error: offline })
    expect(await markSeen(1, async () => Promise.reject(undefined), () => undefined)).toMatchObject({ ok: false }) // whatever was thrown
  })
})
