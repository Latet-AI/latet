// Strict data mode is set in Rules, never from an exchange. While it is off for an exchange, a tool output of its
// request that holds structure strict data mode would mask (a table, JSON, key=value pairs, as the daemon reads them)
// gets one line saying so, with a link to where the mode is set for that exchange.

import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { ProjectDetail, Slot } from '../../api/types'
import { holdsStructure, strictLayer, strictOn } from '../../lib/columns'
import { useAsync } from '../../lib/useAsync'
import { DocsWhyLink } from '../DocsLinks'
import { Icon } from '../Icon'
import { ANCHORS, PROJECT_ANCHORS } from '../rules'
import { rulesHref } from '../ScopeBar'

export function StrictNotice({ exchangeId, slots, projectId }: { exchangeId: string; slots: readonly Slot[]; projectId: string | null }) {
  const config = useAsync('strict:config', (signal) => api.config(signal))
  const project = useAsync(projectId ? `strict:project:${projectId}` : null, (signal) => api.project(projectId ?? '', signal))
  // A project Latet has not loaded since it started answers 404: the global rules decide until then. The previous
  // exchange's project, kept while another loads, does not decide for this one.
  const detail = !projectId ? null : project.stale ? undefined : project.error ? null : project.data
  const strict = strictOn(config.data?.data_effective, detail)
  // The daemon reads the columns from the texts the history keeps: asked only with `history: full`, and only while
  // strict data mode is off (on, it masks them already).
  const ask = strict === false && config.data?.history === 'full' && slots.some((slot) => slot.kind === 'tool_result')
  const structure = useAsync(ask ? `strict:structure:${exchangeId}` : null, (signal) =>
    holdsStructure(slots, (slot) => api.columnShape(exchangeId, slot, signal)),
  )
  if (!ask || structure.stale || structure.data !== true || detail === undefined) return null
  return <StrictLine project={detail} />
}

/** The line, leading to where strict data mode is set for the exchange: its project's rules or the global ones. */
export function StrictLine({ project }: { project: ProjectDetail | null }) {
  const { t } = useTranslation()
  const to = project && strictLayer(project) === 'project' ? `${rulesHref(project.id)}#${PROJECT_ANCHORS.projectData}` : `/rules#${ANCHORS.data}`
  return (
    <p role="note" data-strict-notice className="mb-3 flex max-w-[100ch] items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-ui text-ink-2">
      <Icon name="info" size={15} className="mt-0.5 flex-none" />
      <span>
        {t('exchange.strict.notice')}{' '}
        <Link to={to} className="link">
          {t('exchange.strict.open')}
        </Link>{' '}
        <DocsWhyLink target="why.strict" />
      </span>
    </p>
  )
}
