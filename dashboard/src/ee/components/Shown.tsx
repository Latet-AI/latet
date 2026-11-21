import { useReducer, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { CommandLine, CopyButton } from '../../components/ui'

/** One thing a page shows once: a command to run, or a secret (a code, a key, a token) to copy. */
export interface ShownLine {
  label: string
  text: string
  command?: boolean
}

/** What a page shows once, until it is closed: never kept anywhere, and gone when the page is left. */
export interface Shown {
  title: string
  lines: ShownLine[]
}

function SecretLine({ text }: { text: string }) {
  return (
    <div className="well flex items-center gap-2 py-1 pr-1 pl-3">
      <code className="mono min-w-0 flex-1 text-ui break-all text-ink">{text}</code>
      <CopyButton text={text} compact />
    </div>
  )
}

/** A command or a secret the server gave once (a one-time code, a key, a SCIM token), shown until it is closed. */
export function ShownOnce({ shown, onClose }: { shown: Shown; onClose: () => void }) {
  const { t } = useTranslation()
  return (
    <div role="status" className="well mb-4 space-y-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 max-w-[80ch] text-ui font-medium text-ink">{shown.title}</p>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
      {shown.lines.map((line) => (
        <div key={line.label} className="space-y-1">
          <p className="text-meta text-ink-3">{line.label}</p>
          {line.command ? <CommandLine command={line.text} /> : <SecretLine text={line.text} />}
        </div>
      ))}
    </div>
  )
}

/**
 * What a page keeps on screen from its changes: the secrets and commands the server gave once, each in a panel of its
 * own, and a note (a sentence that holds no secret). A secret stays until its own Close takes it away: another secret
 * made meanwhile is added beside it, and a note never takes its place.
 */
export interface Panels {
  secrets: { id: number; shown: Shown }[]
  note: string | null
  made: number // how many secrets were shown: the next one's id
}

export type PanelAction =
  | { kind: 'secret'; shown: Shown }
  | { kind: 'note'; text: string }
  | { kind: 'close'; id: number }
  | { kind: 'close-note' }

export const NO_PANELS: Panels = { secrets: [], note: null, made: 0 }

export function panelsReducer(state: Panels, action: PanelAction): Panels {
  switch (action.kind) {
    case 'secret':
      return { ...state, secrets: [...state.secrets, { id: state.made, shown: action.shown }], made: state.made + 1 }
    case 'note':
      return { ...state, note: action.text }
    case 'close':
      return { ...state, secrets: state.secrets.filter((held) => held.id !== action.id) }
    case 'close-note':
      return { ...state, note: null }
  }
}

/**
 * What the page puts on screen from the panels it holds: each secret in a panel of its own, in the order they were made,
 * then the note. A secret that was closed is gone from the panels the page holds, so nothing of it is left to show.
 */
export function panelsView(state: Panels, on: { close: (id: number) => void; closeNote: () => void }): ReactNode {
  return (
    <>
      {state.secrets.map((held) => (
        <ShownOnce key={held.id} shown={held.shown} onClose={() => on.close(held.id)} />
      ))}
      {state.note !== null && <ShownOnce shown={{ title: state.note, lines: [] }} onClose={on.closeNote} />}
    </>
  )
}

/**
 * `show` a secret or a command, `note` a sentence; `panels` is what the page puts where they appear; `held` is how many
 * secrets are on screen now (a page that makes one thing at a time offers to make it again once `held` is 0).
 */
export function usePanels(): { show: (shown: Shown) => void; note: (text: string) => void; panels: ReactNode; held: number } {
  const [state, dispatch] = useReducer(panelsReducer, NO_PANELS)
  const panels = panelsView(state, { close: (id) => dispatch({ kind: 'close', id }), closeNote: () => dispatch({ kind: 'close-note' }) })
  return {
    show: (shown) => dispatch({ kind: 'secret', shown }),
    note: (text) => dispatch({ kind: 'note', text }),
    panels,
    held: state.secrets.length,
  }
}

/** The panel that shows a code to connect a computer: the command to run, then the code it asks for, apart from it. */
export function connectShown(title: string, made: { command: string; code: string }, labels: { command: string; code: string }): Shown {
  return {
    title,
    lines: [
      { label: labels.command, text: made.command, command: true },
      { label: labels.code, text: made.code },
    ],
  }
}
