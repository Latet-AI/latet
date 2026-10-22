import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it } from 'vitest'
import i18n from '../i18n'
import { SHORTCUTS } from '../lib/shortcuts'
import { ShortcutsDialog } from './ShortcutsDialog'

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ').trim()

/** The rows of the list: the keys and what they do, as read. */
function rows(html: string): Record<string, { keys: string; does: string }> {
  return Object.fromEntries(
    [...html.matchAll(/<div[^>]*data-shortcut="(\w+)"[^>]*><dt[^>]*>(.*?)<\/dt><dd[^>]*>(.*?)<\/dd><\/div>/g)].map((match) => [
      match[1],
      { keys: text(match[2] ?? ''), does: text(match[3] ?? '') },
    ]),
  )
}

const render = (enabled: boolean) =>
  renderToStaticMarkup(h(ShortcutsDialog, { open: false, onClose: () => undefined, enabled, onEnabled: () => undefined }))

describe('the list of keyboard shortcuts', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en')
  })

  it('is a dialog named by its title, with the switch that turns them on', () => {
    const html = render(false)
    expect(html).toMatch(/^<dialog[^>]*aria-labelledby="([^"]+)"/)
    const title = /aria-labelledby="([^"]+)"/.exec(html)?.[1]
    expect(html).toContain(`id="${title}"`)
    expect(text(html)).toContain(i18n.t('approvals.shortcuts.title'))
    expect(html).toMatch(/<input[^>]*type="checkbox"[^>]*role="switch"/)
    expect(text(html)).toContain(i18n.t('approvals.shortcuts.switch'))
    expect(html).not.toMatch(/role="switch"[^>]*checked/)
    expect(render(true)).toMatch(/role="switch"[^>]*checked/)
  })

  it('lists every shortcut once: its key, as on the keyboard, and what it does', () => {
    const list = rows(render(false))
    expect(Object.keys(list).sort()).toEqual(SHORTCUTS.map((s) => s.action).sort())
    expect(list.approve?.keys).toBe('A')
    expect(list.skip?.keys).toBe('X')
    expect(list.next?.keys).toBe('J')
    expect(list.previous?.keys).toBe('K')
    expect(list.mask?.keys).toBe('M')
    expect(list.approveAll?.keys).toBe(`${i18n.t('approvals.shortcuts.shift')} + A`)
    expect(list.help?.keys).toBe('?')
    for (const [action, row] of Object.entries(list)) {
      expect(row.does, action).toBe(i18n.t(`approvals.shortcuts.keys.${action}`))
      expect(i18n.exists(`approvals.shortcuts.keys.${action}`), action).toBe(true)
    }
  })

  it('says Shift as the keyboard of the language does', async () => {
    await i18n.changeLanguage('de')
    expect(rows(render(false)).approveAll?.keys).toBe('Umschalt + A')
  })
})
