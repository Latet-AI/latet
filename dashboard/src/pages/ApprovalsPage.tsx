// The Live Reviewer: what your agents are about to send, waiting for your approval. You approve a
// batch as Latet masked it, mask more first, or stop it. Two tabs: the texts waiting (Texts), and the change to
// your settings waiting (Settings). Below 1100 px, one text at a time with its decision in a bar at the bottom.
// Keyboard shortcuts, off until you turn them on, decide the texts too (lib/shortcuts); Enter approves nothing. A text
// is decided only once it has shown 400 ms, and after a decision the focus is on the text that shows next.
// `embedded` is the queue and the batch alone, for another page to show (the tutorial's): no header, no tabs, no
// approve-all, no switch, no shortcuts, one column.

import { useEffect, useEffectEvent, useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import type { Batch, LiveItem, ReviewCategory } from '../api/types'
import { AgentLabel } from '../components/badges'
import { ClassLegend } from '../components/exchange/Legend'
import { DocsWhyLink } from '../components/DocsLinks'
import { Icon } from '../components/Icon'
import { LiveReviewSwitch } from '../components/LiveReviewEditor'
import { PendingChanges } from '../components/SettingsChange'
import { tabIds, Tabs } from '../components/Tabs'
import { blockMarks } from '../components/masked/marks'
import { markStyle, type MarkTone } from '../components/masked/MaskedText'
import { REFUSED, SelectableText, type SelectableHandle, type ShownRange } from '../components/masked/SelectableText'
import type { TextMark } from '../components/masked/types'
import { CATEGORY_LABEL } from '../components/ReviewerEditor'
import { ShortcutsDialog } from '../components/ShortcutsDialog'
import { useToast } from '../components/Toast'
import { ImageCanvas } from '../components/images/ImageCanvas'
import { ConfirmDialog, EmptyState, errorText, Loading, PageHeader } from '../components/ui'
import { chooseValue } from '../components/ValueScope'
import { tk } from '../i18n'
import {
  approveBody,
  batchKinds,
  checkSelection,
  findValues,
  inProject,
  maskedRanges,
  openSuggestions,
  previewSent,
  projectsWaiting,
  selectedBatch,
  shortSession,
  toCodePoint,
  type AddedMask,
  type ProjectFilter,
} from '../lib/approvals'
import { useApprovals } from '../lib/approvals-context'
import {
  APPROVALS_TABS,
  approvalsTabNamed,
  initialApprovalsTab,
  mayDecideNow,
  queuePosition,
  shownNow,
  type ApprovalsTab,
  type Shown,
} from '../lib/approvalsView'
import { formatRelative } from '../lib/format'
import { choiceFor, imageChanges, type ImageChoice } from '../lib/images'
import { isJudgement, KIND_LABEL } from '../lib/labels'
import { useDeploymentKey } from '../lib/me'
import { usePref } from '../lib/prefs'
import { CATEGORY_TYPE } from '../lib/reviewer'
import { SHORTCUTS, turnedOn, useApprovalShortcuts, type ShortcutAction } from '../lib/shortcuts'
import { codePointMapper, splitByRanges } from '../lib/spans'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'
import { useNarrow } from '../lib/useNarrow'

const FAILED_OTHER = tk('reviewer.failed.other')
const FAILED_LABEL: Record<string, string> = {
  timeout: tk('reviewer.failed.timeout'),
  unreachable: tk('reviewer.failed.unreachable'),
  unreadable: tk('reviewer.failed.unreadable'),
  suspect: tk('reviewer.failed.suspect'),
  busy: tk('reviewer.failed.busy'),
}

const NO_PROJECT = '-' // the filter's value for batches of no project (project ids are hex)

/** The ids of the tabs of Approvals and of their panels (`tabIds`). */
const TABS_ID = 'approvals'

/** The tone of the Settings tab's count while a change waits. */
const WARM = 'warm' as const

const TAB_LABEL: Record<ApprovalsTab, string> = {
  texts: tk('approvals.tabs.texts'),
  settings: tk('approvals.tabs.settings'),
}

/** The key of a shortcut, as a button names it (`aria-keyshortcuts`) and as its hint shows it. */
const KEY = Object.fromEntries(SHORTCUTS.map((s) => [s.action, s.shift ? `Shift+${s.key.toUpperCase()}` : s.key.toUpperCase()])) as Record<
  ShortcutAction,
  string
>

/** What M asks of a text of the batch shown: mask its selection, or its first suggestion; false when it has none. */
interface Masker {
  selection: () => boolean
  suggestion: () => boolean
}

/** Seconds since the epoch, ticking every second while `active`. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

export function ApprovalsPage({ embedded = false }: { embedded?: boolean }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { batches, changes, loaded, stream, live, remove } = useApprovals()
  const projects = useAsync('projects', (signal) => api.projects(signal))
  const projectNames = useMemo(() => new Map((projects.data?.items ?? []).map((p) => [p.id, p.name])), [projects.data])
  const [selection, setSelection] = useState<{ id: string; index: number } | null>(null)
  const [masks, setMasks] = useState<Record<string, AddedMask[]>>({})
  const [images, setImages] = useState<Record<string, Record<number, ImageChoice>>>({})
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState<{ id: string; message: string } | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [approvingAll, setApprovingAll] = useState(false)
  const [filter, setFilter] = useState<ProjectFilter>(undefined)
  // Below 1100 px, one text at a time: a counter and the project menu above it, its decision in a bar at the bottom.
  const narrowWindow = useNarrow()
  const narrow = narrowWindow && !embedded
  // The tab: the one a link names (`#settings`), else the one chosen once as the queue loads. A change or a text
  // arriving later only changes the counts: the tab under you never moves by itself.
  const location = useLocation()
  const navigate = useNavigate()
  const [openedOn, setOpenedOn] = useState<ApprovalsTab | null>(null)
  if (openedOn === null && loaded) setOpenedOn(initialApprovalsTab(batches, changes))
  const tab = approvalsTabNamed(location.hash) ?? openedOn ?? 'texts'
  const pickTab = (id: string) => navigate({ search: location.search, hash: `#${id}` }, { replace: true })
  const waitingIn = projectsWaiting(batches)
  const shown = inProject(batches, filter)
  const filtered = shown !== batches
  const now = useNow(batches.length > 0)
  const selected = selectedBatch(shown, selection)
  const selectedMasks = selected ? (masks[selected.id] ?? []) : []
  const anyMasks =
    Object.entries(masks).some(([id, list]) => list.length > 0 && shown.some((b) => b.id === id)) ||
    Object.entries(images).some(([id, choices]) => Object.keys(choices).length > 0 && shown.some((b) => b.id === id))

  // A project seen for the first time (a session in a new folder): load the names again.
  const unknownProject = projects.data !== undefined && batches.some((b) => b.project !== null && !projectNames.has(b.project))
  const reloadProjects = projects.reload
  useEffect(() => {
    if (unknownProject) reloadProjects()
  }, [unknownProject, reloadProjects])

  const select = (batch: Batch) => setSelection({ id: batch.id, index: shown.indexOf(batch) })
  const projectLabel = (project: string | null) =>
    project === null ? t('approvals.filter.none') : (projectNames.get(project) ?? t('approvals.filter.unknown'))
  const setBatchMasks = (id: string, update: (list: AddedMask[]) => AddedMask[]) => {
    setMasks((all) => ({ ...all, [id]: update(all[id] ?? []) }))
    setError(null)
  }
  const setBatchImage = (id: string, item: number, choice: ImageChoice) => {
    setImages((all) => ({ ...all, [id]: { ...all[id], [item]: choice } }))
    setError(null)
  }
  const forget = (ids: readonly string[]) => {
    const drop = <T,>(all: Record<string, T>) => Object.fromEntries(Object.entries(all).filter(([id]) => !ids.includes(id)))
    setMasks(drop)
    setImages(drop)
  }

  /** "Always mask": the values you chose, added to your terms (the project's, when the session has one). */
  const keepMasking = async (batch: Batch, added: readonly AddedMask[]) => {
    const always = [...new Map(added.filter((m) => m.always).map((m) => [m.value, m])).values()]
    if (always.length === 0) return
    try {
      for (const mask of always) {
        await chooseValue({ kind: 'mask', value: mask.value, type: CATEGORY_TYPE[mask.category] }, batch.project)
      }
      toast.show(batch.project ? t('approvals.termsAddedInProject', { count: always.length }) : t('approvals.termsAdded', { count: always.length }))
    } catch (err) {
      toast.show(errorText(t, err), 'fail')
    }
  }

  // The text drawn anew takes the focus once, as `refocus` says: the step button pressed to show it (a narrow window),
  // or, after a decision, the text itself (its section), never its Approve, so that a key or a click pressed again
  // does not decide it. With nothing left to show, the request is dropped, not kept for a text arriving later.
  const focusNext = useRef<'previous' | 'next' | 'batch' | null>(null)
  const refocus = () => {
    const to = focusNext.current
    focusNext.current = null
    return to
  }
  const hasSelected = selected !== null
  useEffect(() => {
    if (!hasSelected) focusNext.current = null
  })

  // When the text shown came into view: a decision on it waits until it has shown DECIDE_AFTER_MS (`mayDecide`), so
  // that the second click of a double click, or of a double tap, on Approve does not decide the next text unseen. A
  // text shown anew (masked again while you looked) waits again as well.
  const shownSince = useRef<Shown | null>(null)
  const selectedId = selected?.id ?? null
  const selectedItems = selected?.items
  useEffect(() => {
    shownSince.current = selectedId === null ? null : shownNow(selectedId)
  }, [selectedId, selectedItems])

  const decide = async (batch: Batch, action: 'approve' | 'reject') => {
    if (busy || !mayDecideNow(shownSince.current, batch.id)) return
    const added = masks[batch.id] ?? []
    setBusy(action)
    setError(null)
    try {
      await api.decide(batch.id, action === 'approve' ? approveBody(added, images[batch.id]) : { action: 'reject' })
      focusNext.current = 'batch'
      remove(batch.id)
      forget([batch.id])
      if (action === 'reject') toast.show(t('approvals.rejected'), 'info')
      else toast.show(added.length > 0 ? t('approvals.approvedWith', { count: added.length }) : t('approvals.approved'))
      if (action === 'approve') await keepMasking(batch, added)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        focusNext.current = 'batch'
        remove(batch.id)
        toast.show(t('approvals.gone'), 'info')
      } else if (err instanceof ApiError && err.status === 422) {
        setError({ id: batch.id, message: t('approvals.refused', { message: err.message }) })
      } else {
        setError({ id: batch.id, message: errorText(t, err) })
      }
    } finally {
      setBusy(null)
    }
  }

  const approveAll = async () => {
    setApprovingAll(true)
    try {
      const waiting = shown.map((b) => b.id)
      const result = await api.approveAll(filtered ? filter : undefined)
      for (const id of waiting) remove(id)
      forget(waiting)
      toast.show(t('approvals.allApproved', { count: result.approved }))
      setConfirmAll(false)
    } catch (err) {
      toast.show(errorText(t, err), 'fail')
    } finally {
      setApprovingAll(false)
    }
  }

  const move = (step: number) => {
    if (!selected) return
    const next = shown[shown.indexOf(selected) + step]
    if (next) select(next)
  }
  // In a narrow window the bar of the text shown is drawn anew with it: the focus goes back to the step button pressed.
  const step = (to: 'previous' | 'next') => {
    focusNext.current = to
    move(to === 'next' ? 1 : -1)
  }

  // The keyboard shortcuts, off until turned on (this browser keeps the choice: `true`, no other value); never in
  // another page's queue (the tutorial's, which has its own keys). They act on the texts only while their tab shows;
  // ? opens their list anywhere.
  const [shortcutsPref, setShortcutsPref] = usePref<unknown>('approvals.shortcuts', false)
  const shortcutsTurnedOn = turnedOn(shortcutsPref)
  const shortcutsOn = shortcutsTurnedOn && !embedded
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const maskers = useRef(new Map<number, Masker>())
  const onTexts = tab === 'texts'
  /** M: the text selected in the original, else the first suggestion, as its Mask button does. */
  const maskShown = () => {
    const texts = [...maskers.current.entries()].sort(([a], [b]) => a - b).map(([, masker]) => masker)
    if (texts.some((text) => text.selection()) || texts.some((text) => text.suggestion())) return
    toast.show(t('approvals.shortcuts.nothingToMask'), 'info')
  }
  useApprovalShortcuts(shortcutsOn, {
    approve: () => {
      if (onTexts && selected) void decide(selected, 'approve')
    },
    skip: () => {
      if (onTexts && selected) void decide(selected, 'reject')
    },
    next: () => {
      if (onTexts) move(1)
    },
    previous: () => {
      if (onTexts) move(-1)
    },
    mask: () => {
      if (onTexts && selected) maskShown()
    },
    approveAll: () => {
      if (onTexts && shown.length > 0) setConfirmAll(true)
    },
    help: () => setShortcutsOpen(true),
  })

  let content
  if (!loaded) {
    content = (
      <div className="panel">
        <Loading />
      </div>
    )
  } else if (!selected) {
    content = (
      <div className="panel">
        {live && !live.enabled ? (
          <EmptyState icon="approvals" title={t('approvals.off.title')}>
            {t('approvals.off.body')}
          </EmptyState>
        ) : (
          <EmptyState icon="approvals" title={changes.length > 0 ? t('approvals.empty.noTexts') : t('approvals.empty.title')}>
            {t('approvals.empty.body')}
          </EmptyState>
        )}
      </div>
    )
  } else {
    const projectMenu = waitingIn.length > 1 && (
      <select
        name="project"
        aria-label={t('approvals.filter.label')}
        className="field w-auto max-w-[16rem] shrink-0 py-0.5"
        value={!filtered ? '' : filter === null ? NO_PROJECT : (filter ?? '')}
        onChange={(e) => setFilter(e.target.value === '' ? undefined : e.target.value === NO_PROJECT ? null : e.target.value)}
      >
        <option value="">{t('approvals.filter.all')}</option>
        {waitingIn.map((project) => (
          <option key={project ?? NO_PROJECT} value={project ?? NO_PROJECT}>
            {projectLabel(project)}
          </option>
        ))}
      </select>
    )
    const position = queuePosition(shown, selected)
    // Previous and next move through the queue as J and K do.
    const steps = narrow ? { previous: position.previous ? () => step('previous') : null, next: position.next ? () => step('next') : null } : undefined
    const batchView = (
      <BatchView
        key={selected.id}
        batch={selected}
        masks={selectedMasks}
        setMasks={(update) => setBatchMasks(selected.id, update)}
        images={images[selected.id] ?? {}}
        setImage={(item, choice) => setBatchImage(selected.id, item, choice)}
        projectName={selected.project ? (projectNames.get(selected.project) ?? null) : null}
        now={now}
        busy={busy}
        error={error?.id === selected.id ? error.message : null}
        onDecide={(action) => void decide(selected, action)}
        steps={steps}
        refocus={refocus}
        shortcuts={shortcutsOn}
        maskers={maskers}
      />
    )
    content = narrow ? (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <p role="status" className="text-ui font-medium text-ink tabular-nums">
            {t('approvals.narrow.position', { number: position.number, count: position.count })}
          </p>
          {projectMenu}
        </div>
        {batchView}
      </div>
    ) : (
      <div className={embedded ? 'grid grid-cols-1 items-start gap-4' : 'grid grid-cols-[300px_minmax(0,1fr)] items-start gap-4 max-xl:grid-cols-1'}>
        <Queue
          batches={shown}
          selected={selected}
          onSelect={select}
          projectNames={projectNames}
          now={now}
          hidden={embedded && shown.length < 2} // embedded, a lone batch is the whole view: the list shows when there is a choice
          filter={projectMenu}
        />
        {batchView}
      </div>
    )
  }

  if (embedded) {
    return (
      <>
        {loaded && stream !== 'open' && (
          <p role="status" className="mb-3 text-meta text-ink-3">
            {t('live.stream.retrying')}
          </p>
        )}
        {content}
      </>
    )
  }

  const textsIds = tabIds(TABS_ID, 'texts')
  const settingsIds = tabIds(TABS_ID, 'settings')
  return (
    <>
      <PageHeader title={t('approvals.title')} description={t('approvals.description')} actions={<NotifyButton />} docs="page.approvals" />

      {loaded && stream !== 'open' && (
        <p role="status" className="mb-3 text-meta text-ink-3">
          {t('live.stream.retrying')}
        </p>
      )}

      <div className="mb-4">
        <Tabs
          label={t('approvals.tabs.label')}
          idPrefix={TABS_ID}
          active={tab}
          onChange={pickTab}
          tabs={APPROVALS_TABS.map((id) => ({
            id,
            label: t(TAB_LABEL[id]),
            count: loaded ? (id === 'texts' ? batches.length : changes.length) : undefined,
            // A change that lowers protection is easy to miss while the page shows the texts: its count is warm.
            countTone: id === 'settings' && loaded && changes.length > 0 ? WARM : undefined,
          }))}
        />
      </div>

      <div id={textsIds.panel} role="tabpanel" aria-labelledby={textsIds.tab} hidden={tab !== 'texts'}>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 rounded-md border border-line bg-surface px-3 py-2">
          <LiveReviewSwitch />
          <span className="flex flex-wrap items-center gap-x-4 gap-y-2 text-meta text-ink-3">
            <button type="button" className="btn btn-ghost btn-sm" aria-haspopup="dialog" onClick={() => setShortcutsOpen(true)}>
              <Icon name="keyboard" size={14} />
              <span>{shortcutsOn ? t('approvals.shortcuts.on') : t('approvals.shortcuts.title')}</span>
              {shortcutsOn && (
                <kbd className="kbd" aria-hidden="true">
                  {KEY.help}
                </kbd>
              )}
            </button>
            <Link to="/rules#live-review" className="link">
              {t('approvals.settings')}
            </Link>
            <button type="button" className="btn" disabled={shown.length === 0} onClick={() => setConfirmAll(true)}>
              <Icon name="check" size={14} />
              {filtered
                ? t('approvals.approveAllIn', { count: shown.length, project: projectLabel(filter ?? null) })
                : t('approvals.approveAll', { count: shown.length })}
            </button>
          </span>
        </div>

        {content}
      </div>

      <div id={settingsIds.panel} role="tabpanel" aria-labelledby={settingsIds.tab} hidden={tab !== 'settings'}>
        {!loaded ? (
          <div className="panel">
            <Loading />
          </div>
        ) : changes.length > 0 ? (
          <PendingChanges />
        ) : (
          <div className="panel">
            <EmptyState icon="rules" title={t('approvals.settingsEmpty.title')}>
              {t('approvals.settingsEmpty.body')}
            </EmptyState>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmAll}
        title={
          filtered
            ? t('approvals.approveAllInTitle', { count: shown.length, project: projectLabel(filter ?? null) })
            : t('approvals.approveAllTitle', { count: shown.length })
        }
        confirmLabel={
          filtered
            ? t('approvals.approveAllIn', { count: shown.length, project: projectLabel(filter ?? null) })
            : t('approvals.approveAll', { count: shown.length })
        }
        onConfirm={() => void approveAll()}
        onCancel={() => setConfirmAll(false)}
        busy={approvingAll}
        destructive={false}
      >
        <p>{t('approvals.approveAllBody')}</p>
        {anyMasks && <p className="mt-2 text-crit-ink">{t('approvals.approveAllMasks')}</p>}
      </ConfirmDialog>

      <ShortcutsDialog open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} enabled={shortcutsTurnedOn} onEnabled={setShortcutsPref} />
    </>
  )
}

function NotifyButton() {
  const { t } = useTranslation()
  const supported = typeof Notification !== 'undefined'
  const [permission, setPermission] = useState<NotificationPermission>(() => (supported ? Notification.permission : 'denied'))
  if (!supported) return null
  if (permission === 'granted') {
    return (
      <span className="inline-flex items-center gap-1.5 text-meta text-ink-3" title={t('approvals.notify.onHelp')}>
        <Icon name="bell" size={13} />
        {t('approvals.notify.on')}
      </span>
    )
  }
  if (permission === 'denied') return <span className="text-meta text-ink-3">{t('approvals.notify.blocked')}</span>
  return (
    <button
      type="button"
      className="btn"
      title={t('approvals.notify.help')}
      onClick={() => void Notification.requestPermission().then(setPermission)}
    >
      <Icon name="bell" size={14} />
      {t('approvals.notify.ask')}
    </button>
  )
}

function Queue({
  batches,
  selected,
  onSelect,
  projectNames,
  now,
  filter,
  hidden,
}: {
  batches: readonly Batch[]
  selected: Batch
  onSelect: (batch: Batch) => void
  projectNames: ReadonlyMap<string, string>
  now: number
  filter: ReactNode
  hidden: boolean
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const list = useRef<HTMLUListElement>(null)

  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0
    if (!step) return
    event.preventDefault()
    const index = batches.indexOf(selected) + step
    const next = batches[index]
    if (!next) return
    onSelect(next)
    list.current?.querySelectorAll<HTMLElement>('[role="option"]')[index]?.focus()
  }

  if (hidden) return null
  return (
    <div className="panel overflow-hidden xl:sticky xl:top-4">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <p className="py-0.5 text-meta text-ink-2">{t('approvals.queue.title', { count: batches.length })}</p>
        {filter}
      </div>
      <ul ref={list} role="listbox" aria-label={t('approvals.queue.label')} className="max-h-[70vh] overflow-y-auto" onKeyDown={onKeyDown}>
        {batches.map((batch) => {
          const active = batch === selected
          const project = batch.project ? projectNames.get(batch.project) : undefined
          const left = batch.expires === null ? null : batch.expires - now
          return (
            <li
              key={batch.id}
              role="option"
              aria-selected={active}
              tabIndex={active ? 0 : -1}
              onClick={() => onSelect(batch)}
              className={`relative cursor-pointer border-b border-line px-3 py-2.5 last:border-b-0 ${active ? 'bg-wash-2' : 'hover:bg-wash'}`}
            >
              <span className={`absolute top-0 bottom-0 left-0 w-[3px] ${active ? 'bg-brand' : 'bg-transparent'}`} aria-hidden="true" />
              <div className="flex items-center justify-between gap-2">
                <AgentLabel agent={batch.agent} />
                <span className="flex-none text-meta text-ink-3 tabular-nums">{formatRelative(fmt.locale, Math.min(batch.created, now), now)}</span>
              </div>
              <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-meta">
                {project && <span className="text-ink-2">{project}</span>}
                <span className="mono truncate text-ink-3" title={batch.session}>
                  {shortSession(batch.session)}
                </span>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {batchKinds(batch).map((kind) => (
                  <span key={kind} className="tag">
                    {t(key(KIND_LABEL[kind]))}
                  </span>
                ))}
              </div>
              {left !== null && (
                <p className={`mt-1.5 text-meta ${left < 30 ? 'text-crit-ink' : 'text-ink-2'}`}>
                  {t('approvals.expires', { time: formatRelative(fmt.locale, batch.expires ?? now, now) })}
                </p>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function BatchView({
  batch,
  masks,
  setMasks,
  images,
  setImage,
  projectName,
  now,
  busy,
  error,
  onDecide,
  steps,
  refocus,
  shortcuts = false,
  maskers,
}: {
  batch: Batch
  masks: readonly AddedMask[]
  setMasks: (update: (list: AddedMask[]) => AddedMask[]) => void
  /** What you did to the batch's images, by item (only those you touched). */
  images: Readonly<Record<number, ImageChoice>>
  setImage: (item: number, choice: ImageChoice) => void
  projectName: string | null
  now: number
  busy: 'approve' | 'reject' | null
  error: string | null
  onDecide: (action: 'approve' | 'reject') => void
  /**
   * In a narrow window, one text at a time: the original and what will be sent one above the other, and the decision
   * in an action bar at the bottom with the previous and the next text (null: none). Undefined: the wide layout.
   */
  steps?: { previous: (() => void) | null; next: (() => void) | null } | undefined
  /**
   * Where the focus goes as this text shows, asked once: the step button pressed to show it (`previous`, `next`), or,
   * after a decision on the text before, this text's section (`batch`); null: it stays where it is.
   */
  refocus?: () => 'previous' | 'next' | 'batch' | null
  /** The keyboard shortcuts are on: the buttons show their keys. */
  shortcuts?: boolean
  /** Where each text of the batch tells the page how to mask for the M shortcut, by its index. */
  maskers?: RefObject<Map<number, Masker>>
}) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const section = useRef<HTMLElement>(null)
  const previousButton = useRef<HTMLButtonElement>(null)
  const nextButton = useRef<HTMLButtonElement>(null)
  const takeFocus = useEffectEvent(() => {
    const to = refocus?.()
    if (!to) return
    if (to === 'batch') {
      section.current?.focus()
      return
    }
    // At an end of the queue the button pressed is off: the other one takes the focus.
    const [pressed, other] = to === 'next' ? [nextButton.current, previousButton.current] : [previousButton.current, nextButton.current]
    ;(pressed && !pressed.disabled ? pressed : other)?.focus()
  })
  useEffect(() => takeFocus(), [])
  const values = useMemo(() => masks.map((m, key) => ({ value: m.value, category: m.category, key })), [masks])
  const legend = useMemo(
    () => batch.items.flatMap((item, i) => blockMarks(item.original, item.masked, item.replacements, `${batch.id}:${i}`).values),
    [batch],
  )
  const changed = imageChanges(images)
  const anyChange = masks.length > 0 || Object.keys(images).length > 0
  const summary = (
    <div className="min-w-0 space-y-1 text-meta text-ink-2">
      {masks.length > 0 && <p>{t('approvals.decision.masks', { count: masks.length })}</p>}
      {changed.rects > 0 && <p>{t('approvals.decision.rects', { count: changed.rects })}</p>}
      {changed.withheld > 0 && <p>{t('approvals.decision.withheld', { count: changed.withheld })}</p>}
      {!anyChange && <p>{t('approvals.decision.asIs')}</p>}
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-crit-ink">
          <Icon name="ban" size={13} className="mt-px flex-none" />
          {error}
        </p>
      )}
    </div>
  )
  const buttons = (
    <div className="flex flex-wrap gap-2">
      <button
        type="button"
        className="btn"
        disabled={busy !== null}
        onClick={() => onDecide('reject')}
        title={t('approvals.rejectHelp')}
        aria-keyshortcuts={shortcuts ? KEY.skip : undefined}
      >
        <Icon name="ban" size={14} />
        {t('approvals.reject')}
        {shortcuts && (
          <kbd className="kbd" aria-hidden="true">
            {KEY.skip}
          </kbd>
        )}
      </button>
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy !== null}
        onClick={() => onDecide('approve')}
        aria-keyshortcuts={shortcuts ? KEY.approve : undefined}
      >
        <Icon name="check" size={14} />
        {busy === 'approve'
          ? t('approvals.approving')
          : masks.length > 0
            ? t('approvals.approveWith', { count: masks.length })
            : anyChange
              ? t('approvals.approveChanged')
              : t('approvals.approve')}
        {shortcuts && (
          <kbd className="kbd" aria-hidden="true">
            {KEY.approve}
          </kbd>
        )}
      </button>
    </div>
  )
  return (
    // tabIndex -1: the focus can be put here after a decision (the text that shows next), never reached with Tab.
    <section ref={section} tabIndex={-1} aria-label={t('approvals.batch.label')} className="min-w-0 space-y-3">
      <header className="panel flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 py-2.5">
        <AgentLabel agent={batch.agent} />
        {projectName && <span className="text-ui text-ink-2">{projectName}</span>}
        <code className="mono text-meta text-ink-3" title={batch.session}>
          {shortSession(batch.session)}
        </code>
        <span className="text-meta text-ink-3">{t('approvals.arrived', { time: formatRelative(fmt.locale, Math.min(batch.created, now), now) })}</span>
        {batch.expires !== null && (
          <span className={`text-meta ${batch.expires - now < 30 ? 'text-crit-ink' : 'text-ink-2'}`}>
            {t('approvals.expires', { time: formatRelative(fmt.locale, batch.expires, now) })}
          </span>
        )}
        <span className="ml-auto">
          <ClassLegend values={legend} />
        </span>
      </header>

      <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-meta text-ink-2" aria-label={t('legend.label')}>
        <li className="inline-flex items-center gap-1.5">
          <span className="mask-suggested code flex-none px-1 text-micro">{t('legend.sample')}</span>
          {t('approvals.legend.suggested')}
        </li>
        <li className="inline-flex items-center gap-1.5">
          <span className="mask-pending code flex-none px-1 text-micro">{t('legend.sample')}</span>
          {t('approvals.legend.added')}
        </li>
      </ul>

      {batch.items.map((item, index) => (
        <ItemView
          key={index}
          item={item}
          index={index}
          count={batch.items.length}
          scope={`${batch.id}:${index}`}
          masks={masks}
          values={values}
          inProject={batch.project !== null}
          stacked={steps !== undefined}
          maskers={maskers}
          setMasks={setMasks}
          image={
            item.image && {
              choice: choiceFor(item.image, images[index]),
              src: (view) => api.approvalImageUrl(batch.id, index, view),
              onChange: (choice) => setImage(index, choice),
            }
          }
        />
      ))}

      {steps ? (
        // The page's bottom edge to edge, over the main area's padding.
        <div className="actionbar -mx-7 px-7 max-lg:-mx-5 max-lg:px-5">
          <div className="flex gap-1">
            <button
              ref={previousButton}
              type="button"
              className="btn btn-icon"
              disabled={steps.previous === null}
              onClick={steps.previous ?? undefined}
              aria-keyshortcuts={shortcuts ? KEY.previous : undefined}
              aria-label={t('approvals.narrow.previous')}
              title={t('approvals.narrow.previous')}
            >
              <Icon name="chevronLeft" size={14} />
            </button>
            <button
              ref={nextButton}
              type="button"
              className="btn btn-icon"
              disabled={steps.next === null}
              onClick={steps.next ?? undefined}
              aria-keyshortcuts={shortcuts ? KEY.next : undefined}
              aria-label={t('approvals.narrow.next')}
              title={t('approvals.narrow.next')}
            >
              <Icon name="chevronRight" size={14} />
            </button>
          </div>
          <div className="min-w-[12rem] flex-1">{summary}</div>
          {buttons}
        </div>
      ) : (
        <div className="panel sticky bottom-3 z-10 flex flex-wrap items-center justify-between gap-3 px-3 py-2.5 shadow-[var(--shadow)]">
          {summary}
          {buttons}
        </div>
      )}
    </section>
  )
}

function ItemView({
  item,
  index,
  count,
  scope,
  masks,
  values,
  inProject,
  stacked,
  maskers,
  setMasks,
  image,
}: {
  item: LiveItem
  index: number
  count: number
  scope: string
  /** Every value you masked in the batch: each is masked wherever it appears. */
  masks: readonly AddedMask[]
  values: readonly { value: string; category: ReviewCategory; key: number }[]
  inProject: boolean
  /** The original above what will be sent, whatever the width (a narrow window); else side by side when there is room. */
  stacked: boolean
  maskers?: RefObject<Map<number, Masker>> | undefined
  setMasks: (update: (list: AddedMask[]) => AddedMask[]) => void
  /** The image the text was read in, with what you did to it. */
  image?: { choice: ImageChoice; src: (view: 'original' | 'sent') => string; onChange: (choice: ImageChoice) => void } | undefined
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const toast = useToast()
  const marks = useMemo(() => blockMarks(item.original, item.masked, item.replacements, scope), [item, scope])
  const original = item.original
  const toOriginal = useMemo(() => codePointMapper(original), [original])
  const masked = useMemo(() => maskedRanges(item), [item])
  // Where the values you masked appear in this text, and the suggestions they leave open.
  const found = useMemo(() => findValues(original, values, marks.original ?? []), [original, values, marks.original])
  const foundPoints = useMemo(
    () => found.map((f) => ({ start: toCodePoint(original, f.start), end: toCodePoint(original, f.end) })),
    [found, original],
  )
  const suggestions = useMemo(() => openSuggestions(item, foundPoints), [item, foundPoints])
  const preview = useMemo(() => previewSent(item.masked, marks.sent, values), [item.masked, marks.sent, values])
  const mine = masks.map((mask, key) => ({ mask, key })).filter(({ mask }) => mask.item === index)
  const kind = KIND_LABEL[item.kind] ? t(key(KIND_LABEL[item.kind])) : item.kind

  const check = (start: number, end: number) => checkSelection(original, start, end, masked, foundPoints)
  const add = (value: { start: number; end: number; value: string }, category: ReviewCategory) =>
    setMasks((list) => [...list, { item: index, start: value.start, end: value.end, value: value.value, category, always: false }])
  const addSuggestion = (key: number) => {
    const suggestion = suggestions[key]
    if (!suggestion) return
    const result = check(toOriginal(suggestion.start), toOriginal(suggestion.end))
    if (result.ok) add(result, suggestion.category)
    else toast.show(t(REFUSED[result.reason]), 'info')
  }

  // What the M shortcut does in this text: open the menu for its selection, else mask its first suggestion.
  const selectable = useRef<SelectableHandle>(null)
  useEffect(() => {
    const all = maskers?.current
    if (!all) return
    all.set(index, {
      selection: () => selectable.current?.pickSelection() ?? false,
      suggestion: () => {
        if (suggestions.length === 0) return false
        addSuggestion(0)
        return true
      },
    })
    return () => {
      all.delete(index)
    }
  })

  const shownFound: ShownRange[] = found.map((f) => ({ ...f, category: masks[f.key]?.category ?? 'sensitive' }))
  const shownSuggestions: ShownRange[] = suggestions.map((s, key) => ({ start: toOriginal(s.start), end: toOriginal(s.end), key, category: s.category }))

  return (
    // A container, as a block of the exchange page is: the texts stack when the item is narrow (the tutorial's pane).
    <article className="panel @container overflow-hidden" aria-label={t('approvals.item.label', { number: index + 1, kind })}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2">
        <h3 className="flex items-center gap-2 text-ui font-semibold text-ink">
          {count > 1 && <span className="text-meta font-normal text-ink-3 tabular-nums">{t('approvals.item.number', { number: index + 1, count })}</span>}
          {kind}
        </h3>
        {item.tool && <span className="tag mono">{item.tool}</span>}
        {item.path && (
          <code className="mono min-w-0 flex-1 truncate text-meta text-ink-3" title={item.path}>
            {item.path}
          </code>
        )}
        <span className="ml-auto text-meta text-ink-2 tabular-nums">
          {item.replacements.length === 0 ? t('exchange.block.noValues') : t('exchange.block.values', { count: item.replacements.length })}
        </span>
      </header>

      {item.failed && (
        <p role="status" className="flex items-start gap-2 border-b border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
          <Icon name="info" size={15} className="mt-0.5 flex-none" />
          <span>
            {t('approvals.item.failed')}{' '}
            <span className="text-meta text-ink-2">{t(FAILED_LABEL[item.failed] ?? FAILED_OTHER)}</span>{' '}
            <DocsWhyLink target="why.reviewerFailed" />
          </span>
        </p>
      )}

      {item.image && image && (
        <ImageCanvas image={item.image} src={image.src} choice={image.choice} onChange={image.onChange} pending={foundPoints} />
      )}

      <div className={stacked ? 'grid grid-cols-1' : 'grid grid-cols-2 @max-[640px]:grid-cols-1'}>
        <div className={`min-w-0 ${stacked ? 'border-b border-line' : 'border-r border-line @max-[640px]:border-r-0 @max-[640px]:border-b'}`}>
          <p className="flex items-center gap-1.5 border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">
            <Icon name="vault" size={12} className="text-real" />
            {t('approvals.panes.original')}
            <span className="ml-auto text-ink-3">{t('approvals.panes.selectHint')}</span>
          </p>
          <div className="bg-well">
            <SelectableText
              ref={selectable}
              text={original}
              marks={marks.original ?? []}
              suggestions={shownSuggestions}
              added={shownFound}
              check={check}
              onMask={add}
              onSuggestion={addSuggestion}
              label={t('approvals.panes.original')}
              tone="real"
            />
          </div>
        </div>
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 border-b border-line bg-surface px-3 py-1 text-meta text-ink-2">
            <Icon name="external" size={12} className="text-fake" />
            {t('approvals.panes.sent')}
          </p>
          <div className="bg-well">
            <PreviewText text={preview.text} marks={preview.marks} added={preview.added} label={t('approvals.panes.sent')} tone="fake" />
          </div>
        </div>
      </div>

      {suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-2">
          <span className="text-meta text-ink-2">{t('approvals.suggestion.intro')}</span>
          {suggestions.map((s, key) => (
            <span key={key} className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface py-0.5 pr-0.5 pl-2 text-meta">
              <code className="mono break-all text-ink">{original.slice(toOriginal(s.start), toOriginal(s.end))}</code>
              <span className="text-ink-3">{t(CATEGORY_LABEL[s.category])}</span>
              <button type="button" className="btn btn-sm" onClick={() => addSuggestion(key)}>
                <Icon name="eyeOff" size={13} />
                {t('approvals.suggestion.action')}
              </button>
            </span>
          ))}
        </div>
      )}

      {mine.length > 0 && (
        <ul className="divide-y divide-line border-t border-line" aria-label={t('approvals.added.label')}>
          {mine.map(({ mask, key }) => (
            <li key={key} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-ui">
              <code className="mono break-all text-ink">{mask.value}</code>
              <span className="text-meta text-ink-3">{t(CATEGORY_LABEL[mask.category])}</span>
              <span className="text-meta text-ink-3">
                {t('approvals.added.sentAs', { placeholder: CATEGORY_TYPE[mask.category] })}
              </span>
              <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-meta text-ink-2" title={t('approvals.added.alwaysHelp')}>
                <input
                  type="checkbox"
                  className="accent-[var(--ink)]"
                  checked={mask.always}
                  onChange={(e) => setMasks((list) => list.map((m, i) => (i === key ? { ...m, always: e.target.checked } : m)))}
                />
                {inProject ? t('approvals.added.alwaysProject') : t('approvals.added.always')}
              </label>
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-icon"
                aria-label={t('approvals.added.remove', { value: mask.value })}
                title={t('approvals.added.remove', { value: mask.value })}
                onClick={() => setMasks((list) => list.filter((_, i) => i !== key))}
              >
                <Icon name="x" size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

type PreviewPiece = { start: number; end: number; mark: TextMark } | { start: number; end: number; category: ReviewCategory }

/** What will be sent: the masked text, with a placeholder where a value you masked will get its fake. */
function PreviewText({
  text,
  marks,
  added,
  label,
  tone,
}: {
  text: string
  marks: readonly TextMark[]
  added: readonly { start: number; end: number; category: ReviewCategory }[]
  label: string
  /** The tone of the values Latet masked: the fake tone, in what will be sent. */
  tone?: MarkTone
}) {
  const { t } = useTranslation()
  const pieces: PreviewPiece[] = [...marks.map((mark) => ({ start: mark.start, end: mark.end, mark })), ...added]
  const segments = splitByRanges(text, pieces, 'utf16')
  if (text === '') return <div className="code px-3 py-2.5 text-ink-3">{t('masked.empty')}</div>
  return (
    <div className="code max-h-[480px] overflow-auto px-3 py-2.5 text-ink" role="region" aria-label={label}>
      {segments.map((segment, i) => {
        const piece = segment.range
        if (!piece) return <span key={i}>{segment.text}</span>
        if ('category' in piece) {
          return (
            <mark key={i} className="mask-pending" title={t('approvals.added.preview')}>
              {segment.text}
            </mark>
          )
        }
        const style = markStyle(piece.mark, tone)
        return (
          <mark
            key={i}
            className={style.className}
            data-tone={style.tone}
            data-category={piece.mark.info.cls}
            data-dashed={isJudgement(piece.mark.info.cls) ? '' : undefined}
            data-pair={piece.mark.info.pair}
            aria-label={t('masked.valueLabel', { type: piece.mark.info.type, value: segment.text })}
          >
            {segment.text}
          </mark>
        )
      })}
    </div>
  )
}
