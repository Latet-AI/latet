import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Icon } from './Icon'

/** A short list of strings with an add field (phone regions, data files and hosts, keywords): a tag each. The long
 * lists of the rules are tables (RuleTable). */
export function StringList({
  values,
  onChange,
  placeholder,
  addLabel,
  mono = true,
  transform = (v: string) => v,
  validate,
  emptyLabel,
}: {
  values: string[]
  onChange: (values: string[]) => void
  placeholder: string
  addLabel: string
  mono?: boolean
  transform?: (value: string) => string
  validate?: (value: string) => string | null
  emptyLabel: string
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState('')
  const value = transform(draft.trim())
  const problem = value && validate ? validate(value) : null
  const add = () => {
    if (!value || values.includes(value) || problem !== null) return
    onChange([...values, value])
    setDraft('')
  }
  const remove = (v: string) => onChange(values.filter((x) => x !== v))
  return (
    <div className="space-y-2">
      {values.length === 0 ? (
        <p className="text-meta text-ink-3">{emptyLabel}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {values.map((v) => (
            <li key={v} className="tag h-7 gap-1 pr-0.5 pl-2 text-meta text-ink">
              <span className={mono ? 'mono' : undefined}>{v}</span>
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-icon h-6 w-6"
                onClick={() => remove(v)}
                aria-label={t('rules.remove', { value: v })}
              >
                <Icon name="x" size={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex flex-wrap items-start gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          add()
        }}
      >
        <input
          className={`field w-[320px] max-w-full ${mono ? 'mono' : ''}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          aria-label={placeholder}
          spellCheck={false}
          autoComplete="off"
        />
        <button type="submit" className="btn" disabled={!value || values.includes(value) || problem !== null}>
          <Icon name="plus" size={14} />
          {addLabel}
        </button>
        {problem && <p className="basis-full text-meta text-crit-ink">{problem}</p>}
      </form>
    </div>
  )
}
