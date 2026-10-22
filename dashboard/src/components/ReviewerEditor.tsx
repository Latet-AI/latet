// The local reviewer's settings (Rules), with Try it: the server reads a sample with the settings shown.

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { LlmReviewSettings, ReviewCategory, ReviewerKey, ReviewerTest, ReviewKind, ServerCertificate } from '../api/types'
import { tk } from '../i18n'
import {
  canTrust,
  cleanGuidance,
  inClear,
  MAX_GUIDANCE,
  needsHttps,
  readableFingerprint,
  REVIEW_CATEGORIES,
  REVIEW_KINDS,
  serverPlace,
} from '../lib/reviewer'
import { pendingKey } from '../lib/deployment'
import { useDeploymentKey, useMe } from '../lib/me'
import { useAsync } from '../lib/useAsync'
import { DocsSectionLink, TitleWithDocs } from './DocsLinks'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { CopyButton, errorText, Segmented, Switch } from './ui'

export const KIND_LABEL: Record<ReviewKind, string> = {
  user_text: tk('reviewer.kinds.user_text'),
  attachment: tk('reviewer.kinds.attachment'),
  system: tk('reviewer.kinds.system'),
  tool_result: tk('reviewer.kinds.tool_result'),
  tool_input: tk('reviewer.kinds.tool_input'),
  tool_description: tk('reviewer.kinds.tool_description'),
  image_text: tk('reviewer.kinds.image_text'),
}

export const CATEGORY_LABEL: Record<ReviewCategory, string> = {
  person: tk('reviewer.categories.person'),
  address: tk('reviewer.categories.address'),
  phone: tk('reviewer.categories.phone'),
  email: tk('reviewer.categories.email'),
  id: tk('reviewer.categories.id'),
  health: tk('reviewer.categories.health'),
  sensitive: tk('reviewer.categories.sensitive'),
  financial: tk('reviewer.categories.financial'),
  secret: tk('reviewer.categories.secret'),
  org: tk('reviewer.categories.org'),
  confidential: tk('reviewer.categories.confidential'),
}

function toggle<T>(list: readonly T[], item: T, on: boolean): T[] {
  return on ? [...list.filter((x) => x !== item), item] : list.filter((x) => x !== item)
}

function numberOrNull(raw: string, min: number): number | null {
  if (raw.trim() === '') return null
  const n = Number.parseInt(raw, 10)
  return Number.isNaN(n) ? null : Math.max(min, n)
}

export function ReviewerEditor({ value, onChange }: { value: LlmReviewSettings; onChange: (next: LlmReviewSettings) => void }) {
  const { t } = useTranslation()
  const { me, unreachable } = useMe()
  const key = useDeploymentKey()
  const set = <K extends keyof LlmReviewSettings>(key: K, v: LlmReviewSettings[K]) => onChange({ ...value, [key]: v })
  const off = !value.enabled
  if (me === undefined) return <p role="status" className="text-meta text-ink-3">{t(pendingKey(unreachable))}</p>
  return (
    <div className="space-y-5">
      <Switch
        checked={value.enabled}
        onChange={(on) => set('enabled', on)}
        label={t('reviewer.enabled')}
        description={t(key('reviewer.enabledHelp'))}
      />
      <fieldset disabled={off} className="space-y-5 disabled:opacity-60">
        <legend className="sr-only">{t('reviewer.title')}</legend>
        {me?.server ? (
          // On a Latet server its server is the shared reviewer's: set for everyone, with its key, never here.
          <>
            <ServerReviewer value={value} />
            <ReviewerKeyField server />
          </>
        ) : (
          <ReviewerServer value={value} onChange={onChange} />
        )}

        <div>
          <TitleWithDocs title={t('reviewer.reads')} docs="section.reviewerReads" />
          <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('reviewer.readsHelp')}</p>
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {REVIEW_KINDS.map((kind) => (
              <li key={kind}>
                <label className="flex cursor-pointer items-center gap-2 text-ui text-ink">
                  <input
                    type="checkbox"
                    className="accent-[var(--ink)]"
                    checked={value.inline.includes(kind)}
                    onChange={(e) => set('inline', toggle(value.inline, kind, e.target.checked))}
                  />
                  {t(KIND_LABEL[kind])}
                </label>
              </li>
            ))}
          </ul>
        </div>

        <div className="flex flex-wrap items-start gap-8">
          <div>
            <TitleWithDocs title={t('reviewer.wait')} docs="section.reviewerWait" />
            <p className="mb-2 text-meta text-ink-3">{t('reviewer.waitHelp')}</p>
            <label className="flex items-center gap-2 text-ui text-ink-2">
              <input
                type="number"
                min={500}
                step={500}
                className="field mono w-[170px]"
                value={value.budget_ms ?? ''}
                placeholder={t('reviewer.noLimit')}
                onChange={(e) => set('budget_ms', numberOrNull(e.target.value, 500))}
                aria-label={t('reviewer.wait')}
              />
              {t('reviewer.waitUnit')}
            </label>
          </div>
          <div>
            <TitleWithDocs title={t('reviewer.onFailure')} docs="section.reviewerWait" />
            <p className="mb-2 text-meta text-ink-3">{t('reviewer.onFailureHelp')}</p>
            <Segmented<'stop' | 'send'>
              label={t('reviewer.onFailure')}
              value={value.on_failure}
              onChange={(v) => set('on_failure', v)}
              options={[
                { value: 'stop', label: t('reviewer.stop') },
                { value: 'send', label: t('reviewer.send') },
              ]}
            />
          </div>
        </div>

        <div className="flex flex-wrap gap-6">
          <Switch
            checked={value.background}
            onChange={(on) => set('background', on)}
            label={t('reviewer.background')}
            description={t('reviewer.backgroundHelp')}
          />
          <Switch checked={value.block} onChange={(on) => set('block', on)} label={t('reviewer.block')} description={t('reviewer.blockHelp')} />
          <Switch
            checked={value.images ?? false}
            onChange={(on) => set('images', on)}
            label={t('reviewer.images')}
            description={t('reviewer.imagesHelp')}
          />
        </div>

        <div>
          <TitleWithDocs title={t('reviewer.masks')} docs="section.reviewerMasks" />
          <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('reviewer.masksHelp')}</p>
          <ul className="flex flex-wrap gap-1.5">
            {REVIEW_CATEGORIES.map((category) => (
              <li key={category}>
                <label
                  className={`tag h-7 cursor-pointer gap-1.5 px-2 text-meta ${value.auto_mask.includes(category) ? 'border-ink text-ink' : 'text-ink-3'}`}
                >
                  <input
                    type="checkbox"
                    className="accent-[var(--ink)]"
                    checked={value.auto_mask.includes(category)}
                    onChange={(e) => set('auto_mask', toggle(value.auto_mask, category, e.target.checked))}
                  />
                  {t(CATEGORY_LABEL[category])}
                </label>
              </li>
            ))}
          </ul>
        </div>

        <div className="space-y-2">
          <GuidanceField
            title={t('reviewer.guidance.title')}
            help={t('reviewer.guidance.help')}
            placeholder={t('reviewer.guidance.placeholder')}
            value={value.guidance ?? null}
            onChange={(next) => set('guidance', next)}
          />
          <ReviewerInstructions guidance={value.guidance ?? null} />
        </div>

        {!me?.server && <ServerDetails value={value} onChange={onChange} />}

        <TryReviewer settings={value} />
      </fieldset>
    </div>
  )
}

/** On a Latet server: where the local reviewer runs, which its administrators set for everyone (shown, not editable). */
function ServerReviewer({ value }: { value: LlmReviewSettings }) {
  const { t } = useTranslation()
  const rows: [string, string, boolean][] = [
    [t('reviewer.url'), value.url, true],
    [t('reviewer.model'), value.model ?? t('reviewer.modelPlaceholder'), value.model !== null],
    [t('reviewer.slots'), value.slots === null ? t('reviewer.slotsAuto') : String(value.slots), false],
    [t('reviewer.chunk'), String(value.chunk_chars), false],
    ...(value.certificate ? [[t('reviewer.certificate.title'), readableFingerprint(value.certificate), true] as [string, string, boolean]] : []),
  ]
  return (
    <div className="max-w-[80ch] space-y-2 rounded-lg border border-line bg-surface p-3" role="note" data-server-reviewer>
      <p className="text-ui font-medium text-ink">{t('shared.title')}</p>
      <p className="text-meta text-ink-3">{t('reviewer.server')}</p>
      <dl className="grid grid-cols-[minmax(0,180px)_minmax(0,1fr)] gap-x-4 gap-y-1 text-ui">
        {rows.map(([label, shown, mono]) => (
          <div key={label} className="contents">
            <dt className="text-ink-3">{label}</dt>
            <dd className={`min-w-0 break-all text-ink ${mono ? 'mono' : ''}`}>{shown}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/** Where the local reviewer runs, on this computer: its address and model, and how its server is reached. */
function ReviewerServer({ value, onChange }: { value: LlmReviewSettings; onChange: (next: LlmReviewSettings) => void }) {
  const { t } = useTranslation()
  const set = <K extends keyof LlmReviewSettings>(key: K, v: LlmReviewSettings[K]) => onChange({ ...value, [key]: v })
  return (
    <>
      <div className="flex flex-wrap gap-4">
        <label className="flex flex-col gap-1">
          <span className="label">{t('reviewer.url')}</span>
          <input
            className="field mono w-[300px]"
            value={value.url}
            // A certificate you trusted belongs to the server it came from: another address forgets it.
            onChange={(e) => onChange({ ...value, url: e.target.value.trim(), certificate: null })}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('reviewer.model')}</span>
          <input
            className="field mono w-[220px]"
            value={value.model ?? ''}
            placeholder={t('reviewer.modelPlaceholder')}
            onChange={(e) => set('model', e.target.value.trim() || null)}
          />
        </label>
      </div>
      <p className="-mt-3 max-w-[80ch] text-meta text-ink-3">{t('reviewer.urlHelp')}</p>
      {serverPlace(value.url) !== 'here' && serverPlace(value.url) !== null && (
        <div className="max-w-[80ch] space-y-2 rounded-lg border border-warn/60 bg-warn/10 p-3">
          <label className="flex cursor-pointer items-start gap-2.5 text-ui text-ink">
            <input
              type="checkbox"
              className="mt-0.5 accent-[var(--ink)]"
              checked={value.remote}
              onChange={(e) => set('remote', e.target.checked)}
            />
            <span>
              <span className="block font-medium">{t('reviewer.remote')}</span>
              <span className="block text-meta text-ink-2">{t('reviewer.remoteHelp')}</span>
            </span>
          </label>
          {needsHttps(value.url) && <p className="text-meta text-crit-ink">{t('reviewer.httpsNeeded')}</p>}
          {inClear(value.url) && <p className="text-meta text-crit-ink">{t('reviewer.inClear')}</p>}
          {canTrust(value.url) && (
            <TrustCertificate url={value.url} trusted={value.certificate ?? null} onChange={(fp) => set('certificate', fp)} />
          )}
        </div>
      )}
      <ReviewerKeyField />
    </>
  )
}

/** The server's details on this computer: other ways to give its key, its slots and the size of the pieces it reads. */
function ServerDetails({ value, onChange }: { value: LlmReviewSettings; onChange: (next: LlmReviewSettings) => void }) {
  const { t } = useTranslation()
  const set = <K extends keyof LlmReviewSettings>(key: K, v: LlmReviewSettings[K]) => onChange({ ...value, [key]: v })
  return (
    <details className="rounded-lg border border-line bg-surface p-3">
      <summary className="cursor-pointer text-ui font-medium text-ink">{t('reviewer.advanced')}</summary>
      <div className="mt-3 space-y-4">
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1">
            <span className="label">{t('reviewer.keyEnv')}</span>
            <input
              className="field mono w-[240px]"
              value={value.api_key_env}
              onChange={(e) => set('api_key_env', e.target.value.trim() || 'LATET_LLM_API_KEY')}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">{t('reviewer.keyFile')}</span>
            <input
              className="field mono w-[300px]"
              value={value.api_key_file ?? ''}
              placeholder={t('reviewer.keyFilePlaceholder')}
              onChange={(e) => set('api_key_file', e.target.value.trim() || null)}
            />
          </label>
        </div>
        <p className="max-w-[80ch] text-meta text-ink-3">{t('reviewer.keyHelp')}</p>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1">
            <span className="label">{t('reviewer.slots')}</span>
            <input
              type="number"
              min={1}
              max={4}
              className="field mono w-[110px]"
              value={value.slots ?? ''}
              placeholder={t('reviewer.slotsAuto')}
              onChange={(e) => set('slots', numberOrNull(e.target.value, 1))}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">{t('reviewer.chunk')}</span>
            <input
              type="number"
              min={1000}
              step={1000}
              className="field mono w-[130px]"
              value={value.chunk_chars}
              onChange={(e) => set('chunk_chars', numberOrNull(e.target.value, 1000) ?? 7000)}
            />
          </label>
        </div>
        <p className="max-w-[80ch] text-meta text-ink-3">{t('reviewer.advancedHelp')}</p>
      </div>
    </details>
  )
}

/** Your guidance to the reviewer: free text after Latet's own instructions, counted against its limit. */
export function GuidanceField({
  title,
  help,
  placeholder,
  value,
  onChange,
}: {
  title: string
  help: string
  placeholder: string
  value: string | null
  onChange: (next: string | null) => void
}) {
  const { t } = useTranslation()
  const id = useId()
  const length = cleanGuidance(value)?.length ?? 0
  const over = length > MAX_GUIDANCE
  return (
    <div className="max-w-[80ch]">
      <div className="flex items-center gap-1">
        <label htmlFor={id} className="block text-ui font-medium text-ink">
          {title}
        </label>
        <DocsSectionLink target="section.guidance" section={title} />
      </div>
      <p className="mb-2 text-meta text-ink-3">{help}</p>
      <textarea
        id={id}
        className="field min-h-[110px] w-full resize-y"
        rows={5}
        value={value ?? ''}
        placeholder={placeholder}
        aria-describedby={`${id}-count`}
        onChange={(e) => onChange(e.target.value === '' ? null : e.target.value)}
      />
      <p id={`${id}-count`} className={`mt-1 text-meta ${over ? 'text-crit-ink' : 'text-ink-3'}`}>
        {over
          ? t('reviewer.guidance.tooLong', { chars: length, max: MAX_GUIDANCE })
          : t('reviewer.guidance.count', { chars: length, max: MAX_GUIDANCE })}
      </p>
    </div>
  )
}

/** What the reviewer is told: Latet's own instructions, read-only and exactly as the daemon sends them, then your guidance. */
export function ReviewerInstructions({ guidance }: { guidance: string | null }) {
  const { t } = useTranslation()
  const prompt = useAsync('reviewer-prompt', (signal) => api.reviewerPrompt(signal))
  const yours = cleanGuidance(guidance)
  const shown = prompt.data
  const full = shown ? (yours ? `${shown.system}\n\n${shown.intro}\n${yours}` : shown.system) : ''
  return (
    <details className="max-w-[100ch] rounded-lg border border-line bg-surface p-3">
      <summary className="cursor-pointer text-ui font-medium text-ink">{t('reviewer.instructions.title')}</summary>
      <div className="mt-3 space-y-2">
        <p className="max-w-[80ch] text-meta text-ink-3">{t('reviewer.instructions.help')}</p>
        {prompt.error !== undefined && prompt.error !== null ? (
          <p className="text-meta text-crit-ink">{errorText(t, prompt.error)}</p>
        ) : !shown ? (
          <p className="text-meta text-ink-3">{t('common.loading')}</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-meta text-ink-3">{t('reviewer.instructions.version', { version: shown.version })}</span>
              <CopyButton text={full} />
            </div>
            <pre className="well code max-h-[420px] overflow-auto px-3 py-2.5 text-ink-3">
              {shown.system}
              {yours && <span className="text-ink">{`\n\n${shown.intro}\n${yours}`}</span>}
            </pre>
            {yours && <p className="text-meta text-ink-3">{t('reviewer.instructions.yours')}</p>}
          </>
        )}
      </div>
    </details>
  )
}

const KEY_SOURCE: Record<NonNullable<ReviewerKey['source']>, string> = {
  saved: tk('reviewer.key.saved'),
  env: tk('reviewer.key.env'),
  file: tk('reviewer.key.file'),
  server: tk('reviewer.key.server'), // on a Latet server: its administrator sets it, for everyone
}

// A command, the same in every language.
const OPENSSL_FINGERPRINT = 'openssl x509 -noout -fingerprint -sha256 -in cert.pem'

/** Trust the server's own certificate (self-signed): you compare its fingerprint with the server's first. */
function TrustCertificate({ url, trusted, onChange }: { url: string; trusted: string | null; onChange: (fingerprint: string | null) => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const [shown, setShown] = useState<ServerCertificate | null>(null)
  const [busy, setBusy] = useState(false)
  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    try {
      await action()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }
  const show = () => run(async () => setShown(await api.reviewerCertificate(url)))
  const trust = (certificate: ServerCertificate) =>
    run(async () => {
      const kept = await api.trustCertificate(url, certificate.fingerprint)
      onChange(kept.fingerprint)
      setShown(null)
      toast.show(t('reviewer.certificate.trustedToast'))
    })
  return (
    <div className="space-y-2 border-t border-warn/40 pt-2">
      <p className="text-ui font-medium text-ink">{t('reviewer.certificate.title')}</p>
      {trusted ? (
        <>
          <p className="text-meta text-ink-2">{t('reviewer.certificate.trusted')}</p>
          <p className="mono text-meta break-all text-ink">{readableFingerprint(trusted)}</p>
          <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => onChange(null)}>
            {t('reviewer.certificate.forget')}
          </button>
        </>
      ) : shown ? (
        <>
          <p className="text-meta text-ink-2">{t('reviewer.certificate.compare')}</p>
          <p className="mono text-meta break-all text-ink">{shown.readable}</p>
          <p className="mono text-meta text-ink-3">{OPENSSL_FINGERPRINT}</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void trust(shown)}>
              {t('reviewer.certificate.same')}
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => setShown(null)}>
              {t('reviewer.certificate.different')}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="text-meta text-ink-2">{t('reviewer.certificate.help')}</p>
          <button type="button" className="btn" disabled={busy} onClick={() => void show()}>
            {t('reviewer.certificate.show')}
          </button>
        </>
      )}
    </div>
  )
}

/**
 * The server's API key: saved by the daemon in the operating system's credential store, never shown again. On a Latet
 * server (`server`) the server gives it, which its administrator sets for everyone: none is typed here.
 */
function ReviewerKeyField({ server = false }: { server?: boolean }) {
  const { t } = useTranslation()
  const toast = useToast()
  const status = useAsync(server ? null : 'reviewer-key', (signal) => api.reviewerKey(signal))
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const source = server ? 'server' : (status.data?.source ?? null)
  const act = async (what: 'save' | 'remove') => {
    setBusy(true)
    try {
      if (what === 'save') await api.saveReviewerKey(key.trim())
      else await api.removeReviewerKey()
      setKey('')
      status.reload()
      toast.show(what === 'save' ? t('reviewer.key.savedToast') : t('reviewer.key.removedToast'))
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="max-w-[80ch]">
      <TitleWithDocs title={t('reviewer.key.title')} docs="section.reviewerKey" />
      <p className="mb-2 text-meta text-ink-3">
        {source ? t(KEY_SOURCE[source], { variable: status.data?.variable ?? '' }) : t('reviewer.key.none')}
      </p>
      {source !== 'server' && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="password"
            autoComplete="off"
            className="field mono w-[400px] max-w-full"
            value={key}
            placeholder={source === 'saved' ? t('reviewer.key.replace') : t('reviewer.key.placeholder')}
            aria-label={t('reviewer.key.title')}
            onChange={(e) => setKey(e.target.value)}
          />
          <button type="button" className="btn" disabled={busy || !key.trim()} onClick={() => void act('save')}>
            {t('reviewer.key.save')}
          </button>
          {source === 'saved' && (
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => void act('remove')}>
              {t('reviewer.key.remove')}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/** Try it: the server reads a sample, or a text of yours, with the settings shown (and a project's guidance after yours). */
export function TryReviewer({ settings, projectGuidance = null }: { settings: LlmReviewSettings; projectGuidance?: string | null }) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [result, setResult] = useState<ReviewerTest | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.testReviewer({ ...settings, enabled: true }, text.trim() || undefined, cleanGuidance(projectGuidance)))
    } catch (e) {
      setError(e)
      setResult(null)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-2 rounded-lg border border-line bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 flex-1 text-meta text-ink-2">{t('reviewer.tryHelp')}</p>
        <button type="button" className="btn" disabled={busy} onClick={() => void run()}>
          <Icon name="play" size={14} />
          {busy ? t('reviewer.trying') : t('reviewer.try')}
        </button>
      </div>
      <textarea
        className="field mono min-h-[56px] w-full resize-y"
        rows={2}
        value={text}
        placeholder={t('reviewer.tryText')}
        aria-label={t('reviewer.tryText')}
        onChange={(e) => setText(e.target.value)}
      />
      {error !== null && <p className="text-meta text-crit-ink">{errorText(t, error)}</p>}
      {result && (
        <div className="mt-1 space-y-2 text-meta" role="status">
          {result.ok ? (
            <p className="flex flex-wrap items-center gap-1.5 text-good-ink">
              <Icon name="check" size={13} />
              {t('reviewer.tryOk', { seconds: result.seconds, model: result.model })}
              {result.slots !== null && (
                <span className="text-ink-3">{t('reviewer.trySlots', { count: result.slots, ctx: result.n_ctx ?? '?' })}</span>
              )}
            </p>
          ) : (
            <p className="text-crit-ink">{t('reviewer.tryFailed', { error: result.error })}</p>
          )}
          {settings.images && result.vision === false && (
            <p className="flex items-start gap-1.5 text-crit-ink">
              <Icon name="ban" size={13} className="mt-px flex-none" />
              {t('reviewer.noVision')}
            </p>
          )}
          {settings.images && result.ok && result.vision == null && <p className="text-ink-3">{t('reviewer.visionUnknown')}</p>}
          <p className="mono max-w-[100ch] break-words rounded bg-well p-2 text-ink-2">{result.view}</p>
          {result.findings.length === 0 ? (
            result.ok && <p className="text-ink-3">{t('reviewer.tryNothing')}</p>
          ) : (
            <ul className="space-y-1">
              {result.findings.map((f, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2">
                  <code className="mono text-ink">{f.text}</code>
                  <span className="text-ink-3">{t(CATEGORY_LABEL[f.category])}</span>
                  <span className={f.masked ? 'text-ink-2' : 'text-warn-ink'}>{f.masked ? t('reviewer.masked') : t('reviewer.suggested')}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
