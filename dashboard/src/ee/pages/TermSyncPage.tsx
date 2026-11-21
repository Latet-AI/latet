// Term sync (Latet Enterprise, E6), on a Latet server, for whoever terms.manage allows (the security role): where the
// company's lists come from, when each was last read and what it gave, a sync asked for and its result, the sources and
// their secrets [D4], and the terms no longer in their source, masked until someone removes them here. The terms and the
// sources' details are shown to a session the company's sign-in opened, and every change is made from one; a source at
// the server's own computer, and its secret, are `latet server terms`' alone. Once a trial ended the page is read-only.

import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../../api/client'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toast'
import { ConfirmDialog, EmptyState, ErrorState, errorText, InlineCodeText, InlineError, Loading, PageHeader } from '../../components/ui'
import { tk } from '../../i18n'
import { useMe } from '../../lib/me'
import { companySignInNeeded } from '../../lib/refused'
import { type AsyncState, useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { termSyncApi } from '../api'
import { CompanySignInWay } from '../components/CompanyWay'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import {
  allSelected,
  byType,
  followed,
  keepListed,
  lastStarted,
  localSource,
  needsSecret,
  pace,
  ranText,
  refusedKey,
  sourceState,
  sourcesRefusal,
  toggle,
  type FollowRead,
  type Following,
} from '../lib/termSync'
import type { GoneTerm, TermSourcePlace, TermSourceSettings, TermSyncSource, TermSyncSources, TermSyncState } from '../types'

const KIND: Record<NonNullable<TermSyncSource['kind']>, string> = {
  file: tk('ee.termSync.kind.file'),
  url: tk('ee.termSync.kind.url'),
  graph: tk('ee.termSync.kind.graph'),
  ldap: tk('ee.termSync.kind.ldap'),
}
const SAID = { stillRunning: tk('ee.termSync.stillRunning'), notShown: tk('ee.termSync.notShown') }

/** A change refused because the trial ended meanwhile: /me is read again, and the page turns read-only. */
function trialEnded(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 403) return false
  return (error.body as { code?: unknown } | null)?.code === 'trial-ended'
}

/**
 * A sync asked for (by hand, or with a secret set), and its result told (`followed`): the state read again every 2
 * seconds while a source asked for has not run since, then every 15 seconds once past a minute (said once), for 15
 * minutes at most (then said, and no longer waited for); one toast per source when its new run is there. A read that
 * fails is said once; one refused (4xx) is not tried again.
 */
function useSyncRuns(state: AsyncState<TermSyncState>) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const toast = useToast()
  const { reload: reloadMe } = useMe()
  const [asking, setAsking] = useState<string | null>(null) // a source's name, or '*' for every one
  const [waiting, setWaiting] = useState<Following | null>(null)

  /** Waits for the runs of `names`, from the runs the page had read when it asked (`sources`). */
  const wait = (names: readonly string[], sources: readonly TermSyncSource[]) => {
    if (names.length === 0) return
    const before = lastStarted(sources, names)
    setWaiting((w) => ({ before: { ...before, ...w?.before }, since: Date.now(), late: false, failed: w?.failed ?? false }))
  }

  const tick = useEffectEvent(async (current: Following) => {
    let read: FollowRead
    let failure: unknown
    try {
      const now = await termSyncApi.state()
      state.mutate(() => now)
      read = { sources: now.sources }
    } catch (error) {
      failure = error
      const refused = error instanceof ApiError && error.status >= 400 && error.status < 500
      read = { error: refused ? 'stop' : 'again' }
    }
    const step = followed(current, read, Date.now())
    for (const run of step.runs) toast.show(ranText(t, fmt.number, run), run.outcome === 'ok' ? 'ok' : 'fail')
    for (const said of step.say) {
      if (said !== 'failed') toast.show(t(SAID[said]), 'info')
      else {
        toast.show(errorText(t, failure), 'fail')
        if (trialEnded(failure)) reloadMe()
      }
    }
    setWaiting((w) => {
      if (!w || (step.stop && w.since === current.since)) return null // a later ask follows on
      const before = Object.fromEntries(Object.entries(w.before).filter(([name]) => !step.done.includes(name)))
      return Object.keys(before).length ? { ...w, before, late: w.late || step.late, failed: w.failed || step.failed } : null
    })
  })

  useEffect(() => {
    if (waiting === null) return
    const timer = setTimeout(() => void tick(waiting), pace(Date.now() - waiting.since))
    return () => clearTimeout(timer)
  }, [waiting])

  const ask = async (source?: string) => {
    const sources = state.data?.sources ?? []
    setAsking(source ?? '*')
    try {
      const { asked } = await termSyncApi.run(source)
      toast.show(t('ee.termSync.asked'), 'info')
      wait(asked, sources)
    } catch (error) {
      toast.show(errorText(t, error), 'fail') // 403 not-manageable or trial-ended: in the server's words
      if (trialEnded(error)) reloadMe()
    } finally {
      setAsking(null)
    }
  }

  return { asking, ask, wait }
}

export function TermSyncPage() {
  const { t } = useTranslation()
  const fmt = useFormat()
  const readOnly = useEnterpriseReadOnly() // a trial ended: read, not changed
  const state = useAsync('ee-term-sync', (signal) => termSyncApi.state(signal))
  const gone = useAsync('ee-term-sync-gone', (signal) => termSyncApi.gone(signal))
  // The sources' details: the editor's text, which sources read with a secret, and the etag a change names.
  const sources = useAsync(readOnly ? null : 'ee-term-sync-sources', (signal) => termSyncApi.sources(signal))
  const runs = useSyncRuns(state)
  const [secretOf, setSecretOf] = useState<string | null>(null)
  const data = state.data
  const configured = data?.sources.filter((source) => source.configured) ?? []

  const header = (
    <PageHeader
      title={t('ee.termSync.title')}
      description={t('ee.termSync.description')}
      actions={
        !readOnly &&
        configured.length > 0 && (
          <button type="button" className="btn" disabled={runs.asking !== null || data?.paused} onClick={() => void runs.ask()}>
            <Icon name="refresh" size={14} />
            {runs.asking === '*' ? t('ee.termSync.running') : t('ee.termSync.runAll')}
          </button>
        )
      }
    />
  )
  if (!data) {
    return (
      <>
        {header}
        <div className="panel">{state.error ? <ErrorState error={state.error} onRetry={state.reload} /> : <Loading />}</div>
      </>
    )
  }
  const settings = (name: string): TermSourceSettings | undefined => sources.data?.sources.find((s) => s.name === name)
  const hasLocal = sources.data?.sources.some(localSource) ?? false
  const said = data.paused || data.problems.length > 0 || data.unreadable > 0 // a line above the sources

  return (
    <div className={state.stale && !runs.asking ? 'opacity-60' : undefined}>
      {header}
      {said && (
        <div className="mb-6 space-y-2">
          {data.paused && (
            <p role="status" className="panel px-4 py-3 text-ui text-warn-ink">
              {t('ee.termSync.paused')}
            </p>
          )}
          {data.problems.map((problem, i) => (
            // In the server's own words, which say what each line is about (an instance that masks no synced term, one
            // without the `ldap` extra), its commands as code: the save's problems are shown alike.
            <p key={`${i}:${problem}`} role="status" className="panel px-4 py-3 text-ui text-warn-ink">
              <InlineCodeText text={problem} />
            </p>
          ))}
          {data.unreadable > 0 && (
            <p role="status" className="panel px-4 py-3 text-ui text-crit-ink">
              {t('ee.termSync.unreadable', { count: data.unreadable, n: fmt.number(data.unreadable) })}
            </p>
          )}
        </div>
      )}
      <section aria-labelledby="term-sync-sources" className="mb-8">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="term-sync-sources" className="text-title font-semibold text-ink">
            {t('ee.termSync.sources.title')}
          </h2>
          <p className="text-ui text-ink-2 tabular-nums">
            {t('ee.termSync.kept', { count: data.kept, n: fmt.number(data.kept), most: fmt.number(data.most) })}
          </p>
        </div>
        {data.sources.length === 0 ? (
          <div className="panel">
            <EmptyState icon="refresh" title={t('ee.termSync.sources.empty')}>
              {!readOnly && <InlineCodeText text={t('ee.termSync.sources.howTo')} />}
            </EmptyState>
          </div>
        ) : (
          <div className="panel overflow-x-auto">
            <table className="data-table w-full">
              <thead>
                <tr>
                  <th scope="col">{t('ee.termSync.sources.name')}</th>
                  <th scope="col">{t('ee.termSync.sources.kind')}</th>
                  <th scope="col">{t('ee.termSync.sources.last')}</th>
                  <th scope="col">{t('ee.termSync.sources.terms')}</th>
                  {!readOnly && (
                    <th scope="col">
                      <span className="sr-only">{t('ee.termSync.run')}</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {data.sources.map((source) => {
                  const set = settings(source.name)
                  return (
                    <SourceRow
                      key={source.name}
                      source={source}
                      readOnly={readOnly}
                      paused={data.paused}
                      busy={runs.asking !== null}
                      asking={runs.asking === source.name}
                      onRun={() => void runs.ask(source.name)}
                      secret={source.configured && set !== undefined && needsSecret(set) && !localSource(set)}
                      onSecret={() => setSecretOf(source.name)}
                    />
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {!readOnly && <SourcesEditor read={sources} hasLocal={hasLocal} paused={data.paused} onChanged={state.reload} />}
      </section>
      <GoneTerms gone={gone} readOnly={readOnly} paused={data.paused} onRemoved={() => {
        gone.reload()
        state.reload()
      }} />
      {!readOnly && (
        <SecretDialog
          source={secretOf}
          place={secretOf === null ? null : (sources.data?.where?.[secretOf] ?? null)}
          paused={data.paused}
          etag={sources.data?.etag ?? null}
          onClose={() => setSecretOf(null)}
          onChanged={() => {
            sources.reload()
            state.reload()
          }}
          onSaved={(name) => runs.wait([name], data.sources)}
        />
      )}
    </div>
  )
}

function SourceRow({
  source,
  readOnly,
  paused,
  busy,
  asking,
  onRun,
  secret,
  onSecret,
}: {
  source: TermSyncSource
  readOnly: boolean
  /** The license pauses term sync: the server refuses every change (403 not-manageable), so none is offered. */
  paused: boolean
  busy: boolean
  asking: boolean
  onRun: () => void
  secret: boolean
  onSecret: () => void
}) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const state = sourceState(source)
  const when = source.last ? fmt.relative(source.last.finished) : ''
  const syncing = asking || source.asked !== null
  return (
    <tr>
      <td className="mono">{source.name}</td>
      <td>
        {source.kind ? t(KIND[source.kind]) : t('common.none')}
        {source.every_minutes !== null && (
          <span className="block text-meta text-ink-3">{t('ee.termSync.every', { count: source.every_minutes })}</span>
        )}
      </td>
      <td>
        {state === 'ok' && t('ee.termSync.state.ok', { when })}
        {state === 'never' && <span className="text-ink-3">{t('ee.termSync.state.never')}</span>}
        {state === 'retired' && <span className="text-ink-3">{t('ee.termSync.state.retired')}</span>}
        {state === 'failed' && (
          <span className="text-crit-ink">{t('ee.termSync.state.failed', { when, reason: source.last?.reason ?? '' })}</span>
        )}
        {syncing && <span className="block text-meta text-ink-2">{t('ee.termSync.running')}</span>}
      </td>
      <td>
        <span className="font-medium text-ink tabular-nums">{fmt.number(source.terms)}</span>
        {source.terms > 0 && (
          <span className="block text-meta text-ink-3 tabular-nums">
            {byType(source.by_type)
              .map(([type, n]) => `${type} ${fmt.number(n)}`)
              .join(' · ')}
          </span>
        )}
        {source.gone > 0 && (
          <span className="block text-meta text-warn-ink">
            {t('ee.termSync.gone.count', { count: source.gone, n: fmt.number(source.gone) })}
          </span>
        )}
      </td>
      {!readOnly && (
        <td className="text-right whitespace-nowrap">
          {source.configured && (
            <span className="inline-flex gap-2">
              {secret && (
                <button type="button" className="btn btn-sm" onClick={onSecret} disabled={paused}>
                  <Icon name="key" size={14} />
                  {t('ee.termSync.secret.button')}
                </button>
              )}
              <button type="button" className="btn btn-sm" onClick={onRun} disabled={paused || busy || syncing}>
                {syncing ? t('ee.termSync.running') : t('ee.termSync.run')}
              </button>
            </span>
          )}
        </td>
      )}
    </tr>
  )
}

/**
 * The sources' editor [D4]: the section `term_sync` as YAML, as the Policy page edits the policy's text. A save names
 * the etag of the sources it was made from: when they changed since, it says so and offers to read them again (the
 * text typed is kept until then, and never saved over the other change). A refusal says where and what kind of error,
 * in the server's words, never what was typed.
 */
function SourcesEditor({
  read,
  hasLocal,
  paused,
  onChanged,
}: {
  read: AsyncState<TermSyncSources>
  hasLocal: boolean
  paused: boolean
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const toast = useToast()
  const { reload: reloadMe } = useMe()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<string | null>(null) // null: the text as read, untouched
  // Why the last save was refused: the server's own words (`server`: where, and what kind of error), or the page's.
  const [refusal, setRefusal] = useState<{ text: string; server: boolean } | null>(null)
  const [changed, setChanged] = useState(false) // 409 sources-changed: read them again before saving
  const [company, setCompany] = useState(false)
  const [busy, setBusy] = useState(false)
  const [late, setLate] = useState<string[]>([]) // the last save's problems, in the server's words
  const view = read.data
  const text = draft ?? view?.text ?? ''
  const named = (names: string[]) => (names.length ? fmt.list(names) : fmt.number(0))

  const close = () => {
    setOpen(false)
    setDraft(null)
    setRefusal(null)
    setChanged(false)
    setCompany(false)
  }
  const again = () => {
    setDraft(null)
    setChanged(false)
    setRefusal(null)
    read.reload()
  }
  const save = async () => {
    if (!view) return
    setBusy(true)
    setRefusal(null)
    setCompany(false)
    try {
      const saved = await termSyncApi.saveSources(text, view.etag)
      toast.show(t('ee.termSync.sources.saved', { added: named(saved.added), removed: named(saved.removed), changed: named(saved.changed) }), 'ok')
      setLate(saved.problems)
      close()
      read.reload()
      onChanged()
    } catch (error) {
      const said = sourcesRefusal(error)
      if (said === 'changed') {
        setChanged(true) // the text typed stays until they are read again, never saved over the other change
        onChanged() // the sources' rows as they are now
      } else if (said === 'company') setCompany(true)
      else if (refusedKey(said, 'sources')) setRefusal({ text: t(refusedKey(said, 'sources')!), server: false })
      else if (said === 'refused') setRefusal({ text: (error as ApiError).message, server: true })
      else {
        toast.show(errorText(t, error), 'fail')
        if (trialEnded(error)) reloadMe()
      }
    } finally {
      setBusy(false)
    }
  }

  const problems = late.map((problem, i) => (
    <p key={`${i}:${problem}`} className="text-ui text-warn-ink">
      <InlineCodeText text={problem} />
    </p>
  ))
  if (!open) {
    return (
      <div className="mt-3 space-y-2">
        {problems}
        {view && hasLocal && (
          <p className="text-meta text-ink-3">
            <InlineCodeText text={t('ee.termSync.sources.byCommand')} />
          </p>
        )}
        {companySignInNeeded(read.error) ? (
          <CompanySignInWay quiet text={t('ee.termSync.sources.signIn')} />
        ) : read.error && !view ? (
          <div className="flex flex-wrap items-center gap-3">
            <InlineError error={read.error} />
            <button type="button" className="btn btn-sm" onClick={read.reload}>
              <Icon name="refresh" size={14} />
              {t('common.retry')}
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn"
            disabled={!view || paused}
            onClick={() => {
              setOpen(true)
              read.reload() // the text as it is now, and its etag
            }}
          >
            {t('ee.termSync.sources.edit')}
          </button>
        )}
      </div>
    )
  }
  return (
    <div className="panel mt-3 space-y-3 p-4">
      <p className="text-ui text-ink-2">
        <InlineCodeText text={t('ee.termSync.sources.howTo')} />
      </p>
      {hasLocal && (
        <p className="text-meta text-ink-3">
          <InlineCodeText text={t('ee.termSync.sources.byCommand')} />
        </p>
      )}
      {company && <CompanySignInWay />}
      {changed && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border border-warn/60 bg-warn/10 px-4 py-2.5 text-ui text-ink">
          <p className="min-w-0">{t('ee.termSync.sources.changed')}</p>
          <button type="button" className="btn btn-sm" onClick={again}>
            <Icon name="refresh" size={14} />
            {t('ee.termSync.sources.reload')}
          </button>
        </div>
      )}
      {!view ? (
        companySignInNeeded(read.error) ? (
          <CompanySignInWay quiet text={t('ee.termSync.sources.signIn')} />
        ) : read.error ? (
          <ErrorState error={read.error} onRetry={read.reload} />
        ) : (
          <Loading />
        )
      ) : (
        <textarea
          className="field code min-h-[280px] w-full resize-y"
          rows={16}
          spellCheck={false}
          aria-label={t('ee.termSync.sources.editorLabel')}
          value={text}
          readOnly={read.stale}
          onChange={(event) => {
            setDraft(event.target.value)
            setRefusal(null)
          }}
        />
      )}
      {refusal && (
        <p role="alert" className={`text-meta text-crit-ink ${refusal.server ? 'mono' : ''}`}>
          <InlineCodeText text={refusal.text} />
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || paused || changed || !view || read.stale || draft === null || draft === view.text}
          onClick={() => void save()}
        >
          {t('ee.termSync.sources.save')}
        </button>
        <button type="button" className="btn" onClick={close}>
          {t('ee.termSync.sources.cancel')}
        </button>
      </div>
    </div>
  )
}

/**
 * A source's secret [D4]: set (which asks a sync of the source, whose result the page tells) or removed. The value is
 * read from the field when it is sent, never kept in the page's state, and the field is emptied as it is sent and when
 * the dialog closes; a secret is never shown again. A change names the sources' etag, as the editor's does. Before the
 * field, where the source connects (`place`: its addresses, and the DN a directory signs in as), as
 * `latet server terms secret` says it, so that a source someone pointed elsewhere never gets the secret typed in
 * unseen (the final review's M1); while that cannot be said, no secret is asked for.
 */
export function SecretDialog({
  source,
  place,
  paused,
  etag,
  onClose,
  onChanged,
  onSaved,
}: {
  source: string | null
  place: TermSourcePlace | null
  paused: boolean
  etag: string | null
  onClose: () => void
  onChanged: () => void
  onSaved: (source: string) => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reload: reloadMe } = useMe()
  const dialog = useRef<HTMLDialogElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [said, setSaid] = useState<string | null>(null)
  const [company, setCompany] = useState(false)
  const name = source ?? ''

  const empty = () => {
    if (field.current) field.current.value = ''
    setFilled(false)
  }
  const close = () => {
    empty()
    setSaid(null)
    setCompany(false)
    setRemoving(false)
    onClose()
  }
  useEffect(() => {
    const element = dialog.current
    if (!element) return
    if (source !== null && !element.open) element.showModal()
    if (source === null && element.open) element.close()
  }, [source])

  const refused = (error: unknown) => {
    const why = sourcesRefusal(error)
    if (why === 'changed') {
      setSaid(t('ee.termSync.sources.changed'))
      onChanged() // read again: the next try names the sources as they are now
    } else if (why === 'company') setCompany(true)
    else if (refusedKey(why, 'secret')) setSaid(t(refusedKey(why, 'secret')!)) // the secret's command
    else if (why === 'refused') setSaid((error as ApiError).message)
    else {
      setSaid(errorText(t, error))
      if (trialEnded(error)) reloadMe()
    }
  }
  const save = async () => {
    const value = field.current?.value ?? ''
    if (!source || !etag || !place || !value) return
    empty() // sent: no longer in the field
    setBusy(true)
    setSaid(null)
    setCompany(false)
    try {
      await termSyncApi.saveSecret(source, value, etag)
      toast.show(t('ee.termSync.secret.saved', { source }), 'ok')
      onSaved(source)
      close()
    } catch (error) {
      refused(error)
    } finally {
      setBusy(false)
    }
  }
  const remove = async () => {
    if (!source || !etag) return
    setBusy(true)
    setSaid(null)
    setCompany(false)
    try {
      await termSyncApi.removeSecret(source, etag)
      toast.show(t('ee.termSync.secret.removed', { source }), 'ok')
      close()
    } catch (error) {
      setRemoving(false)
      refused(error)
    } finally {
      setBusy(false)
    }
  }

  // The confirmation of a removal is beside this dialog, never inside it: React passes a dialog's cancel (Escape) on to
  // every enclosing onCancel, which would close this one too.
  return (
    <>
    <dialog
      ref={dialog}
      aria-labelledby="term-sync-secret-title"
      onCancel={(event) => {
        event.preventDefault()
        if (event.target === event.currentTarget) close() // its own Escape alone
      }}
      className="floating m-auto w-[460px] max-w-[calc(100vw-32px)] p-0 backdrop:bg-[rgb(10_11_13/0.45)]"
    >
      <form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <div className="space-y-3 p-5">
          <h2 id="term-sync-secret-title" className="text-title font-semibold text-ink">
            {t('ee.termSync.secret.title', { source: name })}
          </h2>
          {company && <CompanySignInWay />}
          {place ? (
            <div className="space-y-1 text-ui text-ink-2">
              <p>
                <InlineCodeText text={t('ee.termSync.secret.where', { addresses: place.addresses.map((a) => `\`${a}\``).join(', ') })} />
              </p>
              {place.bind !== null && <p className="break-all">{t('ee.termSync.secret.signsInAs', { bind: place.bind })}</p>}
            </div>
          ) : (
            <p role="alert" className="text-meta text-warn-ink">
              {t('ee.termSync.secret.whereUnknown')}
            </p>
          )}
          {place && (
            <label className="flex flex-col gap-1">
              <span className="label">{t('ee.termSync.secret.value')}</span>
              <input
                ref={field}
                type="password"
                autoComplete="off"
                spellCheck={false}
                className="field mono"
                onInput={(event) => setFilled(event.currentTarget.value !== '')}
              />
              <span className="text-meta text-ink-3">{t('ee.termSync.secret.never')}</span>
            </label>
          )}
          {said && (
            <p role="alert" className="text-meta text-crit-ink">
              <InlineCodeText text={said} />
            </p>
          )}
        </div>
        <div className="flex flex-wrap justify-between gap-2 border-t border-line px-5 py-3">
          <button type="button" className="btn" disabled={busy || paused || !etag} onClick={() => setRemoving(true)}>
            <Icon name="trash" size={14} />
            {t('ee.termSync.secret.remove')}
          </button>
          <span className="flex gap-2">
            <button type="button" className="btn" onClick={close}>
              {t('ee.termSync.sources.cancel')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy || paused || !filled || !etag || !place}>
              {t('ee.termSync.secret.save')}
            </button>
          </span>
        </div>
      </form>
    </dialog>
    <ConfirmDialog
      open={removing}
      title={t('ee.termSync.secret.title', { source: name })}
      confirmLabel={t('ee.termSync.secret.remove')}
      busy={busy || paused}
      onCancel={() => setRemoving(false)}
      onConfirm={() => void remove()}
    >
      {t('ee.termSync.secret.removeConfirm', { source: name })}
    </ConfirmDialog>
    </>
  )
}

/** The terms no longer in their source, the latest first: masked until someone removes them here (removing lowers
 * protection), read from a session the company's sign-in opened. */
function GoneTerms({
  gone,
  readOnly,
  paused,
  onRemoved,
}: {
  gone: AsyncState<{ items: GoneTerm[]; total: number }>
  readOnly: boolean
  paused: boolean
  onRemoved: () => void
}) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const toast = useToast()
  const { reload: reloadMe } = useMe()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirm, setConfirm] = useState(false)
  const [removing, setRemoving] = useState(false)
  const items = gone.data?.items ?? []
  const ids = items.map((term) => term.id)
  const chosen = keepListed(selected, ids)

  async function remove() {
    setRemoving(true)
    try {
      const { removed } = await termSyncApi.remove([...chosen])
      toast.show(t('ee.termSync.gone.removed', { count: removed }), 'ok')
      setSelected(new Set())
      setConfirm(false)
      onRemoved()
    } catch (error) {
      setConfirm(false)
      toast.show(errorText(t, error), 'fail')
      if (trialEnded(error)) reloadMe()
    } finally {
      setRemoving(false)
    }
  }

  let body = <Loading />
  if (companySignInNeeded(gone.error)) body = <CompanySignInWay quiet text={t('ee.termSync.gone.signIn')} />
  else if (!gone.data && gone.error) body = <div className="panel"><ErrorState error={gone.error} onRetry={gone.reload} /></div>
  else if (gone.data && items.length === 0) {
    body = (
      <div className="panel">
        <EmptyState icon="check" title={t('ee.termSync.gone.empty')} />
      </div>
    )
  } else if (gone.data) {
    body = (
      <>
        <div className="panel overflow-x-auto">
          <table className="data-table w-full">
            <thead>
              <tr>
                {!readOnly && (
                  <th scope="col" className="w-8">
                    <input
                      type="checkbox"
                      className="accent-[var(--ink)]"
                      aria-label={t('ee.termSync.gone.selectAll')}
                      disabled={paused}
                      checked={allSelected(ids, chosen)}
                      onChange={() => setSelected(allSelected(ids, chosen) ? new Set() : new Set(ids))}
                    />
                  </th>
                )}
                <th scope="col">{t('ee.termSync.gone.value')}</th>
                <th scope="col">{t('ee.termSync.gone.type')}</th>
                <th scope="col">{t('ee.termSync.gone.source')}</th>
                <th scope="col">{t('ee.termSync.gone.since')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((term) => (
                <tr key={term.id}>
                  {!readOnly && (
                    <td>
                      <input
                        type="checkbox"
                        className="accent-[var(--ink)]"
                        aria-label={t('ee.termSync.gone.select', { value: term.value })}
                        disabled={paused}
                        checked={chosen.has(term.id)}
                        onChange={() => setSelected(toggle(chosen, term.id))}
                      />
                    </td>
                  )}
                  <td className="text-ink">{term.value}</td>
                  <td className="mono">{term.type}</td>
                  <td className="mono">{term.source}</td>
                  <td className="tabular-nums">{fmt.dateTime(term.gone)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {gone.data.total > items.length && (
          <p className="mt-2 text-meta text-ink-3">
            {t('ee.termSync.gone.more', { shown: fmt.number(items.length), total: fmt.number(gone.data.total) })}
          </p>
        )}
        {!readOnly && (
          <div className="mt-3 flex justify-end">
            <button type="button" className="btn btn-destructive" disabled={paused || chosen.size === 0 || removing} onClick={() => setConfirm(true)}>
              <Icon name="trash" size={14} />
              {t('ee.termSync.gone.remove', { count: chosen.size })}
            </button>
          </div>
        )}
      </>
    )
  }

  return (
    <section id="term-sync-gone" aria-labelledby="term-sync-gone-title">
      <h2 id="term-sync-gone-title" className="mb-1 text-title font-semibold text-ink">
        {t('ee.termSync.gone.title')}
      </h2>
      <p className="mb-3 max-w-[80ch] text-ui text-ink-2">{t('ee.termSync.gone.description')}</p>
      {body}
      <ConfirmDialog
        open={confirm}
        title={t('ee.termSync.gone.confirmTitle', { count: chosen.size })}
        confirmLabel={t('ee.termSync.gone.confirm')}
        busy={removing || paused}
        onConfirm={() => void remove()}
        onCancel={() => setConfirm(false)}
      >
        {t('ee.termSync.gone.confirmBody')}
      </ConfirmDialog>
    </section>
  )
}
