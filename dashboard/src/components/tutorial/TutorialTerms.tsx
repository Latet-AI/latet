// Your terms, as the tutorial's `terms` chapter shows them: the real editor of Rules, and a Save that sends
// the terms alone (`PUT /config` with `terms`), where the Rules page sends its whole form.

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { Term } from '../../api/types'
import { useMe } from '../../lib/me'
import { whenStale } from '../../lib/refused'
import { useAsync } from '../../lib/useAsync'
import { TermsEditor } from '../rules'
import { useToast } from '../Toast'
import { InlineError, Loading } from '../ui'

export function TutorialTerms() {
  const config = useAsync('tutorial:terms', (signal) => api.config(signal))
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
  // Saved: the form starts again from the terms in use.
  const terms = config.data.terms
  return (
    <TermsForm
      key={JSON.stringify(terms)}
      terms={terms}
      etag={config.data.etag}
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
 * `etag`: of the GET /config the terms were read with, which a save sends back (refused when they changed since; the
 * terms are then read again, `onStale`, and `refused` says why in the form made anew from them).
 */
function TermsForm({
  terms,
  etag,
  refused,
  onSaved,
  onStale,
}: {
  terms: Term[]
  etag: string
  refused: unknown
  onSaved: () => void
  onStale: (error: unknown) => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const titleId = useId()
  const [draft, setDraft] = useState(terms)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(refused)
  const dirty = JSON.stringify(draft) !== JSON.stringify(terms)

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const result = await api.saveConfig({ terms: draft }, etag)
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
      <h3 id={titleId} className="text-ui font-semibold text-ink">
        {t('rules.terms.title')}
      </h3>
      <p className="mt-0.5 mb-3 max-w-[80ch] text-meta text-ink-3">{t('rules.terms.description')}</p>
      <TermsEditor terms={draft} onChange={setDraft} />
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
          <button type="button" className="btn" disabled={!dirty || saving} onClick={() => setDraft(terms)}>
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
