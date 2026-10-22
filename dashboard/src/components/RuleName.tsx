import { createContext, useContext } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { ruleField, strictField } from '../lib/fields'
import { useDocsHref } from './DocsLinks'
import { ANCHORS, PROJECT_ANCHORS } from './rules'
import { rulesHref } from './ScopeBar'

/** Where a rule of your own settings for a project is edited, by the section of the global rules it would be in. */
const IN_PROJECT: Record<string, string> = {
  [ANCHORS.recognizers]: PROJECT_ANCHORS.recognizers,
  [ANCHORS.data]: PROJECT_ANCHORS.projectData,
  [ANCHORS.terms]: PROJECT_ANCHORS.terms,
}

/** The project of the exchange shown: which of its own settings a rule comes from. */
export const RuleScope = createContext<{ projectId: string; layer: (rule: string | null) => 'repo' | 'personal' | null } | null>(null)

function LayerNote({ layer }: { layer: 'repo' | 'personal' | null }) {
  const { t } = useTranslation()
  if (!layer) return null
  return <span className="ml-1.5 font-sans text-micro text-ink-3">{layer === 'repo' ? t('projects.layer.repo') : t('projects.layer.personal')}</span>
}

/** A detector rule id: the user's own rules link to where they are edited, the others to the reference. */
export function RuleName({ rule }: { rule: string | null }) {
  const { t } = useTranslation()
  const scope = useContext(RuleScope)
  const reference = useDocsHref('rule.detectors')
  if (!rule) return <span className="text-ink-3">{t('common.none')}</span>
  const layer = scope?.layer(rule) ?? null
  // A rule of the project's own settings leads to its tab there: its list's, where the rows of yours and of latet.yaml
  // show; a field rule of latet.yaml, to the repository file's.
  const where = (anchor: string) => {
    if (!layer || !scope) return `/rules#${anchor}`
    const section = layer === 'repo' && anchor === ANCHORS.data ? PROJECT_ANCHORS.repo : IN_PROJECT[anchor]
    return section ? `${rulesHref(scope.projectId)}#${section}` : rulesHref(scope.projectId)
  }
  if (rule.startsWith('recognizer:')) {
    return (
      <>
        <Link to={where(ANCHORS.recognizers)} className="link font-sans" title={rule}>
          {t('rules.recognizers.ruleLabel', { name: rule.slice('recognizer:'.length) })}
        </Link>
        <LayerNote layer={layer} />
      </>
    )
  }
  const field = strictField(rule) ?? ruleField(rule)
  if (field !== null) {
    return (
      <>
        <Link to={where(ANCHORS.data)} className="link font-sans" title={rule}>
          {rule.startsWith('strict:') ? t('data.ruleStrict', { field }) : t('data.ruleField', { field })}
        </Link>
        <LayerNote layer={layer} />
      </>
    )
  }
  if (rule.startsWith('terms:')) {
    return (
      <>
        <Link to={where(ANCHORS.terms)} className="link mono break-all" title={rule}>
          {rule}
        </Link>
        <LayerNote layer={layer} />
      </>
    )
  }
  return (
    <a href={reference} target="_blank" rel="noreferrer" className="link mono break-all" title={rule}>
      {rule}
    </a>
  )
}
