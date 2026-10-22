import { describe, expect, it, vi } from 'vitest'
import { anyUnsaved, leavesPage, markUnsaved, subscribeUnsaved } from './unsaved'

// What is edited and not saved on a page (a panel's row, the save bar's settings), which leaving the page asks about
// first; the guard itself, in a browser, is in scripts/ui-checks.mjs.

describe('what is not saved', () => {
  it('is marked and let go by each part of the page, and said to those that listen', () => {
    const heard = vi.fn()
    const stop = subscribeUnsaved(heard)
    expect(anyUnsaved()).toBe(false)
    markUnsaved('panel', true)
    markUnsaved('form', true)
    expect(anyUnsaved()).toBe(true)
    markUnsaved('panel', false)
    expect(anyUnsaved()).toBe(true)
    markUnsaved('form', false)
    expect(anyUnsaved()).toBe(false)
    expect(heard).toHaveBeenCalledTimes(4)
    // Marked as it is already: nothing to say.
    markUnsaved('form', false)
    expect(heard).toHaveBeenCalledTimes(4)
    stop()
  })
})

describe('leavesPage', () => {
  it('leaves for another page or another project, not for another tab of the same one', () => {
    const rules = { pathname: '/rules', search: '' }
    expect(leavesPage(rules, { pathname: '/live', search: '' })).toBe(true)
    expect(leavesPage(rules, { pathname: '/rules', search: '?project=billing-app' })).toBe(true)
    expect(leavesPage(rules, { pathname: '/rules', search: '' })).toBe(false)
  })
})
