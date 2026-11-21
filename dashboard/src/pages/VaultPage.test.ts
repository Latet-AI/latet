import { createElement as h, Fragment, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import { MeContext, type MeState } from '../lib/me'
import { computer, server } from '../test/eop35-fixtures'
import { VaultPage } from './VaultPage'

// The Vault page's search box as it renders. Which values a search finds, and what the listing shows, is the
// daemon's (tests/server and tests/ee); a search of an API key's vault is refused past 200 characters, so the box in
// a key's space takes no more.

const space = vi.hoisted(() => ({ open: null as string | null }))

vi.mock('../api/client', async (original) => ({
  ...(await original<typeof import('../api/client')>()),
  currentSpace: () => space.open,
}))

vi.mock('../components/Floating', async (original) => ({
  ...(await original<typeof import('../components/Floating')>()),
  Tooltip: ({ children }: { children: ReactNode }) => h(Fragment, null, children),
}))

function search(me: MeState['me']): string {
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(MeContext.Provider, { value }, h(VaultPage))))
  const box = /<input[^>]*type="search"[^>]*>/.exec(html)
  expect(box, 'the search box').not.toBeNull()
  return box![0]
}

beforeEach(async () => {
  space.open = null
  await i18n.changeLanguage('en')
})

describe('the Vault page search box', () => {
  it('takes no more than the 200 characters the server reads in an API key\'s space', () => {
    space.open = 'u_billingcix'
    expect(search(server('key'))).toContain('maxLength="200"')
  })

  it('is unbounded in your own space, on a computer and on a server', () => {
    expect(search(computer)).not.toMatch(/maxLength/i)
    expect(search(server('person'))).not.toMatch(/maxLength/i)
  })
})
