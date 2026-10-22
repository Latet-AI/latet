import { useTranslation } from 'react-i18next'
import type { ExchangeDetail } from '../../api/types'
import { isBlocked } from '../../lib/flags'
import { useDeploymentKey } from '../../lib/me'

interface Step {
  id: string
  label: string
  detail: string
  state: 'normal' | 'stopped' | 'off'
}

/**
 * The path of one exchange, left to right: what you had, what was sent, what the model wrote, what
 * your agent got. Each step links to its section; a blocked request stops at the second step.
 */
export function FlowStrip({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { summary, response } = detail
  const blocked = isBlocked(summary.flags)
  const plugin = summary.transport === 'plugin'
  const passthrough = summary.mode === 'passthrough'
  const steps: Step[] = [
    {
      id: 'request',
      label: t('exchange.flow.original'),
      detail: t(key('exchange.flow.blocks'), { count: detail.request.slots.length }),
      state: 'normal',
    },
    {
      id: 'request',
      label: blocked ? t('exchange.flow.notSent') : t('exchange.flow.sent'),
      detail: plugin
        ? t('exchange.flow.pluginDetail')
        : blocked
          ? t('exchange.flow.blockedDetail')
          : passthrough
          ? t('exchange.flow.passthroughDetail')
          : t('exchange.flow.masked', { count: summary.masked }),
      state: blocked ? 'stopped' : 'normal',
    },
    {
      id: 'response',
      label: t('exchange.flow.model'),
      detail: response ? (summary.model ?? summary.upstream) : t('exchange.flow.noResponse'),
      state: response && !blocked ? 'normal' : 'off',
    },
    {
      id: 'response',
      label: t('exchange.flow.agent'),
      detail: response ? t('exchange.flow.restored', { count: summary.restored }) : t('exchange.flow.noResponse'),
      state: response && !blocked ? 'normal' : 'off',
    },
  ]
  return (
    <nav aria-label={t(key('exchange.flow.label'))} className="mb-6">
      <ol className="grid grid-cols-4 gap-0 max-md:grid-cols-2 max-md:gap-2">
        {steps.map((step, i) => (
          <li key={i} className="relative flex min-w-0 items-center">
            <a
              href={`#${step.id}`}
              className={`block min-w-0 flex-1 rounded-md border px-3 py-2 ${
                step.state === 'stopped'
                  ? 'border-crit/60 bg-crit/8'
                  : step.state === 'off'
                    ? 'border-dashed border-line-2 bg-transparent'
                    : 'border-line bg-surface hover:border-line-2'
              }`}
            >
              <span className={`block truncate text-ui font-medium ${step.state === 'off' ? 'text-ink-3' : 'text-ink'}`}>{step.label}</span>
              <span className={`block truncate text-meta ${step.state === 'stopped' ? 'text-crit-ink' : 'text-ink-3'}`}>{step.detail}</span>
            </a>
            {i < steps.length - 1 && (
              <span
                className={`h-px w-5 flex-none max-md:hidden ${steps[i + 1]?.state === 'off' || step.state === 'stopped' ? 'bg-line-2' : 'bg-ink-3'}`}
                aria-hidden="true"
              />
            )}
          </li>
        ))}
      </ol>
    </nav>
  )
}
