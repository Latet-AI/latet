import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { Agent, Slot } from '../../api/types'
import { blockDomId } from '../../lib/blocks'
import { hasStrictValues } from '../../lib/columns'
import { CLASS_LABEL, KIND_LABEL, MODULE_LABEL } from '../../lib/labels'
import { useDeploymentKey } from '../../lib/me'
import { ClassSwatch } from '../badges'
import { RuleName } from '../RuleName'
import { Icon } from '../Icon'
import { ColumnsPanel } from './ColumnsPanel'
import type { BlockMarks } from '../masked/marks'
import { MaskedText } from '../masked/MaskedText'
import type { SpanInfo } from '../masked/types'

interface SlotViewProps {
  slot: Slot
  index: number
  marks: BlockMarks
  footnotes: boolean
  /** Show only the sent side (Sessions timeline). */
  sentOnly?: boolean
  compact?: boolean
  /** The request was blocked: the right pane shows what would have been sent. */
  blocked?: boolean
  /** The exchange's id and agent: a tool's output strict data mode masked shows its columns (exchange view). */
  exchangeId?: string
  agent?: Agent
}

/** One block of the request: the original on this machine next to exactly what was sent. */
export function SlotView({ slot, index, marks, footnotes, sentOnly = false, compact = false, blocked = false, exchangeId, agent }: SlotViewProps) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const titleId = useId()
  const kind = KIND_LABEL[slot.kind] ? t(key(KIND_LABEL[slot.kind])) : slot.kind
  const count = slot.replacements.length
  const unknownOriginal = slot.original === null
  const sentLabel = blocked ? t('exchange.panes.wouldSend') : t('exchange.panes.sent')
  const collapseAfter = compact ? 14 : 40
  const previewLines = compact ? 8 : 24

  return (
    // A container: the two panes stack when the block is narrow (the tutorial's pane), not when the window is. 640px is a bit
    // under the width a page has in a 768px window (md, where the panes used to stack): a page looks as it did from there up.
    <article id={blockDomId(index)} className="panel @container scroll-mt-4 overflow-hidden" aria-labelledby={titleId}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2">
        <h3 id={titleId} className="flex items-center gap-2 text-ui font-semibold text-ink">
          <span className="text-meta font-normal text-ink-3 tabular-nums">{t('exchange.block.number', { number: index + 1 })}</span>
          {kind}
        </h3>
        <code className="mono min-w-0 flex-1 truncate text-meta text-ink-3" title={slot.path}>
          {slot.path}
        </code>
        <span className="flex flex-wrap items-center gap-1.5">
          {slot.from_model && (
            <span className="tag" title={t('exchange.block.fromModelHelp')}>
              {t('exchange.block.fromModel')}
            </span>
          )}
          {slot.new ? (
            <span className="tag border-brand/60 bg-brand/10 text-ink" title={t('exchange.block.newHelp')}>
              {t('exchange.block.new')}
            </span>
          ) : (
            <span className="tag" title={t('exchange.block.resentHelp')}>
              {t('exchange.block.resent')}
            </span>
          )}
          <span className="text-meta text-ink-2 tabular-nums">
            {count === 0 ? t('exchange.block.noValues') : t('exchange.block.values', { count })}
          </span>
        </span>
      </header>

      {sentOnly ? (
        <MaskedText
          text={slot.masked}
          marks={marks.sent}
          footnotes={footnotes}
          collapseAfter={collapseAfter}
          previewLines={previewLines}
          label={sentLabel}
          className="bg-well"
          tone="fake"
        />
      ) : (
        <div className="grid grid-cols-2 @max-[640px]:grid-cols-1">
          <div className="min-w-0 border-r border-line @max-[640px]:border-r-0 @max-[640px]:border-b">
            <p className="flex items-center gap-1.5 border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">
              <Icon name="vault" size={12} className="text-real" />
              {t(key('exchange.panes.original'))}
            </p>
            {unknownOriginal ? (
              <div className="hatch h-full min-h-[72px] px-3 py-3 text-ui text-ink-2">
                <p className="max-w-[46ch] rounded-sm bg-well/90 px-2 py-1.5">{t('exchange.panes.originalUnknown')}</p>
              </div>
            ) : (
              <MaskedText
                text={slot.original ?? ''}
                marks={marks.original ?? []}
                footnotes={footnotes}
                collapseAfter={collapseAfter}
                previewLines={previewLines}
                label={t(key('exchange.panes.original'))}
                className="bg-well"
                tone="real"
              />
            )}
          </div>
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">
              <Icon name="external" size={12} className="text-fake" />
              {sentLabel}
            </p>
            <MaskedText
              text={slot.masked}
              marks={marks.sent}
              footnotes={footnotes}
              collapseAfter={collapseAfter}
              previewLines={previewLines}
              label={sentLabel}
              className="bg-well"
              tone="fake"
            />
          </div>
        </div>
      )}

      {count > 0 && !compact && <ValuesList values={marks.values} />}
      {exchangeId && agent && !compact && !sentOnly && hasStrictValues(slot) && (
        <ColumnsPanel exchangeId={exchangeId} slot={index} agent={agent} />
      )}
    </article>
  )
}

/** The values of a block as a table: the same facts as the highlights, without hovering. */
export function ValuesList({ values }: { values: readonly SpanInfo[] }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className="border-t border-line">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-meta text-ink-2 hover:bg-wash"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name={open ? 'chevronDown' : 'chevronRight'} size={12} />
        {open ? t('exchange.values.hide') : t('exchange.values.show', { count: values.length })}
        {!open && (
          <span className="ml-1 flex min-w-0 flex-wrap gap-1">
            {[...new Set(values.map((v) => v.type))].slice(0, 6).map((type) => (
              <span key={type} className="mono text-micro text-ink-3">
                {type}
              </span>
            ))}
          </span>
        )}
      </button>
      {open && (
        <div id={id} className="overflow-x-auto">
          <table className="data-table text-meta">
            <thead>
              <tr>
                <th scope="col" className="num w-8">
                  {t('exchange.values.number')}
                </th>
                <th scope="col">{t('exchange.values.type')}</th>
                <th scope="col">{t('exchange.values.class')}</th>
                <th scope="col">{t('exchange.values.sent')}</th>
                <th scope="col">{t('exchange.values.module')}</th>
                <th scope="col">{t('exchange.values.rule')}</th>
                <th scope="col">{t('exchange.values.entity')}</th>
              </tr>
            </thead>
            <tbody>
              {values.map((value) => (
                <tr key={value.pair}>
                  <td className="num text-ink-3">{value.footnote}</td>
                  <td className="mono text-ink">{value.type}</td>
                  <td>
                    <span className="inline-flex items-center gap-1.5 text-ink-2">
                      <ClassSwatch cls={value.cls} />
                      {t(CLASS_LABEL[value.cls])}
                    </span>
                  </td>
                  <td className="mono max-w-[320px] break-all text-ink">
                    {value.sent}
                    {!value.known && <span className="ml-1.5 font-sans text-ink-3">{value.redacted ? t('exchange.values.redacted') : t('exchange.values.gone')}</span>}
                  </td>
                  <td className="text-ink-2">{value.module ? t(MODULE_LABEL[value.module]) : t('common.none')}</td>
                  <td>
                    <RuleName rule={value.rule} />
                  </td>
                  <td className="mono text-ink-3">{value.entity ?? t('common.none')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
