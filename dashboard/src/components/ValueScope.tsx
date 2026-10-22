// Where "Always mask" and "Not sensitive" apply: the exchange's project, or every project.

import { useContext, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { withValue, type ValueChoice } from '../lib/projects'
import { RuleScope } from './RuleName'
import { Segmented } from './ui'

export type Where = 'project' | 'everywhere'

/** The choice, the project first. `projectId` is the project a choice applies to, null for everywhere. */
export function useWhere() {
  const scope = useContext(RuleScope)
  const [where, setWhere] = useState<Where>('project')
  const projectId = scope && where === 'project' ? scope.projectId : null
  return { hasProject: scope !== null, where, setWhere, projectId }
}

export function WhereChoice({ where, onChange }: { where: Where; onChange: (where: Where) => void }) {
  const { t } = useTranslation()
  return (
    <Segmented<Where>
      size="sm"
      label={t('valueScope.label')}
      value={where}
      onChange={onChange}
      options={[
        { value: 'project', label: t('valueScope.project') },
        { value: 'everywhere', label: t('valueScope.everywhere') },
      ]}
    />
  )
}

/** Adds the value to your own settings for the project, or to the global ones. */
export async function chooseValue(choice: ValueChoice, projectId: string | null): Promise<void> {
  if (projectId) {
    await api.changePersonal(projectId, (personal) => withValue(personal, choice))
  } else if (choice.kind === 'mask') {
    await api.addTerm(choice.value, choice.type)
  } else {
    await api.allow(choice.value)
  }
}
