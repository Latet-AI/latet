// Pieces of the rules forms shared by the global rules and a project's.

import { useId, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { DataMode, FieldRule, ImagesMode, Term } from '../api/types'
import type { DocsTarget } from '../lib/docsLinks'
import { useDeploymentKey } from '../lib/me'
import { detailReason, detailValues } from '../lib/refused'
import { hasLetterOrDigit, TERM_LONGEST, termTooLong } from '../lib/terms'
import { useFormat } from '../lib/useFormat'
import { DocsSectionLink, TitleWithDocs } from './DocsLinks'
import { Icon } from './Icon'
import { usePageSection } from './sections'
import { StringList } from './StringList'
import { InlineCodeText, Segmented, Switch } from './ui'

/** The sections of the rules, for links from elsewhere (`/rules#terms`): each opens the tab that holds it (`tabOf`). */
export const ANCHORS = {
  profile: 'profile',
  pii: 'personal-data',
  ner: 'names',
  phones: 'phones',
  privateIps: 'private-ips',
  terms: 'terms',
  recognizers: 'recognizers',
  allow: 'allow',
  policies: 'policies',
  reviewer: 'reviewer',
  live: 'live-review',
  data: 'data',
  identity: 'identity',
  history: 'history',
} as const

/**
 * The sections of a project's rules (`/rules?project=<id>#personal`), for links from elsewhere: each opens the tab that
 * holds it (`projectTabOf`). Those of your own settings are named as the global ones, after `project-`.
 */
export const PROJECT_ANCHORS = {
  protection: 'protection',
  effective: 'effective',
  personal: 'personal',
  /** Production data in your settings for a project: where an exchange of it leads when they decide strict data mode. */
  projectData: 'project-data',
  terms: 'project-terms',
  recognizers: 'project-recognizers',
  allow: 'project-allow',
  policies: 'project-policies',
  reviewer: 'project-reviewer',
  live: 'project-live-review',
  repo: 'repo',
} as const

export function Section({
  title,
  description,
  anchor,
  listed = true,
  hidden = false,
  wide = false,
  docs,
  children,
}: {
  title: string
  description?: ReactNode
  /** Where links to this section land (rules shown in an exchange link here). */
  anchor?: string
  /** Listed in the side menu under the page's entry (Rules has its tabs instead). */
  listed?: boolean
  /** Kept with what it holds, out of sight: in another tab, or not found by a search. */
  hidden?: boolean
  /** Its title above what it holds, which takes the whole width (a table). */
  wide?: boolean
  /** The section of the documentation that explains it: a "?" beside its title, named after it. */
  docs?: DocsTarget
  children: ReactNode
}) {
  const id = useId()
  usePageSection(listed ? anchor : undefined, title)
  return (
    // A Linea panel each, apart by a gap rather than by a line (margins collapse between two of them).
    <section
      id={anchor}
      hidden={hidden}
      aria-labelledby={id}
      className={`panel my-4 grid scroll-mt-24 gap-x-10 gap-y-3 p-5 first:mt-0 max-lg:grid-cols-1 max-lg:p-4 ${
        wide ? 'grid-cols-1' : 'grid-cols-[minmax(0,300px)_minmax(0,1fr)]'
      }`}
    >
      <div>
        <div className="flex items-start gap-1">
          <h2 id={id} className="text-title font-semibold text-ink">
            {title}
          </h2>
          {docs && <DocsSectionLink target={docs} section={title} />}
        </div>
        {description && <div className={`mt-1 text-ui text-ink-2 ${wide ? 'max-w-[80ch]' : ''}`}>{description}</div>}
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  )
}

/** The terms as the tutorial's terms chapter edits them, inline, saved with its own Save (Rules has them as a table). */
export function TermsEditor({ terms, onChange }: { terms: Term[]; onChange: (terms: Term[]) => void }) {
  const { t } = useTranslation()
  const format = useFormat()
  const id = useId()
  const [value, setValue] = useState('')
  const [type, setType] = useState('CUSTOM')
  const update = (i: number, patch: Partial<Term>) => onChange(terms.map((term, j) => (j === i ? { ...term, ...patch } : term)))
  // Signs alone (`--`, `###`) cannot be masked, nor can a term longer than the daemon keeps: it refuses either, and one in a
  // file edited by hand is not used.
  const problem = (text: string): string | null =>
    !hasLetterOrDigit(text)
      ? t('rules.terms.noLetterOrDigit')
      : termTooLong(text)
        ? t('rules.terms.tooLong', { max: format.number(TERM_LONGEST) })
        : null
  const wrong = terms.map((term) => problem(term.value))
  const refused = value.trim() !== '' ? problem(value.trim()) : null
  const canAdd = value.trim() !== '' && refused === null && !terms.some((term) => term.value === value.trim())
  return (
    <div className="space-y-3">
      {terms.length === 0 ? (
        <p className="text-meta text-ink-3">{t('rules.terms.empty')}</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('rules.terms.value')}</th>
                <th scope="col">{t('rules.terms.type')}</th>
                <th scope="col">{t('rules.terms.caseSensitive')}</th>
                <th scope="col">{t('rules.terms.wholeWord')}</th>
                <th scope="col">
                  <span className="sr-only">{t('rules.terms.actions')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {terms.map((term, i) => (
                <tr key={i}>
                  <td>
                    <input
                      className="field w-full min-w-[180px]"
                      value={term.value}
                      onChange={(e) => update(i, { value: e.target.value })}
                      aria-label={t('rules.terms.value')}
                      aria-invalid={wrong[i] ? true : undefined}
                      aria-describedby={wrong[i] ? `${id}-${i}` : undefined}
                    />
                    {wrong[i] && (
                      <p id={`${id}-${i}`} className="mt-1 max-w-[48ch] text-meta text-crit-ink">
                        <InlineCodeText text={wrong[i] ?? ''} />
                      </p>
                    )}
                  </td>
                  <td>
                    <input
                      className="field mono w-[140px]"
                      value={term.type}
                      onChange={(e) => update(i, { type: e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_') })}
                      aria-label={t('rules.terms.type')}
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={term.case_sensitive}
                      onChange={(e) => update(i, { case_sensitive: e.target.checked })}
                      aria-label={t('rules.terms.caseSensitiveFor', { value: term.value })}
                      className="mt-2 accent-[var(--ink)]"
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={term.whole_word}
                      onChange={(e) => update(i, { whole_word: e.target.checked })}
                      aria-label={t('rules.terms.wholeWordFor', { value: term.value })}
                      className="mt-2 accent-[var(--ink)]"
                    />
                  </td>
                  <td className="text-right">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm btn-icon"
                      onClick={() => onChange(terms.filter((_, j) => j !== i))}
                      aria-label={t('rules.remove', { value: term.value })}
                    >
                      <Icon name="trash" size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (!canAdd) return
          onChange([...terms, { value: value.trim(), type: type || 'CUSTOM', case_sensitive: false, whole_word: true }])
          setValue('')
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="label">{t('rules.terms.newValue')}</span>
          <input
            className="field w-[260px]"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={t('rules.terms.placeholder')}
            aria-invalid={refused !== null || undefined}
            aria-describedby={refused !== null ? `${id}-new` : undefined}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('rules.terms.type')}</span>
          <input
            className="field mono w-[140px]"
            value={type}
            onChange={(e) => setType(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_'))}
          />
        </label>
        <button type="submit" className="btn" disabled={!canAdd}>
          <Icon name="plus" size={14} />
          {t('rules.terms.add')}
        </button>
        {refused !== null && (
          <p id={`${id}-new`} className="basis-full text-meta text-crit-ink">
            <InlineCodeText text={refused} />
          </p>
        )}
      </form>
    </div>
  )
}

/** What the production data editor changes: in the global rules, the repository file or your own. */
export interface DataDraft {
  data: DataMode | null
  data_tools: string[]
  data_files: string[]
  data_hosts: string[]
  fields: FieldRule[]
  dump_guard: number | null
  images: ImagesMode | null
}

/** Where the editor saves: the repository file can only add protection; yours can also take it away. */
export type DataLayer = 'global' | 'repo' | 'personal'

type Inherit<V extends string> = V | 'unset'

/**
 * Production data: strict data mode (every value of a tool output's tables, JSON and key=value pairs
 * masked), the files, the web addresses and the tools it covers, field rules, the dump guard and images.
 */
export function DataEditor({
  value,
  set,
  layer,
}: {
  value: DataDraft
  set: <K extends keyof DataDraft>(key: K, value: DataDraft[K]) => void
  layer: DataLayer
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const repo = layer === 'repo'
  return (
    <div className="space-y-5">
      <div>
        <TitleWithDocs title={t('data.strict.title')} docs="section.strict" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('data.strict.help')}</p>
        {repo ? (
          <Switch
            checked={value.data === 'strict'}
            onChange={(on) => set('data', on ? 'strict' : null)}
            label={t('data.strict.switch')}
          />
        ) : (
          <>
            <Segmented<Inherit<DataMode>>
              label={t('data.strict.title')}
              value={value.data ?? 'unset'}
              onChange={(v) => set('data', v === 'unset' ? null : v)}
              options={[
                // Not set: your global rules' choice in a project; the profile's in the global rules.
                { value: 'unset', label: layer === 'global' ? t('data.strict.profile') : t('data.unset') },
                { value: 'strict', label: t('data.strict.on') },
                { value: 'normal', label: t('data.strict.off') },
              ]}
            />
            {layer === 'global' && <p className="mt-2 max-w-[80ch] text-meta text-ink-3">{t('data.strict.profileHelp')}</p>}
          </>
        )}
      </div>
      {/* What counts as data: adding a file or an address protects, so a repository file adds them too; choosing the
          tools narrows what strict data mode covers, which is yours alone. */}
      <div>
        <TitleWithDocs title={t('data.files.title')} docs="section.dataFiles" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('data.files.help')}</p>
        <StringList
          values={value.data_files}
          onChange={(files) => set('data_files', files)}
          placeholder={t('data.files.placeholder')}
          addLabel={t('data.files.add')}
          emptyLabel={t('data.files.empty')}
        />
      </div>
      <div>
        <TitleWithDocs title={t('data.hosts.title')} docs="section.dataHosts" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('data.hosts.help')}</p>
        <StringList
          values={value.data_hosts}
          onChange={(hosts) => set('data_hosts', hosts)}
          placeholder={t('data.hosts.placeholder')}
          addLabel={t('data.hosts.add')}
          emptyLabel={t('data.hosts.empty')}
        />
      </div>
      {!repo && (
        <div>
          <TitleWithDocs title={t('data.tools.title')} docs="section.dataTools" />
          <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('data.tools.help')}</p>
          <StringList
            values={value.data_tools}
            onChange={(tools) => set('data_tools', tools)}
            placeholder={t('data.tools.placeholder')}
            addLabel={t('data.tools.add')}
            emptyLabel={t('data.tools.empty')}
          />
        </div>
      )}
      <div>
        <TitleWithDocs title={t('data.fields.title')} docs="section.fields" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{repo ? t('data.fields.repoHelp') : t('data.fields.help')}</p>
        <FieldsEditor fields={value.fields} onChange={(fields) => set('fields', fields)} keepInClear={!repo} />
      </div>
      <div>
        <TitleWithDocs title={t('data.dumpGuard.title')} docs="section.dumpGuard" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">
          {repo ? t('data.dumpGuard.repoHelp') : layer === 'personal' ? t('data.dumpGuard.personalHelp') : t('data.dumpGuard.help')}
        </p>
        <label className="flex items-center gap-2 text-ui text-ink-2">
          <input
            type="number"
            min={0}
            step={1}
            className="field mono w-[170px]"
            value={value.dump_guard ?? ''}
            placeholder={layer === 'personal' ? t('data.unset') : t('data.dumpGuard.off')}
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10)
              set('dump_guard', e.target.value === '' || Number.isNaN(n) ? null : Math.max(0, n))
            }}
            aria-label={t('data.dumpGuard.title')}
          />
          {t('data.dumpGuard.unit')}
        </label>
      </div>
      <div>
        <TitleWithDocs title={t('data.images.title')} docs="section.images" />
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t(key('data.images.help'))}</p>
        {layer === 'global' ? (
          <Segmented<ImagesMode>
            label={t('data.images.title')}
            value={value.images ?? 'allow'}
            onChange={(v) => set('images', v)}
            options={[
              { value: 'allow', label: t('data.images.allow') },
              { value: 'mask', label: t('data.images.mask') },
              { value: 'block', label: t('data.images.block') },
            ]}
          />
        ) : (
          <Segmented<Inherit<ImagesMode>>
            label={t('data.images.title')}
            value={value.images ?? 'unset'}
            onChange={(v) => set('images', v === 'unset' ? null : v)}
            options={[
              { value: 'unset', label: t('data.unset') },
              ...(repo ? [] : [{ value: 'allow' as const, label: t('data.images.allow') }]),
              { value: 'mask', label: t('data.images.mask') },
              { value: 'block', label: t('data.images.block') },
            ]}
          />
        )}
      </div>
    </div>
  )
}

/** Field rules: a field's values masked with a type of their own, or (your settings) kept in clear. */
export function FieldsEditor({
  fields,
  onChange,
  keepInClear,
}: {
  fields: FieldRule[]
  onChange: (fields: FieldRule[]) => void
  /** Offer "keep in clear" (not in a repository file: it cannot take protection away). */
  keepInClear: boolean
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [type, setType] = useState('')
  const update = (i: number, patch: Partial<FieldRule>) => onChange(fields.map((f, j) => (j === i ? { ...f, ...patch } : f)))
  const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase() // as the list tells its own rules apart
  const canAdd = name.trim() !== '' && !fields.some((f) => same(f.name, name.trim()))
  const typeOf = (value: string) => value.toUpperCase().replace(/[^A-Z0-9_]/g, '_')
  return (
    <div className="space-y-3">
      {fields.length === 0 ? (
        <p className="text-meta text-ink-3">{t('data.fields.empty')}</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('data.fields.name')}</th>
                <th scope="col">{t('data.fields.type')}</th>
                {keepInClear && <th scope="col">{t('data.fields.clear')}</th>}
                <th scope="col">{t('data.fields.secret')}</th>
                <th scope="col">
                  <span className="sr-only">{t('rules.terms.actions')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {fields.map((f, i) => {
                const clear = f.mask === false
                return (
                  <tr key={i}>
                    <td>
                      <input
                        className="field mono w-full min-w-[160px]"
                        value={f.name}
                        onChange={(e) => update(i, { name: e.target.value })}
                        aria-label={t('data.fields.name')}
                      />
                    </td>
                    <td>
                      <input
                        className="field mono w-[130px]"
                        value={f.type ?? ''}
                        disabled={clear}
                        placeholder={clear ? '' : t('data.fields.typePlaceholder')}
                        onChange={(e) => update(i, { type: typeOf(e.target.value) || null })}
                        aria-label={t('data.fields.type')}
                      />
                    </td>
                    {keepInClear && (
                      <td>
                        <input
                          type="checkbox"
                          checked={clear}
                          onChange={(e) => update(i, e.target.checked ? { mask: false, type: null, secret: false } : { mask: true })}
                          aria-label={t('data.fields.clearFor', { name: f.name })}
                          className="mt-2 accent-[var(--ink)]"
                        />
                      </td>
                    )}
                    <td>
                      <input
                        type="checkbox"
                        checked={f.secret === true}
                        disabled={clear}
                        onChange={(e) => update(i, { secret: e.target.checked })}
                        aria-label={t('data.fields.secretFor', { name: f.name })}
                        className="mt-2 accent-[var(--ink)]"
                      />
                    </td>
                    <td className="text-right">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm btn-icon"
                        onClick={() => onChange(fields.filter((_, j) => j !== i))}
                        aria-label={t('rules.remove', { value: f.name })}
                      >
                        <Icon name="trash" size={13} />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (!canAdd) return
          onChange([...fields, { name: name.trim(), ...(type ? { type } : {}) }])
          setName('')
          setType('')
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="label">{t('data.fields.newName')}</span>
          <input className="field mono w-[220px]" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('data.fields.placeholder')} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('data.fields.type')}</span>
          <input
            className="field mono w-[140px]"
            value={type}
            onChange={(e) => setType(typeOf(e.target.value))}
            placeholder={t('data.fields.typePlaceholder')}
          />
        </label>
        <button type="submit" className="btn" disabled={!canAdd}>
          <Icon name="plus" size={14} />
          {t('data.fields.add')}
        </button>
      </form>
    </div>
  )
}

export function validatePattern(t: (key: string, options?: Record<string, unknown>) => string, pattern: string): string | null {
  try {
    new RegExp(pattern)
  } catch (error) {
    return t('rules.allow.invalidPattern', { message: error instanceof Error ? error.message : String(error) })
  }
  if (/\(\?<?[=!]|\\[1-9]/.test(pattern)) return t('rules.allow.re2')
  return null
}

export function detailText(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const { loc, msg } = detail as { loc?: unknown; msg?: unknown }
    const where = Array.isArray(loc) ? loc.join('.') : ''
    if (typeof msg === 'string') return where ? `${where}: ${msg}` : msg
  }
  return JSON.stringify(detail)
}

/** Why a save was refused, field by field: in your language where the daemon gives a reason, else in its words. */
export function RefusedDetails({ details }: { details: unknown[] }) {
  const { t } = useTranslation()
  return (
    <ul className="mt-1 list-disc pl-5 text-meta">
      {details.map((detail, i) => {
        const reason = detailReason(detail)
        return reason !== null ? (
          <li key={i}>{t(reason, detailValues(detail))}</li>
        ) : (
          <li key={i} className="mono">
            {detailText(detail)}
          </li>
        )
      })}
    </ul>
  )
}
