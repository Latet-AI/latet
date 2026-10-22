// One chapter of the tutorial: the pretend agent works on the left; on the right Latet's own
// components show what Latet did with each request (narrow screens: the agent above, Latet below).
// The daemon runs the steps (`next`) and says where the chapter stands; the page keeps the step on screen
// (lib/tutorial.ts), reads the state again while the agent works or a wait is the daemon's to check, and
// sends `next` itself once the user did what a step asked. Enter does what the button does.

import { useEffect, useEffectEvent, useReducer, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { ApiError, api } from '../api/client'
import type { TutorialChapter, TutorialMenu, TutorialState } from '../api/types'
import { Icon } from '../components/Icon'
import { AgentPane } from '../components/tutorial/AgentPane'
import { ChapterEnd } from '../components/tutorial/ChapterEnd'
import { EndTutorialButton, TutorialEnded } from '../components/tutorial/EndTutorial'
import { LatetPane } from '../components/tutorial/LatetPane'
import { TutorialNeeds } from '../components/tutorial/TutorialNeeds'
import { useToast } from '../components/Toast'
import { ConfirmDialog, EmptyState, ErrorState, errorText, Loading, PageHeader } from '../components/ui'
import { keyFacts } from '../lib/keys'
import { useMe } from '../lib/me'
import {
  autoSends,
  conflictState,
  enterAction,
  initialView,
  menuCards,
  nextBody,
  openChapter,
  phaseOf,
  pollDelay,
  primaryAction,
  RESTART,
  viewReducer,
  type Action,
  type Opened,
  type View,
} from '../lib/tutorial'
import { useAsync } from '../lib/useAsync'

/** A 409 with no state to go on from: the sandbox runs another chapter now (another tab), or none. */
function notRunning(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && conflictState(error) === null
}

export function TutorialPage() {
  const { t, i18n } = useTranslation()
  const { chapter: id = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const restart = search.has(RESTART) // the menu's Start again: a new run, not the end of the one that finished
  // The page's language: the chapter's texts come in it, but for the chapter running, which keeps its own.
  const lang = i18n.resolvedLanguage ?? 'en'
  const opened = useAsync(`tutorial:${id}:${lang}`, (signal) => openChapter(api.tutorial, id, restart, signal, lang))
  const menu = useAsync(`tutorial:menu:${lang}`, (signal) => api.tutorial.chapters(lang, signal))
  const { reload: reloadMe, reloadConfig } = useMe()
  const [ended, setEnded] = useState(false)
  const data = !opened.stale && opened.data?.chapter.id === id ? opened.data : undefined
  const run = data?.state?.run

  // A chapter opens at the top, the next one included.
  useEffect(() => {
    window.scrollTo(0, 0)
  }, [id])
  // The new run is open: the address forgets it was asked for, so that a reload opens the chapter as it stands.
  // Not while the chapter waits for what it needs (no run is open): looking again must start it over, as asked.
  useEffect(() => {
    if (!restart || !data?.state) return
    setSearch(
      (current) => {
        const rest = new URLSearchParams(current)
        rest.delete(RESTART)
        return rest
      },
      { replace: true },
    )
  }, [restart, data, setSearch])
  // A chapter started (or started over): the bar of the other pages, and the settings the app holds, follow.
  useEffect(() => {
    if (run === undefined) return
    reloadMe()
    reloadConfig()
  }, [run, reloadMe, reloadConfig])

  if (ended) return <TutorialEnded />
  if (opened.error instanceof ApiError && opened.error.status === 404) {
    return (
      <div className="panel">
        <EmptyState
          icon="question"
          title={t('tutorial.chapter.notFound', { id })}
          action={
            <Link to="/tutorial" className="btn">
              {t('tutorial.chapter.back')}
            </Link>
          }
        >
          {t('tutorial.chapter.notFoundBody')}
        </EmptyState>
      </div>
    )
  }
  if (opened.error) return <ErrorState error={opened.error} onRetry={opened.reload} />
  if (!data) return <Loading />
  if (data.state === null) return <TutorialNeeds chapter={data.chapter} onAgain={opened.reload} />
  return (
    <ChapterRun
      key={`${data.chapter.id}:${data.state.run}`}
      chapter={data.chapter}
      first={data.state}
      menu={menu.data}
      onOpened={(next) => opened.mutate(() => next)}
      onEnded={() => setEnded(true)}
    />
  )
}

interface ChapterRunProps {
  chapter: TutorialChapter
  /** Where the chapter stood when this run of it opened. */
  first: TutorialState
  menu: TutorialMenu | undefined
  /** The chapter was started over (here, or in another tab): the page opens afresh on it. */
  onOpened: (opened: Opened) => void
  onEnded: () => void
}

function ChapterRun({ chapter, first, menu, onOpened, onEnded }: ChapterRunProps) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const toast = useToast()
  const { reload: reloadMe } = useMe()
  const [state, setState] = useState(first)
  const [view, dispatch] = useReducer(viewReducer, first, (opening) => initialView(chapter.steps.length, opening))
  const [failure, setFailure] = useState<unknown>(null)
  const [stopped, setStopped] = useState(false)
  const [polls, setPolls] = useState(0) // a failed read asks again
  const [restartOpen, setRestartOpen] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const sending = useRef(false)

  const phase = phaseOf(chapter, state, view)
  const action = stopped ? null : primaryAction(chapter, state, view)
  const cards = menu ? menuCards(menu) : []
  const card = cards.find((one) => one.id === chapter.id)
  const nextCard = cards.find((one) => one.id === state.end?.next)

  /** A state from the daemon. Another run of the chapter (started over elsewhere) opens afresh. */
  const take = (next: TutorialState, as: 'answer' | 'poll') => {
    if (next.run !== first.run) {
      onOpened({ chapter, state: next })
      return
    }
    setState(next)
    dispatch({ type: as, state: next })
  }

  const send = async () => {
    if (sending.current) return // one call at a time, whoever asks (the button, Enter, a wait met)
    sending.current = true
    const body = nextBody(state, view)
    dispatch({ type: 'send' })
    setFailure(null)
    try {
      take((await api.tutorial.next(chapter.id, body)).state, 'answer')
    } catch (error) {
      const conflict = conflictState(error)
      if (conflict) take(conflict, 'answer')
      else {
        dispatch({ type: 'failed' })
        if (notRunning(error)) setStopped(true)
        else setFailure(error)
      }
    } finally {
      sending.current = false
    }
  }

  const restart = async () => {
    setRestarting(true)
    try {
      const opened = await api.tutorial.start(chapter.id, i18n.resolvedLanguage ?? 'en') // Start over: in the page's language
      setRestartOpen(false)
      onOpened(opened)
    } catch (error) {
      toast.show(conflictState(error)?.busy ? t('tutorial.chapter.busy', { agent: chapter.agent.name }) : errorText(t, error), 'fail')
      setRestarting(false)
    }
  }

  const act = (todo: Action) => {
    switch (todo) {
      case 'send':
      case 'retry':
        void send()
        break
      case 'next':
        dispatch({ type: 'advance' })
        break
      case 'finish':
        dispatch({ type: 'advance' })
        break
      case 'chapter':
        if (state.end?.next) navigate(`/tutorial/${encodeURIComponent(state.end.next)}`)
        break
      case 'menu':
        navigate('/tutorial')
        break
    }
  }

  // The last step ran: the chapter is done, and `/me` has none in progress any more. The bar of the other pages knows.
  useEffect(() => {
    if (state.finished && !first.finished) reloadMe()
  }, [state.finished, first.finished, reloadMe])

  // The user did what a step waited for: the agent tries again, by itself (sent right after this render,
  // and asked again then, so a render that comes first cannot send it twice).
  const sendWhenMet = useEffectEvent(() => {
    if (!stopped && autoSends(chapter, state, view)) void send()
  })
  const met = !stopped && autoSends(chapter, state, view)
  useEffect(() => {
    if (!met) return
    const timer = setTimeout(() => sendWhenMet(), 0)
    return () => clearTimeout(timer)
  }, [met])

  // While the agent works, or the daemon is to see what the user does, ask it again each second.
  const delay = stopped ? null : pollDelay(state, view)
  const polled = useEffectEvent((next: TutorialState) => {
    setFailure(null)
    take(next, 'poll')
  })
  const pollFailed = useEffectEvent((error: unknown) => {
    if (notRunning(error)) setStopped(true)
    else {
      setFailure(error)
      dispatch({ type: 'unread' }) // asked again at the usual pace, not every 150 ms
      setPolls((n) => n + 1)
    }
  })
  useEffect(() => {
    if (delay === null) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      // An answer that comes after this read was given up (the state changed meanwhile) is not taken.
      api.tutorial.state(chapter.id, controller.signal).then(
        (answer) => {
          if (!controller.signal.aborted) polled(answer.state)
        },
        (error: unknown) => {
          if (!controller.signal.aborted) pollFailed(error)
        },
      )
    }, delay)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [chapter.id, delay, state, polls])

  // Enter means Next: what the button does, unless something else on the page takes the key.
  const onKey = useEffectEvent((event: KeyboardEvent) => {
    const todo = stopped ? null : enterAction(chapter, state, view, keyFacts(event))
    if (todo === null) return
    event.preventDefault()
    act(todo)
  })
  useEffect(() => {
    const listener = (event: KeyboardEvent) => onKey(event)
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [])

  const onPinned = () => {
    if (state.waiting?.kind === 'pin' && state.waiting.step === view.step) dispatch({ type: 'pinned' })
  }

  return (
    <>
      <Link to="/tutorial" className="mb-3 inline-flex items-center gap-1.5 text-ui text-ink-2 hover:text-ink">
        <Icon name="arrowLeft" size={14} />
        {t('tutorial.chapter.back')}
      </Link>
      <PageHeader
        title={chapter.title}
        docs="page.tutorial"
        description={
          <>
            <span className="text-ui text-ink-3">
              {card ? t('tutorial.chapter.of', { number: card.position, count: card.total }) : t('tutorial.chapter.number', { number: chapter.number })}
              {' · '}
              {t('tutorial.menu.minutes', { count: chapter.minutes })}
            </span>
            <StepTrack chapter={chapter} view={view} />
          </>
        }
        actions={
          <>
            <button type="button" className="btn" onClick={() => setRestartOpen(true)}>
              <Icon name="refresh" size={14} />
              {t('tutorial.chapter.startOver')}
            </button>
            <EndTutorialButton onEnded={onEnded} />
          </>
        }
      />

      {stopped && (
        <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
          <span className="flex items-start gap-2">
            <Icon name="info" size={15} className="mt-0.5 flex-none" />
            {t('tutorial.chapter.stopped')}
          </span>
          <button type="button" className="btn btn-sm" disabled={restarting} onClick={() => void restart()}>
            {t('tutorial.chapter.startOver')}
          </button>
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-5 max-lg:grid-cols-1">
        <AgentPane chapter={chapter} state={state} view={view} phase={phase} action={action} stopped={stopped} onAction={act} failure={failure} />
        {phase === 'end' && state.end ? (
          <ChapterEnd
            end={state.end}
            nextTitle={nextCard?.title ?? null}
            onNext={() => {
              if (action) act(action) // the next chapter, or the menu after the last
            }}
          />
        ) : (
          <LatetPane chapter={chapter} state={state} view={view} phase={phase} onPinned={onPinned} />
        )}
      </div>

      <ConfirmDialog
        open={restartOpen}
        title={t('tutorial.chapter.startOverTitle')}
        confirmLabel={t('tutorial.chapter.startOver')}
        onConfirm={() => void restart()}
        onCancel={() => setRestartOpen(false)}
        busy={restarting}
        destructive={false}
      >
        <p>{t('tutorial.chapter.startOverBody')}</p>
      </ConfirmDialog>
    </>
  )
}

/** The chapter's steps as a row of bars: the ones done, the one on screen, the ones to come. */
function StepTrack({ chapter, view }: { chapter: TutorialChapter; view: View }) {
  const { t } = useTranslation()
  const count = chapter.steps.length
  const label = view.step >= count ? t('tutorial.chapter.allDone', { count }) : t('tutorial.chapter.step', { number: view.step + 1, count })
  return (
    <span className="mt-2 flex items-center gap-3">
      <span className="flex items-center gap-1" aria-hidden="true">
        {chapter.steps.map((step, i) => (
          <span
            key={step.index}
            title={step.title}
            className={`h-1.5 w-7 rounded-full ${i === view.step ? 'bg-brand' : i < view.step ? 'bg-ink-2' : 'bg-line-2'}`}
          />
        ))}
      </span>
      <span className="text-meta text-ink-3">{label}</span>
    </span>
  )
}
