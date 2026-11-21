import { describe, expect, it } from 'vitest'
import type { Person } from '../types'
import { accountLabels, admins, byHand, lastAdmin, rolesOf, sortPeople } from './people'

const leads = { id: 'g_paymentsld', name: 'Payments leads' }
const latetAdmins = { id: 'g_latetadmns', name: 'Latet admins' }
const person = (id: string, name: string, more: Partial<Person> = {}): Person => ({
  id,
  name,
  email: `${id}@acme.com`,
  user_name: null,
  disabled: false,
  source: 'local',
  last_seen: null,
  linked: false,
  claimable: false,
  scim_managed: false,
  disabled_by: null,
  groups: [],
  roles: [],
  through: [],
  ...more,
})

describe('people', () => {
  it('says whether a company account is linked, waits for its first sign-in, and whether SCIM manages it', () => {
    expect(accountLabels(person('u_emily', 'Emily Carter', { linked: true, scim_managed: true }))).toEqual([
      'ee.people.account.linked',
      'ee.people.account.scim',
    ])
    expect(accountLabels(person('u_priya', 'Priya Shah', { claimable: true }))).toEqual(['ee.people.account.waiting'])
    // Linked already: no longer waiting, whatever else the server says.
    expect(accountLabels(person('u_laura', 'Laura Chen', { linked: true, claimable: true }))).toEqual(['ee.people.account.linked'])
    expect(accountLabels(person('u_dan', 'Daniel Okafor', { scim_managed: true, claimable: true }))).toEqual([
      'ee.people.account.waiting',
      'ee.people.account.scim',
    ])
    expect(accountLabels(person('u_tom', 'Tom Becker'))).toEqual([]) // added by hand, not linked: nothing to say
  })

  it('puts the enabled people first, then by name', () => {
    const list = [person('u_sam', 'Sam Rivera', { disabled: true }), person('u_tom', 'Tom Becker'), person('u_emily', 'Emily Carter')]
    expect(sortPeople(list).map((p) => p.name)).toEqual(['Emily Carter', 'Tom Becker', 'Sam Rivera'])
  })

  it('counts the roles given to a person and to their groups once each', () => {
    const through = [
      { role: 'group-lead' as const, group: leads },
      { role: 'security' as const, group: leads },
    ]
    expect(rolesOf(person('u_priya', 'Priya Shah', { roles: ['security'], through }))).toEqual(['group-lead', 'security'])
  })

  it('knows the last administrator, through a group too, the disabled ones aside', () => {
    const emily = person('u_emily', 'Emily Carter', { through: [{ role: 'admin', group: latetAdmins }] })
    const sam = person('u_sam', 'Sam Rivera', { roles: ['admin'], disabled: true })
    expect(admins([emily, sam])).toEqual(['u_emily'])
    expect(lastAdmin([emily, sam], 'u_emily')).toBe(true)
    expect(lastAdmin([emily, person('u_daniel', 'Daniel Okafor', { roles: ['admin'] })], 'u_emily')).toBe(false)
  })

  it('takes away here only the memberships made here', () => {
    expect([byHand('local'), byHand('scim'), byHand('oidc')]).toEqual([true, false, false])
  })
})
