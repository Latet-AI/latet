import { tk } from '../../i18n'
import type { GrantedRole, GroupSource, Person, Role } from '../types'

export const GRANTED_ROLES: GrantedRole[] = ['group-lead', 'security', 'auditor', 'admin']

export const ROLE_LABEL: Record<Role, string> = {
  user: tk('ee.roles.user'),
  'group-lead': tk('ee.roles.groupLead'),
  security: tk('ee.roles.security'),
  auditor: tk('ee.roles.auditor'),
  admin: tk('ee.roles.admin'),
}

const ACCOUNT = {
  linked: tk('ee.people.account.linked'),
  waiting: tk('ee.people.account.waiting'),
  scim: tk('ee.people.account.scim'),
}

/**
 * What the page says of a person's company account, as the server gives it: linked to it (their sign-in finds them by
 * its subject), or waiting for their first sign-in (an administrator or SCIM linked it by user name, nobody signed in
 * to it yet); and whether SCIM manages them. Nothing for someone neither linked nor waiting: an administrator links them
 * (`latet server people link`), or they link their own account.
 */
export function accountLabels(person: Pick<Person, 'linked' | 'claimable' | 'scim_managed'>): string[] {
  const labels: string[] = []
  if (person.linked) labels.push(ACCOUNT.linked)
  else if (person.claimable) labels.push(ACCOUNT.waiting)
  if (person.scim_managed) labels.push(ACCOUNT.scim)
  return labels
}

/** Enabled people first, then by name. */
export function sortPeople(people: Person[]): Person[] {
  return [...people].sort((a, b) => Number(a.disabled) - Number(b.disabled) || a.name.localeCompare(b.name))
}

/** Every role a person has, given to them or to one of their groups, each once, in the table's order. */
export function rolesOf(person: Person): GrantedRole[] {
  return GRANTED_ROLES.filter((role) => person.roles.includes(role) || person.through.some((given) => given.role === role))
}

/** The enabled people who are administrators, themselves or through a group. */
export function admins(people: Person[]): string[] {
  return people.filter((p) => !p.disabled && rolesOf(p).includes('admin')).map((p) => p.id)
}

/** Whether `personId` is the server's only enabled administrator: the page keeps them so. */
export function lastAdmin(people: Person[], personId: string): boolean {
  const found = admins(people)
  return found.length === 1 && found[0] === personId
}

/** Whether a membership is taken away here: those made here; SCIM's and sign-in's change at the provider. */
export function byHand(source: GroupSource): boolean {
  return source === 'local'
}
