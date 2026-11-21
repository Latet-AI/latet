import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { ConfirmDialog, ErrorState, Loading, errorText } from '../../components/ui'
import { companySignInNeeded } from '../../lib/refused'
import { useAsync } from '../../lib/useAsync'
import { policyApi } from '../api'
import { CompanySignInWay } from './CompanyWay'
import { canSave, changePolicy, removable, storedText } from '../lib/policy'

/**
 * The Policy page's editor, for the security role on a server (policy.edit): the stored policy's text, saved as the
 * floor of every person and API key on the server, or removed. A text the server cannot read is refused with its
 * reason, shown under the text, and the policy in force stays. A save, and a removal, name the policy their text was
 * read from: when someone else changed it since, the editor says so and shows it again. While the server asks for the
 * company's sign-in, the text is read only from a session it opened (it holds the company's terms and values): the
 * editor says so.
 */
export function PolicyEditor({ onChanged }: { onChanged: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const stored = useAsync('ee-policy-text', (signal) => policyApi.text(signal))
  const [draft, setDraft] = useState<string | null>(null) // null: the stored text, untouched
  const [refusal, setRefusal] = useState<string | null>(null)
  const [changedMeanwhile, setChangedMeanwhile] = useState(false)
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [late, setLate] = useState<string[]>([]) // the last save's problems, in the server's words (R27)
  const view = stored.data ?? null
  const text = draft ?? storedText(view)
  // `next`: the text shown once it is done (what was saved, or nothing), the stored one again after the reload.
  const change = async (action: () => Promise<unknown>, done: string, next: string) => {
    setBusy(true)
    setRefusal(null)
    setChangedMeanwhile(false)
    const came = await changePolicy(action)
    switch (came.kind) {
      case 'done':
        setLate(came.problems)
        toast.show(done, 'ok')
        setDraft(next)
        stored.reload()
        onChanged()
        break
      case 'refused':
        setRefusal(came.reason)
        break
      case 'changed': // someone else's change, a save's or a removal's: shown again as it is now
        setChangedMeanwhile(true)
        setDraft(null)
        stored.reload()
        onChanged()
        break
      case 'failed':
        toast.show(errorText(t, came.error), 'fail')
        break
    }
    setBusy(false)
    setRemoving(false)
  }
  return (
    <section className="mt-6 max-w-[860px] space-y-3">
      <h2 className="text-title font-semibold text-ink">{t('ee.policy.edit.title')}</h2>
      <p className="text-ui text-ink-2">{t('ee.policy.edit.description')}</p>
      {stored.loading ? (
        <Loading />
      ) : !view ? (
        companySignInNeeded(stored.error) ? (
          <CompanySignInWay quiet text={t('ee.policy.edit.companySignIn')} />
        ) : (
          <ErrorState error={stored.error} onRetry={stored.reload} />
        )
      ) : (
        <>
          {[...view.problems, ...late].map((problem, i) => (
            <p key={`${i}:${problem}`} className="text-ui text-warn-ink">
              {problem}
            </p>
          ))}
          {changedMeanwhile && (
            <p role="alert" className="text-ui text-warn-ink">
              {t('ee.policy.edit.changed')}
            </p>
          )}
          <textarea
            className="field code min-h-[280px] w-full resize-y"
            rows={14}
            spellCheck={false}
            aria-label={t('ee.policy.edit.text')}
            value={text}
            onChange={(event) => {
              setDraft(event.target.value)
              setRefusal(null)
            }}
          />
          {refusal && (
            <p role="alert" className="mono text-meta text-crit-ink">
              {refusal}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || !canSave(text, view)}
              onClick={() => void change(() => policyApi.save(text, view.etag), t('ee.policy.edit.saved'), text)}
            >
              {t('ee.policy.edit.save')}
            </button>
            {removable(view) && (
              <button type="button" className="btn" disabled={busy} onClick={() => setRemoving(true)}>
                {t('ee.policy.edit.remove')}
              </button>
            )}
          </div>
          <ConfirmDialog
            open={removing}
            title={t('ee.policy.edit.removeTitle')}
            confirmLabel={t('ee.policy.edit.remove')}
            busy={busy}
            onCancel={() => setRemoving(false)}
            onConfirm={() => void change(() => policyApi.remove(view.etag), t('ee.policy.edit.removed'), '')}
          >
            {t('ee.policy.edit.removeBody')}
          </ConfirmDialog>
        </>
      )}
    </section>
  )
}
