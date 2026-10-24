// A recognizer's form, as the panel of the Recognizers table shows it: every field, why a draft cannot be applied, and
// Try it on a text.

import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { ChecksumInfo, Policy, RecognizerReport } from '../api/types'
import { tk } from '../i18n'
import { fromDraft, type RecognizerDraft, type RecognizerState } from '../lib/recognizers'
import { Icon } from './Icon'
import { StringList } from './StringList'
import { errorText, Segmented, Switch } from './ui'

export const STATE_LABEL: Record<RecognizerState, string> = {
  active: tk('rules.recognizers.state.active'),
  off: tk('rules.recognizers.state.off'),
  error: tk('rules.recognizers.state.error'),
  failing: tk('rules.recognizers.state.failing'),
  unsaved: tk('rules.recognizers.state.unsaved'),
}
export const STATE_STYLE: Record<RecognizerState, string> = {
  active: 'text-good-ink',
  off: 'text-ink-3',
  error: 'text-crit-ink',
  failing: 'text-crit-ink',
  unsaved: 'text-ink-2',
}
export const PROBLEM_LABEL: Record<string, string> = {
  name: tk('rules.recognizers.problems.name'),
  nameTaken: tk('rules.recognizers.problems.nameTaken'),
  type: tk('rules.recognizers.problems.type'),
  pattern: tk('rules.recognizers.problems.pattern'),
  weights: tk('rules.recognizers.problems.weights'),
  modulus: tk('rules.recognizers.problems.modulus'),
  window: tk('rules.recognizers.problems.window'),
  keep: tk('rules.recognizers.problems.keep'),
  minLength: tk('rules.recognizers.problems.minLength'),
}

/** A field: its label names it; its help, under it, is not part of its name. */
function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="flex flex-col gap-1">
        <span className="label">{label}</span>
        {children}
      </label>
      {help && <span className="text-meta text-ink-3">{help}</span>}
    </div>
  )
}

/** Every field of a recognizer, as the form edits it: what it finds, how it checks, what happens to its values. In a
 * repository file (`repo`), what it finds cannot be let through: a latet.yaml sets such a recognizer aside. */
export function RecognizerFields({
  draft,
  set,
  checksums,
  repo = false,
}: {
  draft: RecognizerDraft
  set: (patch: Partial<RecognizerDraft>) => void
  checksums: ChecksumInfo[]
  repo?: boolean
}) {
  const { t } = useTranslation()
  return (
    <>
      <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
        <Field label={t('rules.recognizers.name')} help={t('rules.recognizers.nameHelp')}>
          <input className="field mono" value={draft.name} onChange={(e) => set({ name: e.target.value.toLowerCase() })} spellCheck={false} />
        </Field>
        <Field label={t('rules.recognizers.type')} help={t('rules.recognizers.typeHelp')}>
          <input
            className="field mono"
            value={draft.type}
            placeholder={draft.name.toUpperCase().replace(/-/g, '_')}
            onChange={(e) => set({ type: e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_') })}
            spellCheck={false}
          />
        </Field>
      </div>
      <Field label={t('rules.recognizers.descriptionLabel')}>
        <input className="field" value={draft.description} onChange={(e) => set({ description: e.target.value })} />
      </Field>
      <Field label={t('rules.recognizers.pattern')} help={t('rules.recognizers.patternHelp')}>
        <textarea className="field mono min-h-[56px]" rows={2} value={draft.pattern} onChange={(e) => set({ pattern: e.target.value })} spellCheck={false} />
      </Field>
      <Field label={t('rules.recognizers.checksum')} help={checksums.find((c) => c.name === draft.checksum)?.description}>
        <select className="field w-[260px] max-w-full" value={draft.checksum} onChange={(e) => set({ checksum: e.target.value })}>
          <option value="">{t('rules.recognizers.checksumNone')}</option>
          {checksums.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name}
            </option>
          ))}
          <option value="weighted">{t('rules.recognizers.checksumWeighted')}</option>
        </select>
      </Field>
      {draft.checksum === 'weighted' && (
        <div className="grid grid-cols-[2fr_1fr_1fr] gap-3 max-md:grid-cols-1">
          <Field label={t('rules.recognizers.weights')} help={t('rules.recognizers.weightsHelp')}>
            <input className="field mono" value={draft.weights} onChange={(e) => set({ weights: e.target.value })} />
          </Field>
          <Field label={t('rules.recognizers.modulus')}>
            <input className="field mono" value={draft.modulus} onChange={(e) => set({ modulus: e.target.value })} />
          </Field>
          <Field label={t('rules.recognizers.remainder')}>
            <input className="field mono" value={draft.remainder} onChange={(e) => set({ remainder: e.target.value })} />
          </Field>
        </div>
      )}
      <div>
        <h4 className="label mb-1">{t('rules.recognizers.keywords')}</h4>
        <p className="mb-2 text-meta text-ink-3">{t('rules.recognizers.keywordsHelp')}</p>
        <StringList
          values={draft.words}
          onChange={(words) => set({ words })}
          placeholder={t('rules.recognizers.keywordPlaceholder')}
          addLabel={t('rules.recognizers.addKeyword')}
          mono={false}
          emptyLabel={t('rules.recognizers.noKeywords')}
        />
      </div>
      <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
        <div>
          <h4 className="label mb-1">{t('rules.recognizers.match')}</h4>
          <p className="mb-2 text-meta text-ink-3">{t('rules.recognizers.matchHelp')}</p>
          <StringList
            values={draft.match}
            onChange={(match) => set({ match })}
            placeholder={t('rules.recognizers.examplePlaceholder')}
            addLabel={t('rules.recognizers.addExample')}
            emptyLabel={t('rules.recognizers.noExamples')}
          />
        </div>
        <div>
          <h4 className="label mb-1">{t('rules.recognizers.noMatch')}</h4>
          <p className="mb-2 text-meta text-ink-3">{t('rules.recognizers.noMatchHelp')}</p>
          <StringList
            values={draft.noMatch}
            onChange={(noMatch) => set({ noMatch })}
            placeholder={t('rules.recognizers.examplePlaceholder')}
            addLabel={t('rules.recognizers.addExample')}
            emptyLabel={t('rules.recognizers.noExamples')}
          />
        </div>
      </div>
      <Switch checked={draft.secret} onChange={(secret) => set({ secret })} label={t('rules.recognizers.secret')} description={t('rules.recognizers.secretHelp')} />
      <div className="flex flex-col gap-1">
        <span className="label">{t('rules.recognizers.policy')}</span>
        <Segmented<'' | Policy>
          label={t('rules.recognizers.policy')}
          value={draft.policy}
          onChange={(policy) => set({ policy })}
          options={[
            { value: '', label: t('rules.recognizers.policyDefault') },
            { value: 'redact', label: t('rules.recognizers.policyRedact') },
            // Shown in a repository file only when a hand-written one says so: its save is refused, and says why.
            ...(repo && draft.policy !== 'allow' ? [] : [{ value: 'allow' as const, label: t('rules.recognizers.policyAllow') }]),
          ]}
        />
      </div>
      <details className="rounded-md border border-line p-3">
        <summary className="cursor-pointer text-ui text-ink-2">{t('rules.recognizers.advanced')}</summary>
        <div className="mt-3 space-y-3">
          <div>
            <h4 className="label mb-1">{t('rules.recognizers.acronyms')}</h4>
            <p className="mb-2 text-meta text-ink-3">{t('rules.recognizers.acronymsHelp')}</p>
            <StringList
              values={draft.acronyms}
              onChange={(acronyms) => set({ acronyms })}
              placeholder={t('rules.recognizers.acronymPlaceholder')}
              addLabel={t('rules.recognizers.addKeyword')}
              emptyLabel={t('rules.recognizers.noKeywords')}
            />
          </div>
          <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
            <Field label={t('rules.recognizers.window')} help={t('rules.recognizers.windowHelp')}>
              <input className="field mono" value={draft.window} onChange={(e) => set({ window: e.target.value })} />
            </Field>
            <Field label={t('rules.recognizers.keep')} help={t('rules.recognizers.keepHelp')}>
              <input className="field mono" value={draft.keep} onChange={(e) => set({ keep: e.target.value })} />
            </Field>
            <Field label={t('rules.recognizers.minLength')}>
              <input className="field mono" value={draft.minLength} onChange={(e) => set({ minLength: e.target.value })} />
            </Field>
          </div>
          <Switch checked={draft.header} onChange={(header) => set({ header })} label={t('rules.recognizers.header')} />
          <Switch checked={draft.enabled} onChange={(enabled) => set({ enabled })} label={t('rules.recognizers.enabled')} />
        </div>
      </details>
    </>
  )
}

/** Why a draft cannot be applied, in words. */
export function DraftProblems({ problems }: { problems: string[] }) {
  const { t } = useTranslation()
  if (problems.length === 0) return null
  return (
    <ul className="space-y-0.5 text-meta text-crit-ink" role="status">
      {problems.map((problem) => (
        <li key={problem}>{t(PROBLEM_LABEL[problem] ?? problem)}</li>
      ))}
    </ul>
  )
}

/** Try it: what the draft would find in a text typed or pasted, and how its examples fare. */
export function RecognizerTry({ draft }: { draft: RecognizerDraft }) {
  const { t } = useTranslation()
  const [sample, setSample] = useState('')
  const [report, setReport] = useState<RecognizerReport | null>(null)
  const [trying, setTrying] = useState(false)
  const [tryError, setTryError] = useState<unknown>(null)
  const tryIt = async () => {
    setTrying(true)
    setTryError(null)
    try {
      setReport(await api.testRecognizer(fromDraft(draft), sample))
    } catch (error) {
      setTryError(error)
    } finally {
      setTrying(false)
    }
  }
  return (
    <div className="space-y-3">
      <h4 className="text-ui font-semibold text-ink">{t('rules.recognizers.try.title')}</h4>
      <p className="text-meta text-ink-3">{t('rules.recognizers.try.help')}</p>
      <textarea
        className="field mono min-h-[96px] w-full"
        rows={4}
        value={sample}
        onChange={(e) => setSample(e.target.value)}
        placeholder={t('rules.recognizers.try.placeholder')}
        aria-label={t('rules.recognizers.try.placeholder')}
        spellCheck={false}
      />
      <button type="button" className="btn" disabled={trying || !draft.pattern.trim()} onClick={() => void tryIt()}>
        {trying ? t('rules.recognizers.try.trying') : t('rules.recognizers.try.button')}
      </button>
      {tryError !== null && <p className="text-meta text-crit-ink">{errorText(t, tryError)}</p>}
      {report && <TryReport report={report} sample={sample} />}
    </div>
  )
}

function TryReport({ report, sample }: { report: RecognizerReport; sample: string }) {
  const { t } = useTranslation()
  return (
    <div className="space-y-3 text-ui" aria-live="polite">
      {report.errors.map((error) => (
        <p key={error} className="text-crit-ink">
          {error}
        </p>
      ))}
      {report.try && sample && <Highlighted text={sample} spans={report.try.spans} reason={report.try.reason} />}
      {report.examples && report.examples.length > 0 && (
        <ul className="space-y-1">
          {report.examples.map((example, i) => (
            <li key={i} className="flex items-start gap-2">
              <Icon name={example.ok ? 'check' : 'x'} size={13} className={`mt-0.5 flex-none ${example.ok ? 'text-good-ink' : 'text-crit-ink'}`} />
              <span className="min-w-0">
                <span className="mono break-words text-ink">{example.text}</span>
                <span className="block text-meta text-ink-3">
                  {example.expect === 'match' ? t('rules.recognizers.try.shouldMatch') : t('rules.recognizers.try.shouldNot')}
                  {example.found.length > 0 && ` · ${t('rules.recognizers.try.found', { values: example.found.join(', ') })}`}
                  {!example.ok && example.reason && ` · ${example.reason}`}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {report.stdlib && (
        <p className={report.stdlib.matches > 50 ? 'text-crit-ink' : 'text-ink-3'}>{t('rules.recognizers.try.stdlib', { count: report.stdlib.matches })}</p>
      )}
    </div>
  )
}

function Highlighted({ text, spans, reason }: { text: string; spans: { start: number; end: number }[]; reason: string | null }) {
  const { t } = useTranslation()
  if (spans.length === 0) {
    return (
      <p className="text-ink-2">
        {t('rules.recognizers.try.none')}
        {reason ? ` (${reason})` : ''}
      </p>
    )
  }
  const parts: ReactNode[] = []
  let cursor = 0
  spans.forEach((span, i) => {
    parts.push(text.slice(cursor, span.start))
    parts.push(
      <mark key={i} className="rounded-sm bg-custom/20 px-0.5 text-ink">
        {text.slice(span.start, span.end)}
      </mark>,
    )
    cursor = span.end
  })
  parts.push(text.slice(cursor))
  return <p className="mono break-words whitespace-pre-wrap text-ink">{parts}</p>
}
