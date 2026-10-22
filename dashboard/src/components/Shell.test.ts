import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { MeContext, type MeState } from '../lib/me'
import { Shell } from './Shell'

// The menu's links as rendered on a page, each with its classes and whether it is the current page.
function menuAt(path: string): { href: string; current: boolean; classes: string[] }[] {
  const value: MeState = { me: undefined, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const html = renderToStaticMarkup(h(MemoryRouter, { initialEntries: [path] }, h(MeContext.Provider, { value }, h(Shell, null, h('p')))))
  const nav = html.slice(html.indexOf('<nav'), html.indexOf('</nav>'))
  return [...nav.matchAll(/<a\b([^>]*)>/g)].map((match) => {
    const attributes = match[1] ?? ''
    return {
      href: /href="([^"]*)"/.exec(attributes)?.[1] ?? '',
      current: /aria-current="page"/.test(attributes),
      classes: (/class="([^"]*)"/.exec(attributes)?.[1] ?? '').split(/\s+/),
    }
  })
}

describe('Shell menu', () => {
  it('marks the current page by more than its colour in the icon rail', () => {
    const links = menuAt('/approvals')
    const current = links.filter((link) => link.current)
    expect(current.map((link) => link.href)).toEqual(['/approvals'])
    // Below lg the label is for screen readers only: the wash alone would say it by colour, so a ring says it too.
    expect(current[0]?.classes).toEqual(expect.arrayContaining(['bg-nav', 'max-lg:shadow-[inset_0_0_0_1.5px_var(--nav-ink)]']))
    // At desktop width the wash and the weight, with no line beside it.
    expect(current[0]?.classes).toContain('font-medium')
    expect(current[0]?.classes.filter((name) => name.includes('shadow') && !name.startsWith('max-lg:'))).toEqual([])
    for (const link of links.filter((one) => !one.current)) {
      expect(link.classes.some((name) => name.startsWith('max-lg:shadow'))).toBe(false)
    }
  })
})
