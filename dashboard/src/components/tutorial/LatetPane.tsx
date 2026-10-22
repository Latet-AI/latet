// Latet's pane: what the last step did, in plain words, then the real dashboard component that shows it,
// the thing to look at pulsing. A step that asks you to do something says what, here. "Find it later"
// opens the same thing in the normal dashboard (the bar on top of every page leads back). When its text
// changes, it lights up, so the eye goes there.

import { useEffect, useId, useRef, type RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { TutorialChapter, TutorialState, TutorialWaitKind } from '../../api/types'
import { tk } from '../../i18n'
import { errorAt, explainKey, flashes, laterPath, triesAgain, visibleResult, type Phase, type View } from '../../lib/tutorial'
import { Icon, LogoMark } from '../Icon'
import { InlineCodeText } from '../ui'
import { ShownPart } from './ShownPart'
import { bringIntoView } from './usePulse'

interface LatetPaneProps {
  chapter: TutorialChapter
  state: TutorialState
  view: View
  phase: Phase
  /** The value the step asks you to open was opened. */
  onPinned: () => void
}

export function LatetPane({ chapter, state, view, phase, onPinned }: LatetPaneProps) {
  const { t } = useTranslation()
  const titleId = useId()
  // What the pane shows: the step on screen's own result, or the last one before it (a `do` step has its
  // own before it runs: what it asks you to look at).
  const result = visibleResult(state, view.step)
  const own = result !== null && result.index === view.step
  const shownStep = result ? chapter.steps[result.index] : undefined
  const wait = chapter.steps[view.step]?.wait ?? null
  const later = result?.shown ? laterPath(result.shown) : null
  // The daemon could not run the step on screen, and nothing is out: the agent's pane says so and offers to try again.
  const stalled = errorAt(state, view.step) !== null && !state.busy && !view.sending
  const card = useRef<HTMLDivElement>(null)
  useFlash(card, explainKey(result))

  return (
    <section aria-labelledby={titleId} className="min-w-0 space-y-3">
      <div ref={card} className="panel px-4 py-3.5">
        <p className="flex items-center gap-1.5 text-meta text-ink-3">
          <LogoMark size={14} />
          <span className="font-medium text-ink-2">{t('tutorial.latet.label')}</span>
          {result && <span>· {t('tutorial.chapter.step', { number: result.index + 1, count: chapter.steps.length })}</span>}
        </p>
        <h2 id={titleId} className="mt-1.5 text-title font-semibold text-ink">
          {shownStep?.title ?? chapter.title}
        </h2>
        {result === null ? (
          <Intro chapter={chapter} />
        ) : (
          result.explain !== '' && (
            <p className="mt-1.5 max-w-[80ch] text-body text-ink-2">
              <InlineCodeText text={result.not_sent ? t('tutorial.latet.notSent') : result.explain} />
            </p>
          )
        )}
        {phase === 'waiting' && wait && <YourTurn hint={wait.hint} kind={wait.kind} />}
        {phase === 'running' && !stalled && (
          <p role="status" className="mt-3 flex items-center gap-2 text-ui text-ink-2">
            <span className="live-dot size-1.5 flex-none rounded-full bg-brand" aria-hidden="true" />
            {triesAgain(chapter, view) ? t('tutorial.latet.again', { agent: chapter.agent.name }) : t('tutorial.latet.running')}
          </p>
        )}
        {later && (
          <Link to={later} className="link mt-3 inline-flex items-center gap-1 text-meta" title={t('tutorial.latet.laterHelp')}>
            {t('tutorial.latet.later')}
            <Icon name="external" size={12} />
          </Link>
        )}
      </div>
      {result?.shown && <ShownPart shown={result.shown} pulse={own} acting={own && phase === 'waiting'} onPinned={onPinned} />}
    </section>
  )
}

const FLASH = 'tutorial-flash'

/**
 * Lights the card up when its text changes (`flashes`), and brings it into view if it is out of it. index.css fades
 * the light out; under reduced motion it keeps a still border instead, until the text changes again.
 */
function useFlash(card: RefObject<HTMLElement | null>, key: string | null): void {
  const seen = useRef<string | null | undefined>(undefined) // undefined: the page just opened
  useEffect(() => {
    const element = card.current
    const before = seen.current
    seen.current = key
    if (!element) return
    if (key === null) element.classList.remove(FLASH) // back at the intro
    if (!flashes(before, key)) return
    element.classList.remove(FLASH)
    element.getBoundingClientRect() // a card lit already lights up again: the animation starts over
    element.classList.add(FLASH)
    bringIntoView(element)
  }, [card, key])
}

/** Before the first step: what the chapter is about, and how the two panes work. */
function Intro({ chapter }: { chapter: TutorialChapter }) {
  const { t } = useTranslation()
  return (
    <>
      <p className="mt-1.5 max-w-[80ch] text-body text-ink-2">{t('tutorial.latet.intro', { agent: chapter.agent.name })}</p>
      <p className="mt-3 text-meta font-medium text-ink-2">{t('tutorial.latet.learn')}</p>
      <ul className="mt-1 space-y-1 text-ui text-ink">
        {chapter.learn.map((line, i) => (
          <li key={i} className="flex items-start gap-2">
            <Icon name="check" size={14} className="mt-[3px] flex-none text-ink-3" />
            {line}
          </li>
        ))}
      </ul>
    </>
  )
}

/** What each kind of wait asks, when the chapter gives no words of its own. */
const HINT: Record<TutorialWaitKind, string> = {
  pin: tk('tutorial.latet.hint.pin'),
  term: tk('tutorial.latet.hint.term'),
  allow: tk('tutorial.latet.hint.allow'),
  approve: tk('tutorial.latet.hint.approve'),
  change: tk('tutorial.latet.hint.change'),
  fields: tk('tutorial.latet.hint.fields'),
  guidance: tk('tutorial.latet.hint.guidance'),
}

/** What the step waits for you to do, in the dashboard: the chapter's own words. */
function YourTurn({ hint, kind }: { hint: string; kind: TutorialWaitKind }) {
  const { t } = useTranslation()
  return (
    <div role="status" className="mt-3 flex items-start gap-2 rounded-md border border-brand/60 bg-brand/10 px-3 py-2 text-ui text-ink">
      <Icon name="chevronRight" size={15} className="mt-0.5 flex-none text-brand-ink" />
      <span className="min-w-0">
        <strong className="font-semibold">{t('tutorial.latet.yourTurn')}</strong> <InlineCodeText text={hint || t(HINT[kind])} />
        {/* The Approve button sends the request as it is: what is not masked by then leaves in clear. */}
        {kind === 'approve' && <span className="mt-1 block text-meta text-ink-2">{t('tutorial.latet.approveAsIs')}</span>}
      </span>
    </div>
  )
}
