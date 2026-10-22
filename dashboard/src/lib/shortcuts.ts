// The keyboard shortcuts of Approvals, off until you turn them on (`approvals.shortcuts` in this browser's
// preferences): which key does what, and when a key is not one. A key never acts while you type (a field, a text
// area, a menu, an editable element, an IME composition), with Ctrl, Meta or Alt held, in a dialog, or when another
// handler took it first (the tutorial's Enter). Enter is not a shortcut: the Approve button sends a text as it is.

import { useEffect, useEffectEvent } from 'react'

export type ShortcutAction = 'approve' | 'skip' | 'next' | 'previous' | 'mask' | 'approveAll' | 'help'

/** What of a keydown decides whether it is a shortcut: a KeyboardEvent has all of it. */
export interface ShortcutEvent {
  key: string
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
  /** A key pressed while an input method composes a character. */
  isComposing: boolean
  /** The key is held down: the browser sends one event per repeat. */
  repeat: boolean
  /** Another handler took the key already. */
  defaultPrevented: boolean
  target: EventTarget | null
}

/**
 * The shortcuts, in the order their list shows them: the key (a letter in lower case, whatever Caps Lock does), with
 * Shift or not; `?` whatever Shift (the keyboards that need it for `?` and those that do not). Only moving goes on
 * while a key is held down: a text is never approved or dropped twice by a key held a moment too long.
 */
export const SHORTCUTS: readonly { action: ShortcutAction; key: string; shift?: boolean; repeats?: boolean }[] = [
  { action: 'approve', key: 'a' },
  { action: 'skip', key: 'x' },
  { action: 'next', key: 'j', repeats: true },
  { action: 'previous', key: 'k', repeats: true },
  { action: 'mask', key: 'm' },
  { action: 'approveAll', key: 'a', shift: true },
  { action: 'help', key: '?' },
]

/** Input types that take no typing: a key pressed on a checkbox or a switch is not text. */
const NOT_TYPED = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'range', 'color', 'file'])

/** The key is typed in an element: a field, a text area, a menu, an editable element. */
function typedIn(target: EventTarget | null): boolean {
  const element = target as { tagName?: string; type?: string; isContentEditable?: boolean } | null
  const tag = element?.tagName?.toLowerCase()
  if (tag === 'textarea' || tag === 'select') return true
  if (tag === 'input') return !NOT_TYPED.has((element?.type || 'text').toLowerCase())
  return element?.isContentEditable === true
}

/** The key is pressed in a dialog (a question, the menu of what a selection is): its keys are its own. */
function inDialog(target: EventTarget | null): boolean {
  const element = target as { closest?: (selector: string) => unknown } | null
  return typeof element?.closest === 'function' && element.closest('dialog, [role="dialog"]') != null
}

/** What the key pressed does, or null when it is not a shortcut. */
export function shortcutFor(event: ShortcutEvent): ShortcutAction | null {
  // A keydown made by autofill or an extension can come with no key at all.
  if (typeof event.key !== 'string') return null
  if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return null
  if (typedIn(event.target) || inDialog(event.target)) return null
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key
  const shortcut = SHORTCUTS.find((s) => s.key === key && (s.key === '?' || Boolean(s.shift) === event.shiftKey))
  if (!shortcut || (event.repeat && !shortcut.repeats)) return null
  return shortcut.action
}

/** The shortcuts are turned on in this browser: the preference kept is `true`, and no other value turns them on. */
export function turnedOn(kept: unknown): boolean {
  return kept === true
}

/** While `enabled`, the page's keys do what `handlers` say; a modal dialog open (a question, the list) keeps them. */
export function useApprovalShortcuts(enabled: boolean, handlers: Record<ShortcutAction, () => void>): void {
  const onKey = useEffectEvent((event: KeyboardEvent) => {
    const action = shortcutFor(event)
    if (action === null || document.querySelector('dialog[open]')) return
    event.preventDefault()
    handlers[action]()
  })
  useEffect(() => {
    if (!enabled) return
    const listener = (event: KeyboardEvent) => onKey(event)
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [enabled])
}
