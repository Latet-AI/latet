import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { switchSpace } from '../../api/client'
import { Icon } from '../../components/Icon'
import { Section } from '../../components/rules'
import { ConfirmDialog, ErrorState, Loading, PageHeader } from '../../components/ui'
import { tk } from '../../i18n'
import { useMe } from '../../lib/me'
import { useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { peopleApi } from '../api'
import { CompanySignInWay } from '../components/CompanyWay'
import { connectShown, usePanels, type Shown } from '../components/Shown'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { useRun, type Run } from '../components/useRun'
import { accountLabels, byHand, GRANTED_ROLES, lastAdmin, ROLE_LABEL, rolesOf, sortPeople } from '../lib/people'
import type { ApiKey, DisabledBy, GrantedRole, Group, GroupSource, Person, Role } from '../types'

const SOURCE: Record<Person['source'], string> = {
  local: tk('ee.people.source.local'),
  scim: tk('ee.people.source.scim'),
  oidc: tk('ee.people.source.oidc'),
}

const DISABLED_BY: Record<DisabledBy, string> = {
  local: tk('ee.people.disabledBy.local'),
  scim: tk('ee.people.disabledBy.scim'),
}

const FROM: Record<GroupSource, string> = {
  local: tk('ee.groups.from.local'),
  scim: tk('ee.groups.from.scim'),
  oidc: tk('ee.groups.from.oidc'),
}

// The design's role table: what each role sees, and what it decides.
const TABLE: { role: Role; sees: string; decides: string }[] = [
  { role: 'user', sees: tk('ee.roles.sees.user'), decides: tk('ee.roles.decides.user') },
  { role: 'group-lead', sees: tk('ee.roles.sees.groupLead'), decides: tk('ee.roles.decides.nothing') },
  { role: 'security', sees: tk('ee.roles.sees.security'), decides: tk('ee.roles.decides.security') },
  { role: 'auditor', sees: tk('ee.roles.sees.auditor'), decides: tk('ee.roles.decides.nothing') },
  { role: 'admin', sees: tk('ee.roles.sees.admin'), decides: tk('ee.roles.decides.admin') },
]

interface Busy {
  busy: boolean
  run: Run
}

/** What a change that can take the Admin role away says of the SCIM tokens it left serving (not ended by a demotion). */
interface Later {
  later: (tokens?: string[]) => void
}

export function PeoplePage() {
  const { t } = useTranslation()
  const fmt = useFormat()
  const { me } = useMe()
  const readOnly = useEnterpriseReadOnly() // a trial that ended: read, not changed (R16)
  const people = useAsync('ee-people', (signal) => peopleApi.people(signal))
  const groups = useAsync('ee-groups', (signal) => peopleApi.groups(signal))
  const keys = useAsync('ee-keys', (signal) => peopleApi.keys(signal))
  const reload = () => {
    people.reload()
    groups.reload()
    keys.reload()
  }
  const [busy, run, company] = useRun(reload)
  const { show, note, panels } = usePanels() // a code or a key shown stays until its own Close: the SCIM tokens' note has its own place
  const edits = !readOnly && (me?.edition?.actions ?? []).includes('people.manage')
  const later = (tokens?: string[]) => {
    if (tokens && tokens.length > 0) note(t('ee.people.tokensStill', { count: tokens.length, ids: fmt.list(tokens) }))
  }
  const header = <PageHeader title={t('ee.people.title')} description={t('ee.people.description')} />
  if (people.loading || groups.loading || keys.loading) return <>{header}<Loading /></>
  if (!people.data || !groups.data || !keys.data) {
    return <>{header}<ErrorState error={people.error ?? groups.error ?? keys.error} onRetry={reload} /></>
  }
  const list = sortPeople(people.data.people)
  return (
    <div className={people.stale || groups.stale || keys.stale ? 'opacity-60' : undefined}>
      {header}
      {company && <CompanySignInWay />}
      {panels}
      <PeopleSection people={list} enabled={people.data.enabled} edits={edits} busy={busy} run={run} later={later} show={show} />
      <GroupsSection groups={groups.data.groups} people={list} edits={edits} busy={busy} run={run} later={later} />
      <KeysSection
        keys={keys.data.keys}
        manage={keys.data.manage && !readOnly}
        people={list}
        groups={groups.data.groups}
        busy={busy}
        run={run}
        show={show}
      />
      <RoleTable />
    </div>
  )
}

interface Tag {
  key: string
  text: string
  /** Takes it away here; none when it changes elsewhere. */
  remove?: () => void
  removeLabel: string
}

/** A labelled row of tags, each with its × when it can be taken away here. */
function Tags({ label, empty, items, busy }: { label: string; empty: string; items: Tag[]; busy: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-ui">
      <span className="text-meta text-ink-3">{label}</span>
      {items.length === 0 && <span className="text-meta text-ink-3">{empty}</span>}
      {items.map((item) => (
        <span key={item.key} className="tag flex items-center gap-1">
          {item.text}
          {item.remove && (
            <button type="button" className="btn btn-ghost btn-sm btn-icon" aria-label={item.removeLabel} disabled={busy} onClick={item.remove}>
              <Icon name="x" size={12} />
            </button>
          )}
        </span>
      ))}
    </div>
  )
}

function PeopleSection({
  people,
  enabled,
  edits,
  busy,
  run,
  later,
  show,
}: Busy & Later & { people: Person[]; enabled: number; edits: boolean; show: (shown: Shown) => void }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const [open, setOpen] = useState<string | null>(null)
  const [disabling, setDisabling] = useState<Person | null>(null)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const connect = (made: { command: string; code: string }, who: string) =>
    show(connectShown(t('ee.people.codeFor', { name: who }), made, { command: t('ee.shown.command'), code: t('ee.shown.code') }))
  const add = () =>
    run(async () => {
      const made = await peopleApi.addPerson(name.trim(), email.trim() || null)
      connect(made, made.person.name)
      setName('')
      setEmail('')
    }, t('ee.people.added'))
  const code = (person: Person) =>
    run(async () => {
      connect(await peopleApi.code(person.id), person.name)
    })
  return (
    <Section title={t('ee.people.list.title')} description={t('ee.people.list.description', { count: enabled })}>
      {edits && (
        <form
          className="mb-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void add()
          }}
        >
          <input className="field" value={name} maxLength={200} aria-label={t('ee.people.name')} placeholder={t('ee.people.namePlaceholder')} onChange={(event) => setName(event.target.value)} />
          <input className="field" type="email" value={email} aria-label={t('ee.people.email')} placeholder={t('ee.people.emailPlaceholder')} onChange={(event) => setEmail(event.target.value)} />
          <button type="submit" className="btn" disabled={busy || !name.trim()}>
            {t('ee.people.add')}
          </button>
        </form>
      )}
      <div className="panel overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">{t('ee.people.columns.person')}</th>
              <th scope="col">{t('ee.people.columns.groups')}</th>
              <th scope="col">{t('ee.people.columns.roles')}</th>
              <th scope="col">{t('ee.people.columns.lastSeen')}</th>
              <th scope="col">
                <span className="sr-only">{t('ee.people.columns.actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {people.map((person) => (
              <Fragment key={person.id}>
                <tr className={person.disabled ? 'text-ink-3' : undefined}>
                  <td>
                    <p className="font-medium text-ink">
                      {person.name}
                      {person.disabled && <span className="tag ml-2">{t('ee.people.disabled')}</span>}
                    </p>
                    <p className="text-meta text-ink-3">{person.email ?? person.user_name ?? person.id}</p>
                    <p className="text-meta text-ink-3">{t(SOURCE[person.source])}</p>
                    {accountLabels(person).length > 0 && (
                      <p className="mt-0.5 flex flex-wrap gap-1">
                        {accountLabels(person).map((label) => (
                          <span key={label} className="tag">
                            {t(label)}
                          </span>
                        ))}
                      </p>
                    )}
                    {person.disabled && person.disabled_by && <p className="text-meta text-ink-3">{t(DISABLED_BY[person.disabled_by])}</p>}
                  </td>
                  <td>{person.groups.map((group) => group.name).join(', ') || t('common.none')}</td>
                  <td>
                    <span className="flex flex-wrap gap-1">
                      {rolesOf(person).map((role) => (
                        <span key={role} className="tag">
                          {t(ROLE_LABEL[role])}
                        </span>
                      ))}
                      {rolesOf(person).length === 0 && t('common.none')}
                    </span>
                  </td>
                  <td>{person.last_seen ? fmt.relative(person.last_seen) : t('ee.people.never')}</td>
                  <td className="text-right">
                    {edits && (
                      <span className="flex justify-end gap-1">
                        <button type="button" className="btn btn-sm" aria-expanded={open === person.id} onClick={() => setOpen(open === person.id ? null : person.id)}>
                          {t('ee.people.roles')}
                        </button>
                        {!person.disabled && (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy || person.last_seen !== null}
                            title={person.last_seen !== null ? t('ee.people.codeConnected') : undefined}
                            onClick={() => void code(person)}
                          >
                            {t('ee.people.code')}
                          </button>
                        )}
                        {person.disabled ? (
                          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void run(() => peopleApi.enable(person.id), t('ee.people.enabledDone', { name: person.name }))}>
                            {t('ee.people.enable')}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy || lastAdmin(people, person.id)}
                            title={lastAdmin(people, person.id) ? t('ee.people.lastAdmin') : undefined}
                            onClick={() => setDisabling(person)}
                          >
                            {t('ee.people.disable')}
                          </button>
                        )}
                      </span>
                    )}
                  </td>
                </tr>
                {edits && open === person.id && (
                  <tr>
                    <td colSpan={5}>
                      <RolesEditor person={person} busy={busy} run={run} later={later} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <ConfirmDialog
        open={disabling !== null}
        title={t('ee.people.disableTitle', { name: disabling?.name ?? '' })}
        confirmLabel={t('ee.people.disable')}
        busy={busy}
        onCancel={() => setDisabling(null)}
        onConfirm={() => {
          const target = disabling
          if (target) {
            void run(async () => later((await peopleApi.disable(target.id)).scim_tokens), t('ee.people.disabledDone', { name: target.name })).then(() => setDisabling(null))
          }
        }}
      >
        {t('ee.people.disableBody')}
      </ConfirmDialog>
    </Section>
  )
}

/** A person's roles: those given to them, taken back here; those of their groups, named; a role to give. */
function RolesEditor({ person, busy, run, later }: Busy & Later & { person: Person }) {
  const { t } = useTranslation()
  const [role, setRole] = useState<GrantedRole>('security')
  const given: Tag[] = person.roles.map((granted) => ({
    key: granted,
    text: t(ROLE_LABEL[granted]),
    remove: () => void run(async () => later((await peopleApi.revoke(person.id, granted)).scim_tokens), t('ee.people.revoked')),
    removeLabel: t('ee.people.revoke', { role: t(ROLE_LABEL[granted]) }),
  }))
  const through: Tag[] = person.through.map((granted) => ({
    key: `${granted.role}:${granted.group.id}`,
    text: t('ee.people.through', { role: t(ROLE_LABEL[granted.role]), group: granted.group.name }),
    removeLabel: '',
  }))
  return (
    <div className="space-y-3 bg-well px-4 py-3">
      <Tags label={t('ee.people.roles')} empty={t('ee.groups.noRole')} items={[...given, ...through]} busy={busy} />
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1">
          <span className="label">{t('ee.people.role')}</span>
          <select className="field" value={role} onChange={(event) => setRole(event.target.value as GrantedRole)}>
            {GRANTED_ROLES.map((option) => (
              <option key={option} value={option}>
                {t(ROLE_LABEL[option])}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn" disabled={busy || person.roles.includes(role)} onClick={() => void run(() => peopleApi.grant(person.id, role), t('ee.people.granted'))}>
          {t('ee.people.grant')}
        </button>
      </div>
    </div>
  )
}

function GroupsSection({ groups, people, edits, busy, run, later }: Busy & Later & { groups: Group[]; people: Person[]; edits: boolean }) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [removing, setRemoving] = useState<Group | null>(null)
  return (
    <Section title={t('ee.groups.title')} description={t('ee.groups.description')}>
      {edits && (
        <form
          className="mb-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void run(() => peopleApi.addGroup(name.trim()), t('ee.groups.added')).then((ok) => ok && setName(''))
          }}
        >
          <input className="field" value={name} maxLength={80} aria-label={t('ee.groups.name')} placeholder={t('ee.groups.namePlaceholder')} onChange={(event) => setName(event.target.value)} />
          <button type="submit" className="btn" disabled={busy || !name.trim()}>
            {t('ee.groups.add')}
          </button>
        </form>
      )}
      {groups.length === 0 ? (
        <p className="text-ui text-ink-3">{t('ee.groups.none')}</p>
      ) : (
        <ul className="panel divide-y divide-line">
          {groups.map((group) => (
            <li key={group.id} className="space-y-2 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <p className="font-medium text-ink">
                  {group.name}
                  <span className="tag ml-2">{t(FROM[group.source])}</span>
                </p>
                {edits && group.source !== 'scim' && (
                  <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setRemoving(group)}>
                    {t('ee.groups.remove')}
                  </button>
                )}
              </div>
              <Tags
                label={t('ee.groups.roles')}
                empty={t('ee.groups.noRole')}
                busy={busy}
                items={group.roles.map((role) => ({
                  key: role,
                  text: t(ROLE_LABEL[role]),
                  remove: edits ? () => void run(async () => later((await peopleApi.revoke(group.id, role)).scim_tokens), t('ee.people.revoked')) : undefined,
                  removeLabel: t('ee.people.revoke', { role: t(ROLE_LABEL[role]) }),
                }))}
              />
              {group.source === 'scim' && group.roles.length > 0 && <p className="text-meta text-warn-ink">{t('ee.groups.scimRole')}</p>}
              <Tags
                label={t('ee.groups.members')}
                empty={t('ee.groups.nobody')}
                busy={busy}
                items={group.members.map((member) => ({
                  key: `${member.id}:${member.source}`,
                  text: byHand(member.source) ? member.name : t('ee.groups.fromProvider', { name: member.name }),
                  remove: edits && byHand(member.source) ? () => void run(async () => later((await peopleApi.leave(group.id, member.id)).scim_tokens), t('ee.groups.left')) : undefined,
                  removeLabel: t('ee.groups.leave', { name: member.name }),
                }))}
              />
              {edits && <GroupActions group={group} people={people} busy={busy} run={run} />}
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={removing !== null}
        title={t('ee.groups.removeTitle', { name: removing?.name ?? '' })}
        confirmLabel={t('ee.groups.remove')}
        busy={busy}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const target = removing
          if (target) void run(async () => later((await peopleApi.removeGroup(target.id)).scim_tokens), t('ee.groups.removed')).then(() => setRemoving(null))
        }}
      >
        {t('ee.groups.removeBody')}
      </ConfirmDialog>
    </Section>
  )
}

/** A role to give the group, and someone to put in it. */
function GroupActions({ group, people, busy, run }: Busy & { group: Group; people: Person[] }) {
  const { t } = useTranslation()
  const [role, setRole] = useState<GrantedRole>('group-lead')
  const [person, setPerson] = useState('')
  const candidates = people.filter((p) => !p.disabled && !group.members.some((member) => member.id === p.id && byHand(member.source)))
  return (
    <div className="flex flex-wrap gap-2">
      <select className="field" value={role} aria-label={t('ee.people.role')} onChange={(event) => setRole(event.target.value as GrantedRole)}>
        {GRANTED_ROLES.map((option) => (
          <option key={option} value={option}>
            {t(ROLE_LABEL[option])}
          </option>
        ))}
      </select>
      <button type="button" className="btn btn-sm" disabled={busy || group.roles.includes(role)} onClick={() => void run(() => peopleApi.grant(group.id, role), t('ee.people.granted'))}>
        {t('ee.groups.grant')}
      </button>
      {candidates.length > 0 && (
        <>
          <select className="field" value={person} aria-label={t('ee.groups.pickPerson')} onChange={(event) => setPerson(event.target.value)}>
            <option value="">{t('ee.groups.pickPerson')}</option>
            {candidates.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-sm" disabled={busy || !person} onClick={() => void run(() => peopleApi.join(group.id, person), t('ee.groups.joined')).then((ok) => ok && setPerson(''))}>
            {t('ee.groups.join')}
          </button>
        </>
      )}
    </div>
  )
}

function KeysSection({
  keys,
  manage,
  people,
  groups,
  busy,
  run,
  show,
}: Busy & { keys: ApiKey[]; manage: boolean; people: Person[]; groups: Group[]; show: (shown: Shown) => void }) {
  const { t } = useTranslation()
  const readOnly = useEnterpriseReadOnly() // a trial that ended closes a key's space (R3)
  const fmt = useFormat()
  const [name, setName] = useState('')
  const [rotating, setRotating] = useState<ApiKey | null>(null)
  const secretOf = (title: string, made: { secret: string; address: string | null }) =>
    show({
      title,
      lines: [
        { label: t('ee.shown.key'), text: made.secret },
        // A token that serves no agents has no address (one added with `latet server keys add-token`).
        ...(made.address !== null ? [{ label: t('ee.shown.address'), text: made.address }] : []),
      ],
    })
  const make = () =>
    run(async () => {
      const made = await peopleApi.makeKey(name.trim())
      secretOf(t('ee.keys.made', { name: made.key.name }), made)
      setName('')
    })
  const rotate = (key: ApiKey) =>
    run(async () => {
      secretOf(t('ee.keys.rotated', { name: key.name }), await peopleApi.rotateKey(key.id))
    }).then(() => setRotating(null))
  return (
    <Section title={t('ee.keys.title')} description={t('ee.keys.description')}>
      {manage && (
        <form
          className="mb-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void make()
          }}
        >
          <input className="field" value={name} maxLength={200} aria-label={t('ee.keys.name')} placeholder={t('ee.keys.namePlaceholder')} onChange={(event) => setName(event.target.value)} />
          <button type="submit" className="btn" disabled={busy || !name.trim()}>
            {t('ee.keys.make')}
          </button>
        </form>
      )}
      {keys.length === 0 ? (
        <p className="text-ui text-ink-3">{t('ee.keys.none')}</p>
      ) : (
        <ul className="panel divide-y divide-line">
          {keys.map((key) => (
            <li key={key.id} className="space-y-2 px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="font-medium text-ink">
                    {key.name}
                    {key.disabled && <span className="tag ml-2">{t('ee.people.disabled')}</span>}
                  </p>
                  <p className="text-meta text-ink-3">{key.last_seen ? t('ee.keys.lastUsed', { when: fmt.relative(key.last_seen) }) : t('ee.keys.neverUsed')}</p>
                </div>
                <span className="flex flex-wrap gap-1">
                  {!key.disabled && !readOnly && (
                    <button type="button" className="btn btn-sm" onClick={() => switchSpace(key.id)}>
                      {t('ee.keys.open')}
                    </button>
                  )}
                  {manage && (
                    <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setRotating(key)}>
                      {t('ee.keys.rotate')}
                    </button>
                  )}
                  {manage &&
                    (key.disabled ? (
                      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void run(() => peopleApi.enableKey(key.id), t('ee.keys.enabledDone'))}>
                        {t('ee.keys.enable')}
                      </button>
                    ) : (
                      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void run(() => peopleApi.disableKey(key.id), t('ee.keys.disabledDone'))}>
                        {t('ee.keys.disable')}
                      </button>
                    ))}
                </span>
              </div>
              {manage && (
                <Tags
                  label={t('ee.keys.controllers')}
                  empty={t('ee.keys.administratorsOnly')}
                  busy={busy}
                  items={key.controllers.map((controller) => ({
                    key: controller.id,
                    text: controller.kind === 'group' ? t('ee.keys.groupOption', { name: controller.name }) : controller.name,
                    remove: () => void run(() => peopleApi.release(key.id, controller.id), t('ee.keys.released')),
                    removeLabel: t('ee.keys.release', { name: controller.name }),
                  }))}
                />
              )}
              {manage && <GiveControl apiKey={key} people={people} groups={groups} busy={busy} run={run} />}
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={rotating !== null}
        title={t('ee.keys.rotateTitle', { name: rotating?.name ?? '' })}
        confirmLabel={t('ee.keys.rotate')}
        busy={busy}
        destructive={false}
        onCancel={() => setRotating(null)}
        onConfirm={() => {
          if (rotating) void rotate(rotating)
        }}
      >
        {t('ee.keys.rotateBody')}
      </ConfirmDialog>
    </Section>
  )
}

/** A group or a person to give the control of the key to. */
function GiveControl({ apiKey, people, groups, busy, run }: Busy & { apiKey: ApiKey; people: Person[]; groups: Group[] }) {
  const { t } = useTranslation()
  const [subject, setSubject] = useState('')
  const taken = new Set(apiKey.controllers.map((controller) => controller.id))
  const options = [
    ...groups.filter((group) => !taken.has(group.id)).map((group) => ({ id: group.id, text: t('ee.keys.groupOption', { name: group.name }) })),
    ...people.filter((p) => !p.disabled && !taken.has(p.id)).map((p) => ({ id: p.id, text: p.name })),
  ]
  if (options.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2">
      <select className="field" value={subject} aria-label={t('ee.keys.pickController')} onChange={(event) => setSubject(event.target.value)}>
        <option value="">{t('ee.keys.pickController')}</option>
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.text}
          </option>
        ))}
      </select>
      <button type="button" className="btn btn-sm" disabled={busy || !subject} onClick={() => void run(() => peopleApi.control(apiKey.id, subject), t('ee.keys.controlled')).then((ok) => ok && setSubject(''))}>
        {t('ee.keys.control')}
      </button>
    </div>
  )
}

function RoleTable() {
  const { t } = useTranslation()
  return (
    <Section title={t('ee.people.table.title')} description={t('ee.people.table.description')}>
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">{t('ee.people.table.role')}</th>
            <th scope="col">{t('ee.people.table.sees')}</th>
            <th scope="col">{t('ee.people.table.decides')}</th>
          </tr>
        </thead>
        <tbody>
          {TABLE.map((row) => (
            <tr key={row.role}>
              <td className="font-medium text-ink">{t(ROLE_LABEL[row.role])}</td>
              <td>{t(row.sees)}</td>
              <td>{t(row.decides)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}
