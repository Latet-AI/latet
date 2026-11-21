import { describe, expect, it } from 'vitest'
import type { Me } from '../api/types'
import { enterpriseView, hasPage, visibleItems } from './edition'

const me = (pages: string[], state: string | null = null) =>
  ({
    edition: { name: 'enterprise', license: state ? { state } : null, features: [], pages, roles: [], actions: [] },
  }) as unknown as Me

describe('the Enterprise pages', () => {
  it('opens a page /me lists for this person, and no other', () => {
    expect(hasPage(me(['license']), 'license')).toBe(true)
    expect(hasPage(me(['license']), 'policy')).toBe(false)
  })

  it('opens none before /me answers, or where it lists none', () => {
    expect(hasPage(undefined, 'license')).toBe(false)
    expect(hasPage(me([]), 'license')).toBe(false)
  })

  it('keeps the menu items of the pages listed, in their order', () => {
    const items = [
      { to: '/policy', page: 'policy' },
      { to: '/license', page: 'license' },
    ]
    expect(visibleItems(items, me(['license', 'policy']))).toEqual(items)
    expect(visibleItems(items, me(['license']))).toEqual([{ to: '/license', page: 'license' }])
  })

  it('once a trial ended, shows every Enterprise page but License read-only', () => {
    const ended = me(['license', 'policy'], 'trial-ended')
    expect(enterpriseView(ended, 'policy')).toBe('read-only')
    expect(enterpriseView(ended, 'license')).toBe('page') // where a license is installed, or the trial removed
    expect(enterpriseView(me(['license', 'policy'], 'paused'), 'policy')).toBe('page') // a paid license's end: no
    expect(enterpriseView(ended, 'reports')).toBe('none') // not listed for this person: the page that does not exist
  })
})
