// The agent's pane: a terminal where the agent works. Its lines come from the daemon: your prompt, the
// agent's own lines, its tool calls and what they printed, the files it wrote, and the model's answer as
// the agent got it (the real values put back). The step's button, at the bottom, starts what comes next.
// A step's lines arrive whole, one after the other (at once under reduced motion).

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { Agent, TutorialChapter, TutorialLine, TutorialNote, TutorialState } from '../../api/types'
import { tk } from '../../i18n'
import { errorAt, groupLines, keepsButton, resumedLines, triesAgain, typedPrompt, type Action, type LineGroup, type Phase, type View } from '../../lib/tutorial'
import { AgentIcon } from '../badges'
import { Icon, type IconName } from '../Icon'
import { InlineError } from '../ui'
import { PrimaryButton } from './PrimaryButton'

/** The tutorial's pretend agent, as the dashboard knows it. */
const TUTORIAL_AGENT: Agent = 'tutorial-agent'
/** The system lines the daemon marks, said in the reader's language (their `text` is the English). */
const NOTES: Record<TutorialNote, string> = { resumed: tk('tutorial.agent.resumed'), cleared: tk('tutorial.agent.cleared') }
/** Lines of a tool's output or a file shown before "more". */
const FOLD_AFTER = 12
/** The lines of a step arrive this far apart. */
const LINE_STAGGER_MS = 90

interface AgentPaneProps {
  chapter: TutorialChapter
  state: TutorialState
  view: View
  phase: Phase
  /** What the button does now; null: nothing to press (the agent works, or waits for you). */
  action: Action | null
  /** The sandbox is not running this chapter any more (another tab started another): nothing is at work. */
  stopped: boolean
  onAction: (action: Action) => void
  /** The last call to the daemon that failed, to say so. */
  failure: unknown
}

export function AgentPane({ chapter, state, view, phase, action, stopped, onAction, failure }: AgentPaneProps) {
  const { t } = useTranslation()
  const titleId = useId()
  const log = useRef<HTMLDivElement>(null)
  // The step's prompt stays typed until the daemon puts it in the log, the moment the agent sends it. The conversation
  // the step resumes, from before Latet was on, is above it from the start: the lines the log then holds, in place.
  const typed = typedPrompt(chapter, state, view, phase)
  const resumed = resumedLines(chapter, state, view, phase)
  const lines = useMemo(() => (resumed.length > 0 ? [...state.lines, ...resumed] : state.lines), [state.lines, resumed])
  // The lines there when the page opened were seen already: only the ones after them arrive.
  const [seen] = useState(lines.length)
  const groups = useMemo(() => {
    const placed: (LineGroup & { start: number })[] = []
    for (const group of groupLines(lines)) {
      const before = placed[placed.length - 1]
      placed.push({ ...group, start: before ? before.start + before.lines.length : 0 })
    }
    return placed
  }, [lines])
  const step = chapter.steps[view.step]
  const agent = chapter.agent.name
  // The step's button stays while its step runs, busy: the focus on it is kept.
  const pressed = !stopped && keepsButton(chapter, view, phase, action)
  const shown = pressed ? 'send' : action

  // The newest line in view: on wide screens the pane stays beside Latet's and its log scrolls.
  useEffect(() => {
    const box = log.current
    if (box) box.scrollTop = box.scrollHeight
  }, [lines.length, phase])

  return (
    // Beside Latet's pane it stays in view, and short enough that its button shows below the page's header.
    <section aria-labelledby={titleId} className="panel flex min-w-0 flex-col overflow-hidden lg:sticky lg:top-4 lg:max-h-[calc(100vh-170px)]">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <AgentIcon agent={TUTORIAL_AGENT} />
        <h2 id={titleId} className="text-ui font-semibold whitespace-nowrap text-ink">
          {agent}
        </h2>
        <span className="min-w-0 truncate text-meta text-ink-3">{t('tutorial.agent.model', { model: chapter.agent.model })}</span>
        <span className="tag ml-auto" title={t('tutorial.agent.pretendHelp')}>
          {t('tutorial.agent.pretend')}
        </span>
      </header>

      {/* It scrolls: focusable, so that the keyboard can scroll it. */}
      <div
        ref={log}
        role="log"
        tabIndex={0}
        aria-label={t('tutorial.agent.log', { agent })}
        className="code min-h-[220px] flex-1 space-y-4 overflow-y-auto bg-well px-3 py-3 text-ink focus-visible:-outline-offset-2"
      >
        {groups.length === 0 && typed === null && <p className="text-ink-3">{t('tutorial.agent.idle', { agent })}</p>}
        {groups.map((group) => (
          <div key={group.start} className="space-y-1.5">
            {group.lines.map((line, i) => {
              const at = group.start + i
              const arriving = at >= seen
              return (
                <div
                  key={at}
                  className={arriving ? 'tutorial-line-in' : undefined}
                  style={arriving ? { animationDelay: `${i * LINE_STAGGER_MS}ms` } : undefined}
                >
                  <Line line={line} />
                </div>
              )
            })}
          </div>
        ))}
        {typed !== null && <TypedPrompt text={typed} sending={phase !== 'ready'} />}
      </div>

      <footer className="flex min-h-[50px] flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-line px-3 py-2.5">
        <div className="min-w-0 flex-1 text-meta text-ink-2" role="status">
          <Status chapter={chapter} state={state} view={view} phase={phase} action={action} failure={failure} />
        </div>
        {shown && shown !== 'chapter' && shown !== 'menu' && (
          <PrimaryButton label={actionLabel(t, shown, step?.button ?? null)} icon={ACTION_ICON[shown]} busy={pressed} onClick={() => onAction(shown)} />
        )}
      </footer>
    </section>
  )
}

const ACTION_ICON: Partial<Record<Action, IconName>> = {
  send: 'play',
  next: 'chevronRight',
  finish: 'check',
  retry: 'refresh',
}

function actionLabel(t: (key: string) => string, action: Action, button: string | null): string {
  switch (action) {
    case 'send':
      return button ?? t('tutorial.agent.continue')
    case 'next':
      return t('tutorial.agent.next')
    case 'finish':
      return t('tutorial.agent.finish')
    case 'retry':
      return t('common.retry')
    case 'chapter':
    case 'menu':
      return t('tutorial.agent.next')
  }
}

/** What the agent is doing, in a line beside the button, or why there is no button. */
function Status({ chapter, state, view, phase, action, failure }: Omit<AgentPaneProps, 'onAction' | 'stopped'>) {
  const { t } = useTranslation()
  const agent = chapter.agent.name
  if (failure) return <InlineError error={failure} />
  // The daemon says what went wrong with the step on screen (until the next call): not run, or not as written.
  const error = errorAt(state, view.step)
  if (error !== null && !state.busy && !view.sending) {
    const ran = state.steps[view.step]?.ran === true && action !== 'retry'
    return (
      <p className="flex items-start gap-1.5 text-crit-ink">
        <Icon name="ban" size={14} className="mt-px flex-none" />
        {ran ? t('tutorial.agent.wrong', { error }) : t('tutorial.agent.failed', { error })}
      </p>
    )
  }
  switch (phase) {
    case 'running':
      return (
        <p className="flex items-center gap-2">
          <span className="live-dot size-1.5 flex-none rounded-full bg-brand" aria-hidden="true" />
          {triesAgain(chapter, view) ? t('tutorial.agent.again', { agent }) : t('tutorial.agent.working', { agent })}
        </p>
      )
    case 'waiting':
      return <p>{state.waiting?.kind === 'approve' ? t('tutorial.agent.held', { agent }) : t('tutorial.agent.waiting', { agent })}</p>
    case 'end':
      return <p>{t('tutorial.agent.done')}</p>
    case 'ready':
    case 'shown':
      return null
  }
}

/** The step's prompt, typed and not sent yet (or on its way: no caret then). */
function TypedPrompt({ text, sending }: { text: string; sending: boolean }) {
  const { t } = useTranslation()
  return (
    <div className="flex gap-2 rounded-sm border border-line-2 bg-surface px-2.5 py-2 font-semibold text-ink">
      <span className="text-brand-ink select-none" aria-hidden="true">
        {'>'}
      </span>
      <p className="min-w-0">
        {!sending && <span className="sr-only">{t('tutorial.agent.typed')} </span>}
        {text}
        {!sending && <span className="tutorial-caret ml-0.5 inline-block h-[14px] w-[7px] translate-y-[2px] bg-ink-3" aria-hidden="true" />}
      </p>
    </div>
  )
}

function Line({ line }: { line: TutorialLine }) {
  const { t } = useTranslation()
  switch (line.type) {
    case 'prompt':
      // Said in a conversation resumed from before Latet was on: greyed, under the line that says so.
      return (
        <p className={`flex gap-2 font-semibold ${line.resumed ? 'text-ink-3' : 'text-ink'}`}>
          <span className={`${line.resumed ? '' : 'text-brand-ink'} select-none`} aria-hidden="true">
            {'>'}
          </span>
          <span className="min-w-0">{line.text}</span>
        </p>
      )
    case 'agent':
      return (
        <p className="flex gap-2 text-ink-2">
          <Bullet className="text-ink-3" />
          <span className="min-w-0 italic">{line.text}</span>
        </p>
      )
    case 'model':
      return (
        <p className={`flex gap-2 ${line.resumed ? 'text-ink-3' : 'text-ink'}`}>
          <Bullet className={line.resumed ? 'text-ink-3' : 'text-brand'} />
          <span className="min-w-0">{line.text}</span>
        </p>
      )
    case 'error':
      return (
        <p className="flex gap-2 text-crit-ink">
          <Icon name="ban" size={13} className="mt-[3px] flex-none" />
          <span className="min-w-0">{line.text}</span>
        </p>
      )
    case 'system':
      return <p className="text-ink-3">{line.note ? t(NOTES[line.note]) : line.text}</p>
    case 'tool':
      return (
        <div>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Bullet className="text-ink-3" />
            <span className="min-w-0">
              <span className="font-semibold">{line.name}</span>
              {'('}
              {line.summary}
              {')'}
            </span>
            {line.pending && <span className="tag font-sans">{t('tutorial.agent.pending')}</span>}
          </p>
          {line.output !== undefined && line.output !== '' && (
            <div className="mt-1 ml-[3px] border-l border-line-2 pl-3 text-ink-2">
              <Folded text={line.output} />
            </div>
          )}
        </div>
      )
    case 'file':
      return (
        <div>
          <p className="flex items-center gap-2 text-ink">
            <Icon name="check" size={13} className="flex-none text-good-ink" />
            {t('tutorial.agent.wrote', { path: line.path })}
          </p>
          <div className="mt-1 rounded-sm border border-line bg-surface px-2.5 py-1.5">
            <Folded text={line.content} />
          </div>
        </div>
      )
  }
}

function Bullet({ className }: { className: string }) {
  return (
    <span className={`flex-none select-none ${className}`} aria-hidden="true">
      {'●'}
    </span>
  )
}

/** A long output or file, its first lines and "more". */
function Folded({ text }: { text: string }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const lines = text.split('\n')
  const long = lines.length > FOLD_AFTER
  return (
    <>
      <span>{long && !open ? lines.slice(0, FOLD_AFTER).join('\n') : text}</span>
      {long && (
        <button type="button" className="link mt-1 block font-sans text-meta" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? t('tutorial.agent.less') : t('tutorial.agent.more', { count: lines.length - FOLD_AFTER })}
        </button>
      )}
    </>
  )
}
