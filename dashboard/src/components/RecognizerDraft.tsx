// "Draft from a description": the local reviewer's model writes a recognizer; it is added only when
// `latet rules test` passes it, and you check it before saving.

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { RecognizerEntry } from '../api/types'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { errorText } from './ui'

export function RecognizerDraft({ onAdd }: { onAdd: (entry: RecognizerEntry) => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const run = async () => {
    setBusy(true)
    setProblem(null)
    try {
      const { draft, report } = await api.draftRecognizer(description.trim())
      const failed = [...report.errors, ...(report.examples ?? []).filter((e) => !e.ok).map((e) => e.text)]
      if (report.state === 'error' || failed.length > 0) {
        setProblem(t('rules.recognizers.draft.failed', { errors: failed.join('; ') }))
        return
      }
      onAdd(draft)
      setDescription('')
      toast.show(t('rules.recognizers.draft.added'))
    } catch (error) {
      setProblem(errorText(t, error))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form
      className="mt-3 rounded-lg border border-line bg-surface p-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (description.trim() && !busy) void run()
      }}
    >
      <label className="flex flex-col gap-1">
        <span className="text-ui font-medium text-ink">{t('rules.recognizers.draft.title')}</span>
        <span className="text-meta text-ink-3">{t('rules.recognizers.draft.help')}</span>
        <span className="mt-1 flex flex-wrap gap-2">
          <input
            className="field min-w-[280px] flex-1"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t('rules.recognizers.draft.placeholder')}
          />
          <button type="submit" className="btn" disabled={busy || !description.trim()}>
            <Icon name="plus" size={14} />
            {busy ? t('rules.recognizers.draft.running') : t('rules.recognizers.draft.run')}
          </button>
        </span>
      </label>
      {problem && <p className="mt-2 text-meta text-crit-ink">{problem}</p>}
    </form>
  )
}
