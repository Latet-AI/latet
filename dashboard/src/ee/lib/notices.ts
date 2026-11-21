import type { ConnectedNotice, Me } from '../../api/types'
import { tk } from '../../i18n'

/** One line of the notice of computers and codes: when it happened, and what it says. */
export interface NoticeLine {
  key: string
  when: number
  /** A key of the translations; `label` is the computer's name, which its text names. */
  text: string
  label?: string
}

const COMPUTER = tk('ee.notice.computer')
const COMPUTER_GONE = tk('ee.notice.computerGone')
const CODE_BY_ADMINISTRATOR = tk('ee.notice.codeByAdministrator')
const CODE_BY_YOU = tk('ee.notice.codeByYou')

/**
 * Whether the notice can be marked as seen from this session: only one the company's sign-in opened (the server says no
 * to any other, and a session that does not say how it was opened is a code one). Else the notice goes by itself 14 days
 * after each entry.
 */
export function canMarkSeen(me: Me | undefined): boolean {
  return me?.session?.via === 'sso'
}

/** The computers connected and the codes made, newest first: every one the server lists, none called new. */
export function noticeLines(connected: ConnectedNotice): NoticeLine[] {
  const computers = connected.computers.map(
    (computer, i): NoticeLine =>
      computer.label === null
        ? { key: `computer:${i}`, when: computer.connected, text: COMPUTER_GONE }
        : { key: `computer:${i}`, when: computer.connected, text: COMPUTER, label: computer.label },
  )
  const codes = connected.codes.map(
    (code, i): NoticeLine => ({ key: `code:${i}`, when: code.made, text: code.by_administrator ? CODE_BY_ADMINISTRATOR : CODE_BY_YOU }),
  )
  return [...computers, ...codes].sort((a, b) => b.when - a.when)
}

/**
 * Says the person saw the notice up to its newest entry (`until`, what `/me.edition.connected` named), and reads /me
 * again once the server took it, so that the notice is gone. The server takes it from a session the company sign-in
 * opened alone; its refusal, or the server not answering, is given back for the page to say, and /me is not read again.
 */
export async function markSeen(
  until: number,
  told: (until: number) => Promise<unknown>,
  reload: () => void,
): Promise<{ ok: true } | { ok: false; error: unknown }> {
  try {
    await told(until)
  } catch (error) {
    return { ok: false, error }
  }
  reload()
  return { ok: true }
}
