import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Me } from '../../api/types'
import { MeContext, type MeState } from '../../lib/me'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { ApiKey, Group, KeysList, PeopleList, Person } from '../types'

// The page as it renders for what the server answered: each of its three reads gets its own answer.
const answers: { 'ee-people'?: PeopleList; 'ee-groups'?: { groups: Group[] }; 'ee-keys'?: KeysList } = {}
vi.mock('../../lib/useAsync', () => ({
  useAsync: (key: keyof typeof answers | null) => ({
    data: key === null ? undefined : answers[key],
    error: undefined,
    loading: false,
    stale: false,
    reload: () => undefined,
    mutate: () => undefined,
  }),
}))

const { PeoplePage } = await import('./PeoplePage')

const admins = { id: 'g_latetadmns', name: 'Latet admins' }
const person = (id: string, name: string, more: Partial<Person> = {}): Person => ({
  id,
  name,
  email: `${name.toLowerCase().replace(' ', '.')}@acme.com`,
  user_name: null,
  disabled: false,
  source: 'local',
  last_seen: 1_790_000_000,
  linked: true,
  claimable: false,
  scim_managed: false,
  disabled_by: null,
  groups: [],
  roles: [],
  through: [],
  ...more,
})
const key: ApiKey = { id: 'u_billingcix', name: 'Billing CI', disabled: false, created: 1_780_000_000, last_seen: null, controllers: [] }

interface Rendered {
  /** The markup but for the dialogs (closed, they hold the labels of their buttons). */
  html: string
  text: string
}

function page(actions: string[], { readOnly = false, manage = true } = {}): Rendered {
  answers['ee-people'] = {
    enabled: 3,
    people: [
      person('u_emilycartr', 'Emily Carter', { source: 'scim', groups: [{ ...admins, source: 'scim' }], through: [{ role: 'admin', group: admins }] }),
      person('u_tombeckerx', 'Tom Becker', { last_seen: null }), // added by hand, no computer connected yet
      person('u_samriverax', 'Sam Rivera', { disabled: true, disabled_by: 'local', source: 'scim' }),
    ],
  }
  answers['ee-groups'] = { groups: [{ ...admins, source: 'scim', external_id: '8c1d2e3f', members: [], roles: ['admin'] }] }
  answers['ee-keys'] = { keys: [key], manage }
  const me = { edition: { actions } } as unknown as Me
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const tree = readOnly ? h(EnterpriseReadOnly.Provider, { value: true }, h(PeoplePage)) : h(PeoplePage)
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(MeContext.Provider, { value }, tree))).replace(/<dialog[\s\S]*?<\/dialog>/g, '')
  return { html, text: html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ') }
}

/** Whether the markup has a button or a link with this label. */
const has = (html: string, label: string) => new RegExp(`<(button|a)[^>]*>${label}<`).test(html)

const BUTTONS = ['Add a person', 'Add a group', 'Make a key', 'Open its space', 'New key', 'Disable', 'Enable', 'Code', 'Roles']

describe('the People and groups page', () => {
  it("lists the people, their groups and roles, the groups, the keys and the roles' table", () => {
    const { text } = page(['people.manage'])
    for (const words of ['Emily Carter', 'Tom Becker', 'Sam Rivera', 'Latet admins', 'Billing CI', 'Everyone is a user.', 'People, groups and API keys']) {
      expect(text).toContain(words)
    }
    expect(text).toContain('3 enabled accounts')
    expect(text).toContain('Never') // Tom has not been seen
  })

  it('says who disabled a person, and that SCIM does not undo an administrator', () => {
    expect(page(['people.manage']).text).toContain('Disabled by an administrator: SCIM does not enable them.')
  })

  it('warns that a role given to a group from SCIM is given to whoever holds a token', () => {
    expect(page(['people.manage']).text).toContain('A role given to a group from SCIM is also given to whoever holds a SCIM token')
  })

  it('offers a code only where no computer is connected yet', () => {
    // Emily has connected hers: her Code button is disabled and says what makes a new code. Tom has none; Sam is disabled.
    const { html } = page(['people.manage'])
    const codes = [...html.matchAll(/<button([^>]*)>Code<\/button>/g)].map((match) => match[1] ?? '')
    expect(codes).toHaveLength(2)
    expect(codes[0]).toContain('disabled=""')
    expect(codes[0]).toContain('latet server people code')
    expect(codes[1]).not.toContain('disabled')
    expect(html.split('latet server people code').length - 1).toBe(1)
  })

  it('gives the administrators their forms and actions', () => {
    const { html } = page(['people.manage'])
    for (const label of BUTTONS) expect(has(html, label), label).toBe(true)
  })

  it('gives everyone else none, but the way into the key they control', () => {
    const reader = page([], { manage: false })
    for (const label of BUTTONS.filter((label) => label !== 'Open its space')) expect(has(reader.html, label), label).toBe(false)
    expect(reader.text).toContain('Emily Carter')
    expect(has(reader.html, 'Open its space')).toBe(true)
  })

  it('shows no form, no button and no role editor once a trial ended, and no way into a key', () => {
    const { html, text } = page(['people.manage'], { readOnly: true })
    for (const label of [...BUTTONS, 'Give the role']) expect(has(html, label), label).toBe(false)
    expect(text).toContain('Emily Carter') // what it holds can be read
    expect(text).toContain('Billing CI')
  })
})
