import { useTranslation } from 'react-i18next'
import { currentSpace, switchSpace } from '../../api/client'
import type { Me, ServerIdentity } from '../../api/types'
import { Icon } from '../../components/Icon'
import { useMe } from '../../lib/me'
import { useAsync } from '../../lib/useAsync'
import { peopleApi } from '../api'
import type { ApiKey } from '../types'

/** An API key whose space can be opened, as the menu lists it. */
export interface SpaceOption {
  id: string
  name: string
}

/**
 * Whether the menu offers an API key's space at all: on a server whose license has roles, and not after a trial ended,
 * which closes every key's space (R3).
 */
export function spaceOffered(me: Me | undefined): boolean {
  return Boolean(me?.server) && (me?.edition?.features ?? []).includes('roles') && me?.edition?.license?.state !== 'trial-ended'
}

/**
 * The keys the menu lists: those of the person's own list that are not disabled, and the key whose space is open
 * whatever the list says (disabled meanwhile, or not listed yet): the way back is always there.
 */
export function spaceOptions(keys: ApiKey[], open: string | null, identity: ServerIdentity | null | undefined): SpaceOption[] {
  const options = keys.filter((key) => !key.disabled || key.id === open).map((key) => ({ id: key.id, name: key.name }))
  if (open !== null && !options.some((option) => option.id === open)) {
    options.push({ id: open, name: identity?.kind === 'key' ? identity.name : open })
  }
  return options
}

/**
 * In the sidebar, on a server whose license has roles: your own space, or the space of an API key you control (an
 * administrator's: any key's). Nothing when there is no key to open. The list of keys is always the person's own
 * (`/ee/keys` is never sent through a key's space), so the way back is there in a key's space too.
 *
 * Below 1024 px the sidebar is a rail of icons, too narrow for the labelled menu: there the same choice is a key icon
 * over a select of its own, which shows that a key's space is open. A person who controls a key without administering
 * has no other way into its space.
 */
export function SpaceMenu() {
  const { t } = useTranslation()
  const { me } = useMe()
  const enterprise = spaceOffered(me)
  const keys = useAsync(enterprise ? 'ee-space-keys' : null, (signal) => peopleApi.keys(signal))
  const open = currentSpace()
  const options = spaceOptions(keys.data?.keys ?? [], open, me?.server?.identity)
  if (!enterprise && open === null) return null
  if (options.length === 0) return null
  const choose = (event: { target: { value: string } }) => switchSpace(event.target.value || null)
  const choices = (
    <>
      <option value="">{t('ee.space.yours')}</option>
      {options.map((option) => (
        <option key={option.id} value={option.id}>
          {t('ee.space.key', { name: option.name })}
        </option>
      ))}
    </>
  )
  return (
    <>
      <label className="block max-lg:hidden">
        <span className="label">{t('ee.space.label')}</span>
        <select className="field w-full" value={open ?? ''} onChange={choose}>
          {choices}
        </select>
      </label>
      <div className="relative lg:hidden" data-space-open={open !== null}>
        <select
          className="peer absolute inset-0 size-full cursor-pointer opacity-0"
          aria-label={t('ee.space.label')}
          title={t('ee.space.label')}
          value={open ?? ''}
          onChange={choose}
        >
          {choices}
        </select>
        <span
          aria-hidden="true"
          className={`flex h-8 items-center justify-center gap-0.5 rounded-md border peer-focus-visible:outline-2 peer-focus-visible:outline-[var(--focus)] ${
            open !== null ? 'border-brand/60 bg-brand/10 text-ink' : 'border-line text-ink-2'
          }`}
        >
          <Icon name="key" />
          <Icon name="chevronDown" size={10} />
        </span>
      </div>
    </>
  )
}

/**
 * Above every page while an API key's space is open: whose space it is, and the way back to yours. Whether a space is
 * open is the tab's own (`currentSpace`), never what `/me` says through the space: the way out is there whatever that
 * answer is.
 */
export function SpaceBar() {
  const { t } = useTranslation()
  const { me } = useMe()
  if (currentSpace() === null) return null
  const identity = me?.server?.identity
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-brand/60 bg-brand/10 px-4 py-2.5 text-ui text-ink"
    >
      <p className="min-w-0 max-w-[80ch]">
        {identity?.kind === 'key' ? t('ee.space.bar', { name: identity.name }) : t('ee.space.barUnnamed')}
      </p>
      <button type="button" className="btn btn-sm" onClick={() => switchSpace(null)}>
        {t('ee.space.back')}
      </button>
    </div>
  )
}
