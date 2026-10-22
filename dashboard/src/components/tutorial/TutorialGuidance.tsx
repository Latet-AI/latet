// Rules → Reviewers → Local reviewer → Your guidance, as the tutorial's `llm` chapter shows it: the sentence the
// chapter wrote in for the reader, saved as it is or as they change it, with the reviewer's other settings as they are.

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { LlmReviewSettings } from '../../api/types'
import { useMe } from '../../lib/me'
import { whenStale } from '../../lib/refused'
import { cleanGuidance } from '../../lib/reviewer'
import { useAsync } from '../../lib/useAsync'
import { GuidanceField } from '../ReviewerEditor'
import { useToast } from '../Toast'
import { InlineError, Loading } from '../ui'

/** `text`: the sentence the chapter wrote in for the reader, which the editor starts with while no guidance is saved. */
export function TutorialGuidance({ text }: { text: string }) {
  const config = useAsync('tutorial:guidance', (signal) => api.config(signal))
  // A save refused because the settings changed since they were read: said again in the form made anew from them.
  const [refused, setRefused] = useState<unknown>(null)
  if (config.error && !config.data) {
    return (
      <div className="panel p-4">
        <InlineError error={config.error} />
      </div>
    )
  }
  if (!config.data) {
    return (
      <div className="panel">
        <Loading />
      </div>
    )
  }
  // Saved: the form starts again from the guidance in use.
  const review = config.data.llm_review
  return (
    <GuidanceForm
      key={review.guidance ?? ''}
      review={review}
      etag={config.data.etag}
      text={text}
      refused={refused}
      onSaved={() => {
        setRefused(null)
        config.reload()
      }}
      onStale={(error) => {
        setRefused(error)
        config.reload()
      }}
    />
  )
}

/**
 * `etag`: of the GET /config the reviewer's settings were read with, which a save sends back (refused when they changed
 * since; they are then read again, `onStale`, and `refused` says why in the form made anew from them).
 */
function GuidanceForm({
  review,
  etag,
  text,
  refused,
  onSaved,
  onStale,
}: {
  review: LlmReviewSettings
  etag: string
  text: string
  refused: unknown
  onSaved: () => void
  onStale: (error: unknown) => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const titleId = useId()
  const saved = cleanGuidance(review.guidance)
  // The guidance saved, or the chapter's sentence when none is: Save is on at once, to save it as it is written.
  const start = saved ?? cleanGuidance(text)
  const [draft, setDraft] = useState<string | null>(start)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(refused)
  const dirty = cleanGuidance(draft) !== saved

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      // Only the guidance changes: the sandbox refuses any other setting of the reviewer (409).
      const result = await api.saveConfig({ llm_review: { ...review, guidance: cleanGuidance(draft) } }, etag)
      toast.show(t('rules.saved', { version: result.rules_version }))
      reloadConfig()
      onSaved()
    } catch (err) {
      setError(err)
      whenStale(err, () => onStale(err)) // the settings changed since they were read: read again, for the change anew
    } finally {
      setSaving(false)
    }
  }

  return (
    <section aria-labelledby={titleId} className="panel p-4">
      <h3 id={titleId} className="mb-3 text-ui font-semibold text-ink">
        {t('reviewer.title')}
      </h3>
      <GuidanceField
        title={t('reviewer.guidance.title')}
        help={t('reviewer.guidance.help')}
        placeholder={t('reviewer.guidance.placeholder')}
        value={draft}
        onChange={setDraft}
      />
      <p className="mt-3 max-w-[80ch] text-meta text-ink-3">{t('tutorial.shown.simulated')}</p>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
        <div className="min-w-0 text-ui" role="status">
          {error !== null ? (
            <InlineError error={error} />
          ) : dirty ? (
            <span className="text-ink">{t('rules.unsaved')}</span>
          ) : (
            <span className="text-ink-3">{t('rules.upToDate')}</span>
          )}
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn" disabled={draft === start || saving} onClick={() => setDraft(start)}>
            {t('rules.discard')}
          </button>
          <button type="button" className="btn btn-primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? t('rules.saving') : t('rules.save')}
          </button>
        </div>
      </div>
    </section>
  )
}
