// Keyboard shortcuts of a page (the tutorial's Enter means Next): which key presses are theirs to take. Approvals'
// own shortcuts, off until turned on, are in shortcuts.ts.

/** Elements that keep the keys pressed on them: Enter on a button presses it, not a shortcut. */
export const OWN_KEYS = 'input, textarea, select, button, a, summary, mark, dialog, [role="dialog"], [contenteditable]'

/** What decides whether a page shortcut may take a keydown. */
export interface KeyFacts {
  key: string
  /** The key is held down: the browser sends one event per repeat. */
  repeat: boolean
  altKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  shiftKey: boolean
  /** Something took the key already (a handler that ran first). */
  defaultPrevented: boolean
  /** It was pressed on an element that keeps its keys (`OWN_KEYS`). */
  ownedByTarget: boolean
  /** A modal dialog is open. */
  dialogOpen: boolean
}

/** The facts of a keydown event. */
export function keyFacts(event: KeyboardEvent): KeyFacts {
  const target = event.target instanceof Element ? event.target : null
  return {
    key: event.key,
    repeat: event.repeat,
    altKey: event.altKey,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
    defaultPrevented: event.defaultPrevented,
    ownedByTarget: target?.closest(OWN_KEYS) != null,
    dialogOpen: document.querySelector('dialog[open]') !== null,
  }
}
