import { useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { PlaygroundResult, Profile, SlotKind } from '../api/types'
import { ClassLegend } from '../components/exchange/Legend'
import { ValuesList } from '../components/exchange/SlotView'
import { Icon } from '../components/Icon'
import { blockMarks } from '../components/masked/marks'
import { MaskedText } from '../components/masked/MaskedText'
import { EmptyState, InlineError, PageHeader } from '../components/ui'
import { tk } from '../i18n'
import { KIND_LABEL, PROFILE_LABEL, PROFILES } from '../lib/labels'
import { playgroundSample } from '../lib/samples'

/** The kinds of text that mask differently, each with what changes for it (the others mask as one of these). */
const KINDS = ['user_text', 'attachment', 'tool_result', 'system'] as const satisfies readonly SlotKind[]
type Kind = (typeof KINDS)[number]
const KIND_HELP: Record<Kind, string> = {
  user_text: tk('playground.kindHelps.user_text'),
  attachment: tk('playground.kindHelps.attachment'),
  tool_result: tk('playground.kindHelps.tool_result'),
  system: tk('playground.kindHelps.system'),
}

interface Run {
  text: string
  kind: Kind
  profile: Profile | ''
  result: PlaygroundResult
}

export function PlaygroundPage() {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [kind, setKind] = useState<Kind>('user_text')
  // '': your rules; else the text as that profile would mask it, your settings left as they are.
  const [profile, setProfile] = useState<Profile | ''>('')
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const latest = useRef(0)

  // Masked when you ask (the button, or Ctrl+Enter), never as you type: a text changed since shows its last result
  // dimmed. Only the answer to the last ask shows.
  const mask = async () => {
    if (!text.trim()) return
    const asked = ++latest.current
    const sent = { text, kind, profile }
    setBusy(true)
    try {
      const result = await api.playground(sent.text, sent.kind, sent.profile || undefined)
      if (asked === latest.current) {
        setRun({ ...sent, result })
        setError(null)
      }
    } catch (err) {
      if (asked === latest.current) setError(err)
    } finally {
      if (asked === latest.current) setBusy(false)
    }
  }

  const marks = useMemo(() => (run ? blockMarks(run.text, run.result.masked, run.result.replacements, 'playground') : null), [run])
  const stale = run !== null && (run.text !== text || run.kind !== kind || run.profile !== profile)
  const showRun = run !== null && text.trim() !== ''

  return (
    <>
      <PageHeader title={t('playground.title')} description={t('playground.description')} docs="page.playground" />

      <p className="mb-4 flex max-w-[90ch] items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-ui text-ink-2">
        <Icon name="shield" size={15} className="mt-0.5 flex-none text-good-ink" />
        {t('playground.nothingStored')}
      </p>

      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          void mask()
        }}
      >
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="label">{t('playground.kind')}</span>
              <select className="field w-[220px]" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {t(KIND_LABEL[k])}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="label">{t('playground.profile')}</span>
              <select className="field w-[200px]" value={profile} onChange={(e) => setProfile(e.target.value as Profile | '')}>
                <option value="">{t('playground.profileYours')}</option>
                {PROFILES.map((p) => (
                  <option key={p} value={p}>
                    {t(PROFILE_LABEL[p])}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={() => setText(playgroundSample())}>
              {t('playground.example')}
            </button>
            <button type="button" className="btn" onClick={() => setText('')} disabled={!text}>
              {t('playground.clear')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={!text.trim() || busy}>
              {busy ? t('playground.masking') : t('playground.mask')}
            </button>
          </div>
        </div>
        <label className="block">
          <span className="sr-only">{t('playground.input')}</span>
          <textarea
            className="field code min-h-[200px] w-full resize-y"
            rows={10}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault()
                void mask()
              }
            }}
            placeholder={t('playground.placeholder')}
            spellCheck={false}
          />
        </label>
        <p className="max-w-[90ch] text-meta text-ink-3">{t(KIND_HELP[kind])}</p>
        {profile && <p className="max-w-[90ch] text-meta text-ink-3">{t('playground.profileHelp')}</p>}
      </form>

      {error !== null && (
        <div className="mt-3">
          <InlineError error={error} />
        </div>
      )}

      <section aria-labelledby="playground-result" aria-busy={busy} className={`mt-6 ${stale ? 'opacity-60' : ''}`}>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 id="playground-result" className="text-title font-semibold text-ink">
            {t('playground.result')}
          </h2>
          {showRun && marks && <ClassLegend values={marks.values} />}
        </div>
        {!showRun || !marks || !run ? (
          <div className="panel">
            <EmptyState icon="playground" title={t('playground.empty.title')}>
              {t('playground.empty.body')}
            </EmptyState>
          </div>
        ) : (
          <article className="panel overflow-hidden">
            <header className="flex items-center justify-between gap-3 border-b border-line px-3 py-2">
              <p className="text-ui text-ink">
                {run.result.replacements.length === 0
                  ? t('playground.nothingFound')
                  : t('playground.found', { count: run.result.replacements.length })}
              </p>
              {stale && <span className="text-meta text-ink-3">{t('playground.stale')}</span>}
            </header>
            <div className="grid grid-cols-2 max-md:grid-cols-1">
              <div className="min-w-0 border-r border-line max-md:border-r-0 max-md:border-b">
                <p className="border-b border-line px-3 py-1 text-meta text-ink-2">{t('playground.original')}</p>
                <MaskedText text={run.text} marks={marks.original ?? []} footnotes label={t('playground.original')} className="bg-well" />
              </div>
              <div className="min-w-0">
                <p className="border-b border-line px-3 py-1 text-meta text-ink-2">{t('playground.masked')}</p>
                <MaskedText text={run.result.masked} marks={marks.sent} footnotes label={t('playground.masked')} className="bg-well" />
              </div>
            </div>
            {marks.values.length > 0 && <ValuesList values={marks.values} />}
          </article>
        )}
      </section>
    </>
  )
}
