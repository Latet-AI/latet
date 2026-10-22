// What a step shows in Latet's pane: the dashboard's own components, on the sandbox's data. An exchange
// as the exchange page draws it (why it was stopped, its blocks or its answer, what the local reviewer
// found in them, its image as it was and as it was sent), the thing to look at pulsing; or the component
// the step asks you to use (Terms, Your guidance, the Live Reviewer's queue, the change waiting, the
// columns of strict data mode), outlined while it waits for you.

import { useEffect, useId, useMemo, useRef, useState, type ReactNode, type SyntheticEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { ExchangeDetail, TutorialShown } from '../../api/types'
import { isBlocked } from '../../lib/flags'
import { useApprovals } from '../../lib/approvals-context'
import { pinMatches, targetOf, WHY_ID } from '../../lib/tutorial'
import { useAsync } from '../../lib/useAsync'
import { ApprovalsPage } from '../../pages/ApprovalsPage'
import { Verdict } from '../../pages/ExchangePage'
import { ExchangeColumns } from '../exchange/ColumnsPanel'
import { ResponseView } from '../exchange/ResponseView'
import { SlotView } from '../exchange/SlotView'
import { Icon } from '../Icon'
import { InspectorProvider } from '../masked/Inspector'
import { knownValues, requestMarks } from '../masked/marks'
import { ReviewPanel } from '../ReviewPanel'
import { PendingChanges } from '../SettingsChange'
import { CommandLine, InlineError, Loading } from '../ui'
import { bringIntoView, usePulse } from './usePulse'
import { TutorialGuidance } from './TutorialGuidance'
import { TutorialTerms } from './TutorialTerms'

interface ShownPartProps {
  shown: TutorialShown
  /** The step on screen is the one that shows this: what it points at pulses. */
  pulse: boolean
  /** The step waits for you to use this component: it is outlined and pulses. */
  acting: boolean
  /** The value the step asks you to open was opened (click, or Enter on it). */
  onPinned: () => void
}

export function ShownPart({ shown, pulse, acting, onPinned }: ShownPartProps) {
  switch (shown.component) {
    case 'exchange':
    case 'why':
    case 'review':
    case 'images':
      return shown.exchange ? <ShownExchange key={shown.exchange} id={shown.exchange} shown={shown} pulse={pulse} onPinned={onPinned} /> : null
    case 'terms':
      return (
        <Act on={acting}>
          <TutorialTerms />
        </Act>
      )
    case 'guidance':
      return (
        <Act on={acting}>
          <TutorialGuidance text={shown.text ?? ''} />
        </Act>
      )
    case 'approvals':
      return (
        <Act on={acting}>
          <ApprovalsPage embedded />
        </Act>
      )
    case 'changes':
      return (
        <Act on={acting}>
          <Changes />
        </Act>
      )
    case 'columns':
      return shown.exchange ? (
        <Act on={acting}>
          <Columns id={shown.exchange} slot={shown.slot} />
        </Act>
      ) : null
    case 'command':
      return <Command shown={shown} />
    case 'none':
      return null
  }
}

/**
 * A component to use: while the step waits for you, the outline pulses around it (`usePulse`'s attribute), and it
 * comes into view if it is out of it, as what `usePulse` points at does.
 */
function Act({ on, children }: { on: boolean; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (on && box.current) bringIntoView(box.current)
  }, [on])
  return (
    <div ref={box} className="rounded-lg" data-tutorial-pulse={on ? '' : undefined}>
      {children}
    </div>
  )
}

function ShownExchange({ id, shown, pulse, onPinned }: { id: string; shown: TutorialShown; pulse: boolean; onPinned: () => void }) {
  const { t } = useTranslation()
  const exchange = useAsync(`exchange:${id}`, (signal) => api.exchange(id, signal))
  const detail = exchange.data?.summary.id === id ? exchange.data : undefined
  const target = useMemo(() => (detail ? targetOf(detail, shown) : null), [detail, shown])
  const root = useRef<HTMLDivElement>(null)
  usePulse(root, pulse ? target : null)

  if (exchange.error && !detail) {
    return (
      <div className="panel flex flex-wrap items-center gap-3 p-4">
        <InlineError error={exchange.error} />
        <button type="button" className="btn btn-sm" onClick={exchange.reload}>
          <Icon name="refresh" size={13} />
          {t('common.retry')}
        </button>
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="panel">
        <Loading />
      </div>
    )
  }
  return (
    // The pane's own inspector: opening the value a step asks for is how that step is done.
    <InspectorProvider
      onPin={(info) => {
        if (target !== null && pinMatches(info, target)) onPinned()
      }}
    >
      <div ref={root} className="space-y-3">
        <div id={shown.component === 'why' ? WHY_ID : undefined} className="rounded-lg">
          <Verdict detail={detail} />
        </div>
        {shown.part === 'response' ? (
          <ResponsePart detail={detail} />
        ) : (
          <>
            {shown.component === 'images' && <Pictures detail={detail} original={shown.original} />}
            <RequestPart detail={detail} slot={shown.slot} />
            {shown.component === 'review' && <Reviewed detail={detail} slot={shown.slot} />}
          </>
        )}
      </div>
    </InspectorProvider>
  )
}

/** What the pictures reserve until the original has loaded and says its own size: the chapter's screenshot is 1100 × 420. */
const SCREENSHOT_RATIO = 1100 / 420

/**
 * The image as it is on this computer, then as it was sent, one above the other and as wide as the pane, so that the
 * text in them can be read and compared; the text read in it is the block below. Each keeps its height while it loads
 * (the original's ratio, which the sent one shares: covering a value does not resize the image), so that what pulses
 * below does not move when they arrive.
 */
function Pictures({ detail, original }: { detail: ExchangeDetail; original: string | undefined }) {
  const { t } = useTranslation()
  const [ratio, setRatio] = useState(SCREENSHOT_RATIO)
  // The sent image whose bytes are still kept (an image pruned with the history is not there to show).
  const sent = (detail.request.images ?? []).find((image) => image.id !== null && image.kept !== false)
  const measure = (event: SyntheticEvent<HTMLImageElement>) => {
    const { naturalWidth, naturalHeight } = event.currentTarget
    if (naturalWidth > 0 && naturalHeight > 0) setRatio(naturalWidth / naturalHeight)
  }
  const picture = 'block w-full bg-well object-contain' // alt is empty: each figure's caption names it
  return (
    <div className="space-y-3">
      <figure className="panel overflow-hidden">
        {original && <img src={api.tutorial.picture(original)} alt="" onLoad={measure} style={{ aspectRatio: ratio }} className={picture} />}
        <figcaption className="px-3 py-2 text-meta text-ink-2">{t('tutorial.shown.imageBefore')}</figcaption>
      </figure>
      <figure className="panel overflow-hidden">
        {sent && <img src={api.imageUrl(sent.id as string)} alt="" style={{ aspectRatio: ratio }} className={picture} />}
        <figcaption className="px-3 py-2 text-meta text-ink-2">{t('tutorial.shown.imageSent')}</figcaption>
      </figure>
    </div>
  )
}

/** The request's block the step names (or its new blocks), the original and what was sent. */
function RequestPart({ detail, slot }: { detail: ExchangeDetail; slot: number | undefined }) {
  const { t } = useTranslation()
  const marks = useMemo(() => requestMarks(detail), [detail])
  const { slots } = detail.request
  const named = slot !== undefined && slots[slot] !== undefined
  const fresh = slots.flatMap((block, i) => (block.new ? [i] : []))
  const shown = named ? [slot] : fresh.length > 0 ? fresh : slots.map((_, i) => i)
  return (
    <>
      <p className="text-meta text-ink-3">
        {named ? t('tutorial.shown.block', { number: slot + 1, count: slots.length }) : t('tutorial.shown.blocks', { count: shown.length, total: slots.length })}
      </p>
      {shown.map((i) => (
        <SlotView
          key={i}
          slot={slots[i]!}
          index={i}
          marks={marks[i]!}
          footnotes
          blocked={isBlocked(detail.summary.flags)}
          exchangeId={detail.summary.id}
          agent={detail.summary.agent}
        />
      ))}
    </>
  )
}

/**
 * What the local reviewer said of the block shown: here the Tutorial Reviewer, simulated, as the chapter writes it.
 * The panel lists that block's reviews (every block's when the step names none) and numbers blocks as the request does.
 */
function Reviewed({ detail, slot }: { detail: ExchangeDetail; slot: number | undefined }) {
  const { t } = useTranslation()
  const { slots } = detail.request
  return (
    <>
      <ReviewPanel session={detail.summary.session} slots={slots} only={slot === undefined ? undefined : slots[slot]} />
      <p className="text-meta text-ink-3">{t('tutorial.shown.simulated')}</p>
    </>
  )
}

/** The answer: as the model wrote it (fakes), and what the agent got (the real values put back). */
function ResponsePart({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  const known = useMemo(() => knownValues(requestMarks(detail)), [detail])
  if (!detail.response) return <div className="panel px-4 py-3 text-ui text-ink-2">{t('exchange.response.none')}</div>
  return (
    <>
      <p className="text-meta text-ink-3">{t('tutorial.shown.response')}</p>
      <ResponseView response={detail.response} known={known} footnotes mode={detail.summary.mode} />
    </>
  )
}

/** The change to the settings waiting for you, or, once decided, a line that says nothing waits. */
function Changes() {
  const { t } = useTranslation()
  const { changes } = useApprovals()
  if (changes.length === 0) return <p className="panel px-4 py-3 text-ui text-ink-3">{t('tutorial.shown.noChange')}</p>
  return <PendingChanges />
}

function Columns({ id, slot }: { id: string; slot: number | undefined }) {
  const { t } = useTranslation()
  const titleId = useId()
  return (
    <section className="panel p-3" aria-labelledby={titleId}>
      <h3 id={titleId} className="mb-2 text-ui font-semibold text-ink">
        {t('exchange.columns.show')}
      </h3>
      <ExchangeColumns exchangeId={id} slot={slot} />
    </section>
  )
}

function Command({ shown }: { shown: TutorialShown }) {
  return (
    <div className="panel space-y-2 p-3">
      {shown.command && <CommandLine command={shown.command} />}
      {shown.snippet && <pre className="well code px-3 py-2 text-ink">{shown.snippet}</pre>}
      {shown.rules && <pre className="well code px-3 py-2 text-ink">{shown.rules}</pre>}
    </div>
  )
}
