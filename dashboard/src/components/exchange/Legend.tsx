import { useTranslation } from 'react-i18next'
import { CLASS_HELP, CLASS_LABEL, CLASS_TONE, isJudgement, type ViewClass } from '../../lib/labels'
import { useDeploymentKey } from '../../lib/me'
import { Tooltip } from '../Floating'
import type { SpanInfo } from '../masked/types'

const ORDER: ViewClass[] = ['SECRET', 'PII', 'IDENTITY', 'CUSTOM', 'LLM', 'NER', 'STRUCT', 'UNKNOWN']

/** What the highlight styles mean, for the classes and cases present in a view (`stopped`: values that stopped the request). */
export function ClassLegend({ values, stopped = false }: { values: readonly SpanInfo[]; stopped?: boolean }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const classes = new Set(values.map((v) => v.cls))
  const redacted = values.some((v) => v.redacted)
  const gone = values.some((v) => !v.known && !v.redacted)
  if (classes.size === 0 && !stopped) return null
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-meta text-ink-2" aria-label={t('legend.label')}>
      {ORDER.filter((cls) => classes.has(cls)).map((cls) => (
        <li key={cls}>
          <Tooltip content={t(key(CLASS_HELP[cls]))}>
            <span className="inline-flex items-center gap-1.5">
              <span className="mask code px-1 text-micro" data-tone={CLASS_TONE[cls]} data-dashed={isJudgement(cls) ? '' : undefined}>
                {t('legend.sample')}
              </span>
              {t(CLASS_LABEL[cls])}
            </span>
          </Tooltip>
        </li>
      ))}
      {stopped && (
        <li>
          <Tooltip content={t(key('legend.stoppedHelp'))}>
            <span className="inline-flex items-center gap-1.5">
              <span className="mask mask-stopped code px-1 text-micro" data-tone="crit">
                {t('legend.sample')}
              </span>
              {t('legend.stopped')}
            </span>
          </Tooltip>
        </li>
      )}
      {redacted && (
        <li>
          <Tooltip content={t('legend.redactedHelp')}>
            <span className="inline-flex items-center gap-1.5">
              <span className="mask mask-redacted code px-1 text-micro" data-tone="neutral">
                {t('legend.sample')}
              </span>
              {t('legend.redacted')}
            </span>
          </Tooltip>
        </li>
      )}
      {gone && (
        <li>
          <Tooltip content={t('legend.goneHelp')}>
            <span className="inline-flex items-center gap-1.5">
              <span className="mask mask-gone code px-1 text-micro" data-tone="neutral">
                {t('legend.sample')}
              </span>
              {t('legend.gone')}
            </span>
          </Tooltip>
        </li>
      )}
    </ul>
  )
}
