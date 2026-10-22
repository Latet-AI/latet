import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import type { ExchangeResponse, Mode, Replacement } from '../../api/types'
import { RESPONSE_TEXT_ID, responseToolId } from '../../lib/blocks'
import { useDeploymentKey } from '../../lib/me'
import { Icon } from '../Icon'
import { knownValues, pairMarks, RESPONSE_TEXT_SCOPE, toolScope } from '../masked/marks'
import { MaskedText } from '../masked/MaskedText'

// The scopes of the response's marks (`response`, `tool0`...): a page that points at a value in it needs them.
export { RESPONSE_TEXT_SCOPE, toolScope }

interface ResponseViewProps {
  response: ExchangeResponse
  known: ReturnType<typeof knownValues>
  footnotes: boolean
  mode: Mode
}

function Pair({
  masked,
  restored,
  spans,
  known,
  scope,
  footnotes,
  leftLabel,
  rightLabel,
  emptyLabel,
}: {
  masked: string
  restored: string
  spans: readonly Replacement[] | undefined
  known: ReturnType<typeof knownValues>
  scope: string
  footnotes: boolean
  leftLabel: string
  rightLabel: string
  emptyLabel: string
}) {
  const { t } = useTranslation()
  const marks = useMemo(() => pairMarks(masked, restored, spans, known, scope), [masked, restored, spans, known, scope])
  return (
    <>
      <div className="grid grid-cols-2 @max-[640px]:grid-cols-1">
        <div className="min-w-0 border-r border-line @max-[640px]:border-r-0 @max-[640px]:border-b">
          <p className="border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">{leftLabel}</p>
          <MaskedText text={masked} marks={marks.model} footnotes={footnotes} label={leftLabel} className="bg-well" empty={emptyLabel} />
        </div>
        <div className="min-w-0">
          <p className="border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">{rightLabel}</p>
          <MaskedText text={restored} marks={marks.agent} footnotes={footnotes} label={rightLabel} className="bg-well" empty={emptyLabel} />
        </div>
      </div>
      {!marks.aligned && marks.model.length > 0 && (
        <p className="border-t border-line px-3 py-1.5 text-meta text-ink-3">{t('exchange.response.notAligned')}</p>
      )}
    </>
  )
}

/** The answer as the model wrote it (surrogates) next to what the agent received (originals). */
export function ResponseView({ response, known, footnotes, mode }: ResponseViewProps) {
  const { t } = useTranslation()
  const deploymentKey = useDeploymentKey()
  const empty = response.text_masked === '' && response.tool_calls.length === 0
  return (
    <div className="space-y-3">
      {mode === 'verify' && <p className="max-w-[90ch] text-ui text-ink-2">{t(deploymentKey('exchange.response.verifyNote'))}</p>}
      {empty ? (
        <div className="panel px-4 py-5 text-ui text-ink-3">{t('exchange.response.empty')}</div>
      ) : (
        <>
          {response.text_masked !== '' && (
            <article id={RESPONSE_TEXT_ID} className="panel @container overflow-hidden">
              <header className="border-b border-line px-3 py-2">
                <h3 className="text-ui font-semibold text-ink">{t('exchange.response.text')}</h3>
              </header>
              <Pair
                masked={response.text_masked}
                restored={response.text_restored}
                spans={response.text_spans}
                known={known}
                scope={RESPONSE_TEXT_SCOPE}
                footnotes={footnotes}
                leftLabel={t('exchange.panes.model')}
                rightLabel={t('exchange.panes.agent')}
                emptyLabel={t('exchange.response.noText')}
              />
            </article>
          )}
          {response.tool_calls.map((call, i) => (
            <article key={i} id={responseToolId(i)} className="panel @container overflow-hidden">
              <header className="flex items-center gap-2 border-b border-line px-3 py-2">
                <Icon name="terminal" size={14} className="text-ink-3" />
                <h3 className="text-ui font-semibold text-ink">
                  {t('exchange.response.toolCall')} <code className="mono font-normal">{call.name}</code>
                </h3>
              </header>
              <Pair
                masked={call.args_masked}
                restored={call.args_restored}
                spans={call.spans}
                known={known}
                scope={toolScope(i)}
                footnotes={footnotes}
                leftLabel={t('exchange.panes.modelArgs')}
                rightLabel={t('exchange.panes.agentArgs')}
                emptyLabel={t('exchange.response.noArgs')}
              />
            </article>
          ))}
        </>
      )}
    </div>
  )
}
