import { describe, expect, it } from 'vitest'
import { SHORTCUTS, shortcutFor, turnedOn, type ShortcutAction, type ShortcutEvent } from './shortcuts'

/** A key pressed on the page (on nothing in particular unless `more` says so). */
const press = (key: string, more: Partial<ShortcutEvent> = {}): ShortcutEvent => ({
  key,
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  isComposing: false,
  repeat: false,
  defaultPrevented: false,
  target: null,
  ...more,
})

/** An element a key can be pressed on, as the browser describes it. */
const element = (tagName: string, more: { type?: string; isContentEditable?: boolean; inDialog?: boolean } = {}) =>
  ({
    tagName: tagName.toUpperCase(),
    type: more.type,
    isContentEditable: more.isContentEditable ?? false,
    closest: (selector: string) => (more.inDialog && selector.includes('dialog') ? {} : null),
  }) as unknown as EventTarget

describe('the shortcuts of Approvals', () => {
  it('map A, X, J, K, M, Shift+A and ? to what they do', () => {
    expect(shortcutFor(press('a'))).toBe('approve')
    expect(shortcutFor(press('x'))).toBe('skip')
    expect(shortcutFor(press('j'))).toBe('next')
    expect(shortcutFor(press('k'))).toBe('previous')
    expect(shortcutFor(press('m'))).toBe('mask')
    expect(shortcutFor(press('A', { shiftKey: true }))).toBe('approveAll')
    expect(shortcutFor(press('?', { shiftKey: true }))).toBe('help')
    expect(shortcutFor(press('?'))).toBe('help') // a keyboard where ? needs no Shift
  })

  it('go by the letter whatever its case: Caps Lock does not make A approve all, Shift does', () => {
    expect(shortcutFor(press('A'))).toBe('approve')
    expect(shortcutFor(press('a', { shiftKey: true }))).toBe('approveAll')
    expect(shortcutFor(press('X', { shiftKey: true }))).toBeNull()
  })

  it('leave Enter alone: it approves nothing', () => {
    expect(shortcutFor(press('Enter'))).toBeNull()
    expect(shortcutFor(press('Enter', { shiftKey: true }))).toBeNull()
  })

  it('leave any key with Ctrl, Meta or Alt to the browser (Ctrl+A selects all)', () => {
    for (const modifier of ['ctrlKey', 'metaKey', 'altKey'] as const) {
      expect(shortcutFor(press('a', { [modifier]: true })), modifier).toBeNull()
      expect(shortcutFor(press('?', { [modifier]: true })), modifier).toBeNull()
    }
  })

  it('never act while you type: in a field, a text area, a menu, an editable element, or an IME composition', () => {
    for (const target of [
      element('input'),
      element('input', { type: 'text' }),
      element('input', { type: 'search' }),
      element('input', { type: 'email' }),
      element('textarea'),
      element('select'),
      element('div', { isContentEditable: true }),
    ]) {
      expect(shortcutFor(press('a', { target }))).toBeNull()
      expect(shortcutFor(press('j', { target }))).toBeNull()
    }
    expect(shortcutFor(press('a', { isComposing: true }))).toBeNull()
    expect(shortcutFor(press('Process', { isComposing: true }))).toBeNull()
  })

  it('act on a key pressed on a switch, a checkbox or a button, which take no typing', () => {
    expect(shortcutFor(press('a', { target: element('input', { type: 'checkbox' }) }))).toBe('approve')
    expect(shortcutFor(press('j', { target: element('button') }))).toBe('next')
    expect(shortcutFor(press('x', { target: element('body') }))).toBe('skip')
  })

  it('leave the keys to a dialog the key was pressed in (the menu of what a selection is, a question)', () => {
    expect(shortcutFor(press('a', { target: element('button', { inDialog: true }) }))).toBeNull()
  })

  it('leave a key another handler took (the tutorial)', () => {
    expect(shortcutFor(press('a', { defaultPrevented: true }))).toBeNull()
  })

  it('take a key held down once, except J and K, which keep moving', () => {
    expect(shortcutFor(press('a', { repeat: true }))).toBeNull()
    expect(shortcutFor(press('x', { repeat: true }))).toBeNull()
    expect(shortcutFor(press('m', { repeat: true }))).toBeNull()
    expect(shortcutFor(press('A', { shiftKey: true, repeat: true }))).toBeNull()
    expect(shortcutFor(press('j', { repeat: true }))).toBe('next')
    expect(shortcutFor(press('k', { repeat: true }))).toBe('previous')
  })

  it('take no key from an event that has none (a keydown of autofill or of an extension)', () => {
    for (const key of [undefined, null, 65]) {
      expect(shortcutFor(press('a', { key: key as unknown as string })), String(key)).toBeNull()
    }
  })

  it('list every action once, each found again from its own key', () => {
    const actions: ShortcutAction[] = ['approve', 'skip', 'next', 'previous', 'mask', 'approveAll', 'help']
    expect(SHORTCUTS.map((shortcut) => shortcut.action).sort()).toEqual([...actions].sort())
    for (const shortcut of SHORTCUTS) {
      expect(shortcutFor(press(shortcut.key, { shiftKey: Boolean(shortcut.shift) }))).toBe(shortcut.action)
    }
  })
})

describe('the choice kept in this browser', () => {
  it('turns the shortcuts on only when it is true: no other value kept there does', () => {
    expect(turnedOn(true)).toBe(true)
    for (const kept of [false, 'true', 1, 'yes', {}, [], null, undefined]) expect(turnedOn(kept), JSON.stringify(kept)).toBe(false)
  })
})
