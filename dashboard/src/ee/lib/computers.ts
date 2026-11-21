import type { ComputerOfYours } from '../../api/types'
import { tk } from '../../i18n'

/** One computer of the person's list: its name (null: it gave none), and the two lines said of it. */
export interface ComputerLine {
  key: string
  label: string | null
  connected: number
  lastUsed: number | null
  /** A key of the translations, with `when` the time it was connected. */
  how: string
  /** A key of the translations, with `when` the time it was last used, if it was. */
  used: string
}

const BY_ADMINISTRATOR = tk('ee.computers.connectedByAdministrator')
const BY_YOU = tk('ee.computers.connectedByYou')
const CONNECTED = tk('ee.computers.connected')
const LAST_USED = tk('ee.computers.lastUsed')
const NEVER_USED = tk('ee.computers.neverUsed')

/**
 * The computers connected to the person's account, newest first: when each was connected, with a code an administrator
 * made or one they made (or nothing said when the server's log does not say), and when it was last used.
 */
export function computerLines(computers: ComputerOfYours[]): ComputerLine[] {
  return [...computers]
    .sort((a, b) => b.connected - a.connected)
    .map((computer, i) => ({
      key: `computer:${i}`,
      label: computer.label.trim() || null,
      connected: computer.connected,
      lastUsed: computer.last_used,
      how: computer.by_administrator === null ? CONNECTED : computer.by_administrator ? BY_ADMINISTRATOR : BY_YOU,
      used: computer.last_used === null ? NEVER_USED : LAST_USED,
    }))
}
