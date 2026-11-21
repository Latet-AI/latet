// The Policy page of the Enterprise Edition: the policy in force as a person is told it (where it comes from, when it was
// set, what it locks and which of your own settings are below it, raised while in use). It never shows a value of the
// company's: what is locked is named, its terms and exceptions counted. A server's administrators set the policy from
// the command line (latet server policy set); the security role (policy.edit) also sets it here, in the editor under
// the policy, which shows its text. Once a trial ended the policy is frozen and the editor is not shown.

import { useTranslation } from 'react-i18next'
import { get } from '../../api/client'
import { ErrorState, Loading, PageHeader } from '../../components/ui'
import { tk } from '../../i18n'
import { useMe } from '../../lib/me'
import { useAsync } from '../../lib/useAsync'
import { PolicyEditor } from '../components/PolicyEditor'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { editsPolicy, lineText, lockLines, locksOf, type PolicyFloor, type PolicyView } from '../lib/policy'

const SOURCE: Record<PolicyFloor['source'], string> = {
  managed: tk('ee.policy.source.managed'),
  server: tk('ee.policy.source.server'),
}
const WHEN: Record<PolicyFloor['source'], string> = { managed: tk('ee.policy.written'), server: tk('ee.policy.setOn') }
const EXCEPTIONS: Record<PolicyFloor['exceptions'], string> = {
  personal: tk('ee.policy.exceptions.personal'),
  company: tk('ee.policy.exceptions.company'),
}

export function PolicyPage() {
  const { t } = useTranslation()
  const { me, reload: reloadMe } = useMe()
  const readOnly = useEnterpriseReadOnly() // a trial that ended: the policy is frozen
  const policy = useAsync('ee-policy-page', (signal) => get<PolicyView>('/ee/policy', signal))
  const changed = () => {
    policy.reload()
    reloadMe() // /me.edition.policy, which the locks on Rules follow
  }
  return (
    <>
      <PageHeader title={t('ee.policy.title')} description={t('ee.policy.description')} />
      {policy.loading ? <Loading /> : policy.data ? <PolicyBody view={policy.data} /> : <ErrorState error={policy.error} onRetry={policy.reload} />}
      {editsPolicy(me?.edition) && !readOnly && <PolicyEditor onChanged={changed} />}
    </>
  )
}

function PolicyBody({ view }: { view: PolicyView }) {
  const { t, i18n } = useTranslation()
  const language = i18n.resolvedLanguage ?? 'en'
  const date = (seconds: number) => new Date(seconds * 1000).toLocaleString(language, { dateStyle: 'medium', timeStyle: 'short' })
  const locks = locksOf(view)
  const lines = locks ? Object.values(lockLines(locks)).flat() : []
  return (
    <div className="max-w-[860px] space-y-4">
      {view.problems.map((problem, i) => (
        <p key={`${i}:${problem}`} className="text-ui text-warn-ink">
          {problem}
        </p>
      ))}
      {view.floors.length === 0 && <p className="text-ui text-ink-2">{t('ee.policy.none')}</p>}
      {view.floors.map((floor) => (
        <section key={`${floor.source}:${floor.origin}`} className="panel space-y-2 p-4">
          <h2 className="text-title font-semibold text-ink">{t(SOURCE[floor.source])}</h2>
          <p className="mono break-all text-meta text-ink-2">{floor.origin}</p>
          <p className="text-ui text-ink-2">{t(WHEN[floor.source], { date: date(floor.received) })}</p>
          <p className="text-ui text-ink">{t(EXCEPTIONS[floor.exceptions])}</p>
          {floor.notices > 0 && <p className="text-meta text-warn-ink">{t('ee.policy.notRead', { count: floor.notices })}</p>}
        </section>
      ))}
      {view.floors.length > 0 && (
        <section className="space-y-1">
          <h2 className="text-title font-semibold text-ink">{t('ee.policy.sets')}</h2>
          {lines.length === 0 ? (
            <p className="text-ui text-ink-2">{t('ee.policy.setsNothing')}</p>
          ) : (
            <ul className="list-disc pl-5 text-ui text-ink">
              {lines.map((line) => (
                <li key={`${line.key}:${JSON.stringify(line.vars ?? {})}`}>{lineText(t, line)}</li>
              ))}
            </ul>
          )}
        </section>
      )}
      {view.below.length > 0 && (
        <section className="space-y-1">
          <h2 className="text-title font-semibold text-ink">{t('ee.policy.below')}</h2>
          <ul className="mono list-disc pl-5 text-meta text-ink-2">
            {view.below.map((reason, i) => (
              <li key={`${i}:${reason}`}>{reason}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
