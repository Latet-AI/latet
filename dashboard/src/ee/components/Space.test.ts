import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { switchSpace } from '../../api/client'
import type { Me, ServerIdentity } from '../../api/types'
import { MeContext, type MeState } from '../../lib/me'
import type { ApiKey, KeysList } from '../types'

// The keys the menu lists are the person's own: the page's one read of them is answered here.
const answer: { keys?: KeysList } = {}
vi.mock('../../lib/useAsync', () => ({
  useAsync: (key: string | null) => ({
    data: key === null ? undefined : answer.keys,
    error: undefined,
    loading: false,
    stale: false,
    reload: () => undefined,
    mutate: () => undefined,
  }),
}))

const { SpaceBar, SpaceMenu, spaceOffered, spaceOptions } = await import('./Space')

const key = (id: string, name: string, disabled = false): ApiKey => ({ id, name, disabled, created: 1, last_seen: null, controllers: [] })

function me(more: { features?: string[]; state?: string; identity?: ServerIdentity } = {}): Me {
  return {
    server: { url: 'https://latet.acme.lan', identity: more.identity ?? { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' } },
    edition: { features: more.features ?? ['roles'], license: { state: more.state ?? 'valid' } },
  } as unknown as Me
}

function render(component: () => ReturnType<typeof h>, state: Me | undefined): string {
  const value: MeState = { me: state, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  return renderToStaticMarkup(h(MeContext.Provider, { value }, component()))
}

/** Opens a key's space in this tab, as the sidebar's menu does (the page would start again there). */
function open(id: string | null): void {
  vi.stubGlobal('window', { sessionStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined }, location: { assign: () => undefined } })
  switchSpace(id)
}

afterEach(() => {
  open(null)
  vi.unstubAllGlobals()
})

describe('the way into an API key', () => {
  it('lists the keys a person controls, apart from those disabled, and nothing when there is none', () => {
    answer.keys = { keys: [key('u_billingcix', 'Billing CI'), key('u_nightlyimp', 'Nightly import', true)], manage: false }
    const html = render(() => h(SpaceMenu), me())
    expect(html).toContain('Your own')
    expect(html).toContain('API key: Billing CI')
    expect(html).not.toContain('Nightly import')
    answer.keys = { keys: [], manage: false }
    expect(render(() => h(SpaceMenu), me())).toBe('')
  })

  it('offers no key to a server whose license has no roles, nor after a trial ended', () => {
    answer.keys = { keys: [key('u_billingcix', 'Billing CI')], manage: true }
    expect(render(() => h(SpaceMenu), me({ features: ['sso'] }))).toBe('')
    expect(render(() => h(SpaceMenu), me({ state: 'trial-ended' }))).toBe('')
    expect(render(() => h(SpaceMenu), undefined)).toBe('')
  })

  it("keeps the open key in the list, and the way back, whatever the list says", () => {
    open('u_billingcix')
    answer.keys = { keys: [], manage: false }
    const html = render(() => h(SpaceMenu), me({ identity: { id: 'u_billingcix', name: 'Billing CI', kind: 'key' } }))
    expect(html).toContain('API key: Billing CI')
    expect(html).toContain('<option value="">Your own</option>')
    expect(html).toContain('value="u_billingcix"')
  })

  it("shows the bar above the pages of a key's space, with the key's name and the way back", () => {
    open('u_billingcix')
    const html = render(() => h(SpaceBar), me({ identity: { id: 'u_billingcix', name: 'Billing CI', kind: 'key' } }))
    expect(html).toContain('You are in the space of the API key Billing CI')
    expect(html).toContain('Back to your space')
  })

  it("keeps the way back when /me cannot say whose space it is: the tab's own space is what decides", () => {
    open('u_billingcix')
    const html = render(() => h(SpaceBar), undefined)
    expect(html).toContain('You are in the space of an API key')
    expect(html).toContain('Back to your space')
    expect(render(() => h(SpaceBar), me())).toContain('Back to your space') // /me through the space said a person
  })

  it('shows no bar in your own space', () => {
    expect(render(() => h(SpaceBar), me({ identity: { id: 'u_billingcix', name: 'Billing CI', kind: 'key' } }))).toBe('')
  })
})

// Below 1024 px the sidebar is a rail of icons 60 px wide: the labelled select does not fit, and a person who controls
// an API key but is not an administrator has no other way into its space.
describe('the way into an API key on a narrow screen', () => {
  /** The two controls the menu renders: [the labelled select, the one of the icon rail]. */
  const controls = (html: string) => {
    const wide = /<label class="block max-lg:hidden">[\s\S]*?<\/label>/.exec(html)?.[0]
    const narrow = /<div class="relative lg:hidden[^"]*"[^>]*>[\s\S]*?<\/div>/.exec(html)?.[0]
    return { wide, narrow }
  }
  const optionsOf = (control: string | undefined) => [...(control ?? '').matchAll(/<option value="([^"]*)"/g)].map((match) => match[1])

  it('is offered in the rail beside the labelled menu, with the same spaces', () => {
    answer.keys = { keys: [key('u_billingcix', 'Billing CI'), key('u_nightlyimp', 'Nightly import', true)], manage: false }
    const { wide, narrow } = controls(render(() => h(SpaceMenu), me()))
    expect(wide).toBeDefined()
    expect(narrow).toBeDefined()
    expect(optionsOf(narrow)).toEqual(['', 'u_billingcix'])
    expect(optionsOf(narrow)).toEqual(optionsOf(wide)) // a disabled key is in neither
    // It is a select with a name of its own for a screen reader, and the rail shows an icon.
    expect(narrow).toMatch(/<select[^>]*aria-label="Space"/)
    expect(narrow).toContain('<svg')
    expect(narrow).toContain('API key: Billing CI')
  })

  it("shows in the rail that a key's space is open, and which", () => {
    answer.keys = { keys: [key('u_billingcix', 'Billing CI')], manage: false }
    const own = controls(render(() => h(SpaceMenu), me())).narrow
    expect(own).toContain('data-space-open="false"')
    open('u_billingcix')
    const inKey = controls(render(() => h(SpaceMenu), me({ identity: { id: 'u_billingcix', name: 'Billing CI', kind: 'key' } }))).narrow
    expect(inKey).toContain('data-space-open="true"')
    expect(inKey).toContain('<option value="u_billingcix" selected="">API key: Billing CI</option>')
  })

  it('is not there when the labelled menu is not: no key to open, no roles, a trial that ended', () => {
    answer.keys = { keys: [], manage: false }
    expect(render(() => h(SpaceMenu), me())).toBe('')
    answer.keys = { keys: [key('u_billingcix', 'Billing CI')], manage: false }
    expect(render(() => h(SpaceMenu), me({ features: ['sso'] }))).toBe('')
    expect(render(() => h(SpaceMenu), me({ state: 'trial-ended' }))).toBe('')
  })

  it('offers a server whose license has roles, and nothing after a trial ended', () => {
    expect(spaceOffered(me())).toBe(true)
    expect(spaceOffered(me({ features: ['sso', 'siem'] }))).toBe(false)
    expect(spaceOffered(me({ state: 'trial-ended' }))).toBe(false)
    expect(spaceOffered(undefined)).toBe(false)
    expect(spaceOffered({ edition: { features: ['roles'], license: { state: 'valid' } } } as unknown as Me)).toBe(false) // a computer: no server
  })

  it("lists the keys that are not disabled, and the open one whatever the list says, named by who is signed in", () => {
    const keys = [key('u_billingcix', 'Billing CI'), key('u_nightlyimp', 'Nightly import', true)]
    expect(spaceOptions(keys, null, undefined)).toEqual([{ id: 'u_billingcix', name: 'Billing CI' }])
    // A key whose space is open but which the list lacks (disabled meanwhile, or not listed yet) is there, so that the way back is.
    expect(spaceOptions(keys, 'u_nightlyimp', undefined)).toEqual([
      { id: 'u_billingcix', name: 'Billing CI' },
      { id: 'u_nightlyimp', name: 'Nightly import' },
    ])
    expect(spaceOptions([], 'u_billingcix', { id: 'u_billingcix', name: 'Billing CI', kind: 'key' })).toEqual([{ id: 'u_billingcix', name: 'Billing CI' }])
    expect(spaceOptions([], 'u_billingcix', { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' })).toEqual([{ id: 'u_billingcix', name: 'u_billingcix' }])
    expect(spaceOptions([], null, undefined)).toEqual([])
  })
})
