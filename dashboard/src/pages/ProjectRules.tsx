// One project's rules, in the tabs of the global ones: what is in effect there, your own settings for it, and its
// latet.yaml (shared, only stricter). The lists are tables of every rule that applies there, each row saying where it
// comes from and edited where it is born; the rest is saved with the save bar.

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { ConfigView, ProjectDetail, ProjectNotice, RecognizerEntry, ReviewMode, SuggestResult } from '../api/types'
import { DocsWhyLink } from '../components/DocsLinks'
import { Icon } from '../components/Icon'
import { PROJECT_DOCS } from '../lib/docsPlaces'
import { GuidanceField, ReviewerInstructions, TryReviewer } from '../components/ReviewerEditor'
import { RulesTransfer } from '../components/RulesTransfer'
import { DataEditor, PROJECT_ANCHORS } from '../components/rules'
import { allowSpec, AllowedTable } from '../components/rules/allowed'
import { PoliciesTable, policiesSpec } from '../components/rules/criteria'
import type { RulesScope } from '../components/rules/editing'
import { RecognizersTable, recognizersSpec } from '../components/rules/recognizers'
import { foundIn, rowsIn, type Incoming, type SettingsOf } from '../components/rules/RuleList'
import { TermsTable, termsSpec } from '../components/rules/terms'
import { formatList } from '../lib/format'
import { useFormat } from '../lib/useFormat'
import { RulesTabs, shownSections, TAB_LABEL, useRulesTabs, type RulesSection, type RulesTabsState } from '../components/RulesTabs'
import { SaveBar, SaveRow } from '../components/SaveBar'
import { useToast } from '../components/Toast'
import { ErrorState, errorText, Loading, Segmented, Switch } from '../components/ui'
import { pendingKey } from '../lib/deployment'
import { PROFILE_LABEL } from '../lib/labels'
import { useDeploymentKey, useMe } from '../lib/me'
import { keepUnsaved } from '../lib/panelSave'
import {
  draftOf,
  personalDraftOf,
  personalOf,
  protectionOf,
  samePersonal,
  sameSettings,
  setProtection,
  settingsOf,
  type PersonalDraft,
  type Protection,
  type SettingsDraft,
} from '../lib/projects'
import { toDraft, type RecognizerDraft } from '../lib/recognizers'
import { noticeReason, savedChangePending, stale } from '../lib/refused'
import { cleanGuidance, joinGuidance } from '../lib/reviewer'
import { allowList, policiesList, recognizersList, termsList } from '../lib/ruleLists'
import type { RuleRow } from '../lib/ruleTable'
import { lostAfter, PROJECT_FIELD_SECTIONS, PROJECT_TABS, projectDirtyTabs, projectTabOf, type ProjectAnchor, type ProjectTab } from '../lib/rulesTabs'
import { useUnsaved } from '../lib/unsaved'
import { useAsync } from '../lib/useAsync'
import i18n, { tk } from '../i18n'

const REVIEW_MODE_LABEL: Record<ReviewMode, string> = {
  off: tk('projects.reviewer.off'),
  background: tk('projects.reviewer.background'),
  inline: tk('projects.reviewer.inline'),
}

const PROTECTION_HELP: Record<Protection, string> = {
  on: tk('projects.protection.onHelp'),
  secrets: tk('projects.protection.secretsHelp'),
  off: tk('projects.protection.offHelp'),
}

/** The name of each tab: those of the global rules as there, then the overview and the repository file. */
const PROJECT_TAB_LABEL: Record<ProjectTab, string> = {
  overview: tk('projects.tabs.overview'),
  protection: TAB_LABEL.protection,
  terms: TAB_LABEL.terms,
  recognizers: TAB_LABEL.recognizers,
  values: TAB_LABEL.values,
  policies: TAB_LABEL.policies,
  reviewers: TAB_LABEL.reviewers,
  repo: tk('projects.tabs.repo'),
}

/** The ids of the tabs of a project's rules and of their panels (`tabIds`). */
const TABS_ID = 'project-rules'

/** The sections of your own settings the save bar saves: those without tables. */
const BAR_SECTIONS: readonly ProjectAnchor[] = [
  PROJECT_ANCHORS.effective,
  PROJECT_ANCHORS.personal,
  PROJECT_ANCHORS.protection,
  PROJECT_ANCHORS.projectData,
  PROJECT_ANCHORS.reviewer,
  PROJECT_ANCHORS.live,
]

export function ProjectRules({ id }: { id: string }) {
  const config = useAsync('rules', (signal) => api.config(signal))
  // The project is read after the settings: their etag, which a save of your own settings for it sends back, is never
  // newer than what the form shows (a change landing between the two reads refuses the save, 409).
  const etag = config.data?.etag
  const project = useAsync(etag === undefined ? null : `project:${id}#${etag}`, async (signal) => ({ etag, detail: await api.project(id, signal) }))
  // A refused or partially completed save: its notice survives the settings read again.
  const [refused, setRefused] = useState<unknown>(null)
  // The tab shown and the search. A link to a section of the project's rules (`#project-data`, from an exchange) opens
  // its tab, and lands there once the form shows.
  const tabs = useRulesTabs(PROJECT_TABS, Boolean(project.data && config.data))
  if (config.loading || (etag !== undefined && project.loading)) return <Loading />
  const reload = () => {
    config.reload()
    project.reload()
  }
  if (!project.data || !config.data) return <ErrorState error={project.error ?? config.error} onRetry={reload} />
  // One form for the project: read again after a save, it carries over what you have not saved (keepUnsaved).
  return (
    <ProjectForm
      project={project.data.detail}
      config={config.data}
      etag={project.data.etag}
      refused={refused}
      tabs={tabs}
      onSaved={() => {
        setRefused(null)
        reload()
      }}
      onRefresh={(error) => {
        setRefused(error)
        reload()
      }}
    />
  )
}

/** The form of one project's rules, made from the settings and the project as read (exported for its tests). */
export function ProjectForm({
  project,
  config,
  etag,
  refused,
  tabs,
  onSaved,
  onRefresh,
}: {
  project: ProjectDetail
  config: ConfigView
  /** Of the GET /config read before the project: what a save of your own settings for it sends back. */
  etag: string | undefined
  /** The last save's notice, kept when the settings are read again. */
  refused: unknown
  tabs: RulesTabsState<ProjectTab>
  onSaved: () => void
  onRefresh: (error: unknown) => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const { me } = useMe()
  const key = useDeploymentKey()
  const format = useFormat()
  const localRepo = me !== undefined && !me.server
  const canEditRepo = localRepo && project.repo.writable
  // The settings as read (bases) and as edited (drafts). Read again after any save, the drafts keep each edit whose
  // setting did not change meanwhile; the others are dropped, and the tabs that lost one said (`lost`).
  const [madeFrom, setMadeFrom] = useState(project)
  const [repoBase, setRepoBase] = useState<SettingsDraft>(() => draftOf(project.repo.settings))
  const [repo, setRepo] = useState<SettingsDraft>(repoBase)
  const [personalBase, setPersonalBase] = useState<PersonalDraft>(() => personalDraftOf(project.personal))
  const [personal, setPersonal] = useState<PersonalDraft>(personalBase)
  const [lost, setLost] = useState<readonly ProjectTab[]>([])
  if (madeFrom !== project) {
    setMadeFrom(project)
    const freshRepo = draftOf(project.repo.settings)
    const freshPersonal = personalDraftOf(project.personal)
    const repoKept = keepUnsaved(repoBase, repo, freshRepo, Object.keys(freshRepo) as (keyof SettingsDraft)[])
    const personalKept = keepUnsaved(personalBase, personal, freshPersonal, Object.keys(freshPersonal) as (keyof PersonalDraft)[])
    setRepoBase(freshRepo)
    setRepo(repoKept.kept)
    setPersonalBase(freshPersonal)
    setPersonal(personalKept.kept)
    const sections: Partial<Record<string, ProjectAnchor>> = PROJECT_FIELD_SECTIONS
    const dropped = new Set<ProjectTab>(personalKept.lost.flatMap((field) => (sections[field] ? [projectTabOf(sections[field])] : [])))
    if (repoKept.lost.length > 0) dropped.add('repo')
    setLost((said) => lostAfter(said, dropped))
  }
  const [repoError, setRepoError] = useState<unknown>(null)
  const [personalError, setPersonalError] = useState<unknown>(refused)
  const [saving, setSaving] = useState(false)
  // A recognizer suggested from the project's files, opened in the Recognizers tab's panel.
  const [suggested, setSuggested] = useState<Incoming<RecognizerDraft> | null>(null)
  const repoDirty = !sameSettings(repo, repoBase)
  const personalDirty = !samePersonal(personal, personalBase)
  useUnsaved('project', repoDirty || personalDirty)
  const setR = <K extends keyof SettingsDraft>(key: K, value: SettingsDraft[K]) => setRepo((d) => ({ ...d, [key]: value }))
  const set = <K extends keyof PersonalDraft>(key: K, value: PersonalDraft[K]) => setPersonal((d) => ({ ...d, [key]: value }))
  const scope: RulesScope = { kind: 'project', project, global: config }

  const run = async (action: () => Promise<unknown>, onError: (error: unknown) => void, done: string, sent?: PersonalDraft) => {
    setSaving(true)
    onError(null)
    try {
      await action()
      toast.show(done)
      onSaved()
    } catch (error) {
      onError(error)
      if (sent && savedChangePending(error)) {
        // Kept: what was sent is what is saved now, until the settings read again say otherwise.
        setPersonalBase(sent)
        setPersonal(sent)
        onRefresh(error)
      } else if (stale(error)) onRefresh(error)
    } finally {
      setSaving(false)
    }
  }
  const saveRepo = () => run(() => api.saveRepo(project.id, settingsOf(repo, true), project.repo.tag), setRepoError, t('projects.repo.saved'))
  const savePersonal = () =>
    run(() => api.savePersonal(project.id, personalOf(personal), etag), setPersonalError, t('projects.personal.saved'), personal)
  const removePersonal = () =>
    run(() => api.removePersonal(project.id), setPersonalError, t('projects.personal.removed'), personalDraftOf(null))

  /** A suggested recognizer: opened, new, in the Recognizers tab's panel, to be saved in latet.yaml. */
  const suggest = (entry: RecognizerEntry) => {
    setSuggested((last) => ({ id: (last?.id ?? 0) + 1, draft: toDraft(entry).draft, source: 'repo' }))
    tabs.pick('recognizers')
  }

  /** A list's description in a project: what it is, and where its rows come from. */
  const listDescription = (description: string) => `${description} ${t('projects.sources')}`

  // Each section of the project's rules, by its anchor: the tabs show them, and the search reads their titles and
  // descriptions. Those of your own settings have the titles and the editors of the global rules.
  const sections: Record<ProjectAnchor, RulesSection> = {
    [PROJECT_ANCHORS.effective]: {
      title: t('projects.effective.title'),
      description: t('projects.effective.description'),
      body: <Effective project={project} />,
    },
    [PROJECT_ANCHORS.personal]: {
      title: t('projects.personal.title'),
      description: t(key('projects.personal.description')),
      body: (
        <div className="flex flex-wrap items-center gap-2">
          <RulesTransfer project={project.id} dirty={personalDirty} onImported={onSaved} />
          {project.personal && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void removePersonal()}>
              <Icon name="trash" size={14} />
              {t('projects.personal.remove')}
            </button>
          )}
        </div>
      ),
    },
    [PROJECT_ANCHORS.protection]: {
      title: t('projects.protection.title'),
      description: t('projects.protection.description'),
      body: (
        <div className="space-y-2">
          <Segmented<Protection>
            label={t('projects.protection.title')}
            value={protectionOf(personal)}
            onChange={(next) => setPersonal((d) => setProtection(d, next))}
            options={[
              { value: 'on', label: t('projects.protection.on') },
              { value: 'secrets', label: t('projects.protection.secrets') },
              { value: 'off', label: t('projects.protection.off') },
            ]}
          />
          <p className={`max-w-[80ch] text-meta ${protectionOf(personal) === 'off' ? 'text-crit-ink' : 'text-ink-3'}`}>
            {t(PROTECTION_HELP[protectionOf(personal)])}
          </p>
        </div>
      ),
    },
    [PROJECT_ANCHORS.projectData]: {
      title: t('data.title'),
      description: t('data.description'),
      body: <DataEditor value={personal} set={(key, value) => set(key, value as never)} layer="personal" />,
    },
    [PROJECT_ANCHORS.terms]: {
      title: t('rules.terms.title'),
      description: listDescription(t('rules.terms.description')),
      body: <TermsTable scope={scope} onSaved={onSaved} search={tabs.query} shown={tabs.active === 'terms'} />,
      found: foundIn(t, termsSpec(t, format.number), scope, tabs.query),
      wide: true,
    },
    [PROJECT_ANCHORS.recognizers]: {
      title: t('rules.recognizers.title'),
      description: listDescription(t('rules.recognizers.description')),
      body: <RecognizersTable scope={scope} onSaved={onSaved} incoming={suggested} search={tabs.query} shown={tabs.active === 'recognizers'} />,
      found: foundIn(t, recognizersSpec(t, key, scope), scope, tabs.query),
      wide: true,
    },
    [PROJECT_ANCHORS.allow]: {
      title: t('rules.allow.title'),
      description: listDescription(t('rules.allow.description')),
      body: <AllowedTable scope={scope} onSaved={onSaved} search={tabs.query} shown={tabs.active === 'values'} />,
      found: foundIn(t, allowSpec(t), scope, tabs.query),
      wide: true,
    },
    [PROJECT_ANCHORS.policies]: {
      title: t('rules.policies.title'),
      description: listDescription(t('rules.policies.description')),
      body: <PoliciesTable scope={scope} onSaved={onSaved} search={tabs.query} shown={tabs.active === 'policies'} />,
      found: foundIn(t, policiesSpec(t, scope), scope, tabs.query),
      wide: true,
    },
    [PROJECT_ANCHORS.reviewer]: {
      title: t('projects.reviewer.title'),
      description: t('projects.reviewer.help'),
      body: (
        <div className="space-y-6">
          <ReviewModeChoice value={personal.llm_review} onChange={(mode) => set('llm_review', mode)} />
          {config.llm_review.enabled && (
            <ImagesHere value={personal.review_images} global={config.llm_review.images ?? false} onChange={(images) => set('review_images', images)} />
          )}
          <GuidanceHere value={personal.review_guidance} config={config} onChange={(guidance) => set('review_guidance', guidance)} />
        </div>
      ),
    },
    [PROJECT_ANCHORS.live]: {
      title: t('projects.live.title'),
      description: t('projects.live.help'),
      body: <LiveHere value={personal.live_review} global={config.live_review?.enabled ?? false} onChange={(live) => set('live_review', live)} />,
    },
    [PROJECT_ANCHORS.repo]: {
      title: t('projects.repo.title'),
      description: t('projects.repo.description'),
      body: (
        <fieldset disabled={!canEditRepo || saving} className="space-y-6 disabled:opacity-60">
          <legend className="sr-only">{t('projects.repo.title')}</legend>
          {!canEditRepo && (
            <p className="text-meta text-ink-3">
              {project.repo.readable === false ? t('projects.repo.unreadable') : t(key('projects.repo.readOnly'))}
            </p>
          )}
          <RepoEditors draft={repo} set={setR} config={config} />
          <Suggestions projectId={project.id} onAdd={suggest} />
        </fieldset>
      ),
    },
  }

  // The save bar has a row for each set of settings on screen: yours for the project, wherever a section of them that
  // is no table shows (the overview too, for Import waits for them to be saved); the repository file's, in its own
  // tab, where it can be written.
  const shown = shownSections(PROJECT_TABS, sections, tabs.active, tabs.query)
  const personalRow = BAR_SECTIONS.some((anchor) => shown.has(anchor))
  const repoRow = localRepo && shown.has(PROJECT_ANCHORS.repo)

  // Each list's tab says how many of its rows are yours, and how many apply there in all.
  const countOf = <T,>(rows: RuleRow<T>[]) =>
    t('rules.tabs.countProject', { count: rows.filter((row) => row.source === 'personal').length, all: rows.length })
  const counts = {
    terms: countOf(rowsIn(scope, (source) => termsList<SettingsOf>(source))),
    recognizers: countOf(rowsIn(scope, (source) => recognizersList<SettingsOf>(source))),
    values: countOf(rowsIn(scope, (source) => allowList<SettingsOf>(source), false)),
    policies: countOf(rowsIn(scope, (source) => policiesList<SettingsOf>(source))),
  }
  const lostTabs = formatList(i18n.resolvedLanguage ?? 'en', lost.map((tab) => t(PROJECT_TAB_LABEL[tab])))

  return (
    <>
      <header className="mb-4 rounded-lg border border-line bg-surface p-4">
        <h2 className="text-title font-semibold text-ink">{project.name}</h2>
        <dl className="mt-2 grid grid-cols-[120px_1fr] gap-x-3 gap-y-1 text-meta">
          <dt className="text-ink-3">{t('projects.folder')}</dt>
          <dd className="mono min-w-0 break-all text-ink-2">{project.root}</dd>
          <dt className="text-ink-3">{t('projects.file')}</dt>
          <dd className="mono min-w-0 break-all text-ink-2">
            {project.repo.exists ? project.repo.path : <span className="font-sans text-ink-3">{t(key('projects.noFile'))}</span>}
          </dd>
        </dl>
        {project.notices.length > 0 && <Notices notices={project.notices} />}
      </header>

      {lost.length > 0 && (
        <div role="status" className="mb-4 flex items-start gap-2 rounded-lg border border-warn/60 bg-warn/10 p-3 text-ui text-ink">
          <Icon name="info" size={14} className="mt-0.5 flex-none text-warn-ink" />
          <p className="min-w-0 flex-1">
            {t('rules.lost', { tabs: lostTabs })} <DocsWhyLink target="why.lostProject" />
          </p>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setLost([])}>
            {t('common.dismiss')}
          </button>
        </div>
      )}

      <RulesTabs
        state={tabs}
        tabs={PROJECT_TABS}
        sections={sections}
        docs={PROJECT_DOCS}
        labels={{
          overview: t(PROJECT_TAB_LABEL.overview),
          protection: t(PROJECT_TAB_LABEL.protection),
          terms: t(PROJECT_TAB_LABEL.terms),
          recognizers: t(PROJECT_TAB_LABEL.recognizers),
          values: t(PROJECT_TAB_LABEL.values),
          policies: t(PROJECT_TAB_LABEL.policies),
          reviewers: t(PROJECT_TAB_LABEL.reviewers),
          repo: t(PROJECT_TAB_LABEL.repo),
        }}
        counts={counts}
        dirty={projectDirtyTabs(personalBase, personal, repoDirty)}
        idPrefix={TABS_ID}
        disabled={saving}
      />

      {(personalRow || repoRow) && (
        <SaveBar>
          {personalRow && (
            <SaveRow
              dirty={personalDirty}
              saving={saving}
              error={personalError}
              saveLabel={t('projects.personal.save')}
              onDiscard={() => setPersonal(personalBase)}
              onSave={() => void savePersonal()}
            />
          )}
          {repoRow && (
            <SaveRow
              dirty={repoDirty}
              saving={saving}
              error={repoError}
              conflict={t('projects.repo.changed')}
              saveLabel={project.repo.exists ? t('projects.repo.save') : t('projects.repo.create')}
              onDiscard={() => setRepo(repoBase)}
              onSave={() => void saveRepo()}
              disabled={!canEditRepo}
              force={!project.repo.exists}
            />
          )}
        </SaveBar>
      )}
    </>
  )
}

type LiveChoice = 'inherit' | 'on' | 'off'

/** The Live Reviewer in this project: as in the global settings, or on or off here. Yours alone. */
function LiveHere({ value, global, onChange }: { value: boolean | null; global: boolean; onChange: (value: boolean | null) => void }) {
  const { t } = useTranslation()
  const choice: LiveChoice = value === null ? 'inherit' : value ? 'on' : 'off'
  return (
    <Segmented<LiveChoice>
      label={t('projects.live.title')}
      value={choice}
      onChange={(v) => onChange(v === 'inherit' ? null : v === 'on')}
      options={[
        { value: 'inherit', label: global ? t('projects.live.inheritOn') : t('projects.live.inheritOff') },
        { value: 'on', label: t('projects.live.on') },
        { value: 'off', label: t('projects.live.off') },
      ]}
    />
  )
}

/** How much the local reviewer reads in this project: as elsewhere, or (yours alone) off; a repository file, only more. */
function ReviewModeChoice({ value, onChange, repo = false }: { value: ReviewMode | null; onChange: (value: ReviewMode | null) => void; repo?: boolean }) {
  const { t } = useTranslation()
  return (
    <Segmented<'unset' | ReviewMode>
      label={t('projects.reviewer.title')}
      value={value ?? 'unset'}
      onChange={(v) => onChange(v === 'unset' ? null : v)}
      options={[
        { value: 'unset', label: t('data.unset') },
        ...(repo ? [] : [{ value: 'off' as const, label: t(REVIEW_MODE_LABEL.off) }]),
        { value: 'background', label: t(REVIEW_MODE_LABEL.background) },
        { value: 'inline', label: t(REVIEW_MODE_LABEL.inline) },
      ]}
    />
  )
}

/** Whether the local reviewer is shown the images in this project. Yours alone. */
function ImagesHere({ value, global, onChange }: { value: boolean | null; global: boolean; onChange: (value: boolean | null) => void }) {
  const { t } = useTranslation()
  const choice: LiveChoice = value === null ? 'inherit' : value ? 'on' : 'off'
  return (
    <div>
      <h3 className="text-ui font-medium text-ink">{t('projects.reviewImages.title')}</h3>
      <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('projects.reviewImages.help')}</p>
      <Segmented<LiveChoice>
        label={t('projects.reviewImages.title')}
        value={choice}
        onChange={(v) => onChange(v === 'inherit' ? null : v === 'on')}
        options={[
          { value: 'inherit', label: global ? t('projects.reviewImages.inheritOn') : t('projects.reviewImages.inheritOff') },
          { value: 'on', label: t('projects.reviewImages.on') },
          { value: 'off', label: t('projects.reviewImages.off') },
        ]}
      />
    </div>
  )
}

/** Your guidance to the local reviewer in this project, after the global one. Yours alone. */
function GuidanceHere({ value, config, onChange }: { value: string | null; config: ConfigView; onChange: (guidance: string | null) => void }) {
  const { t } = useTranslation()
  const review = config.llm_review
  const global = cleanGuidance(review.guidance)
  return (
    <div className="space-y-2">
      <GuidanceField
        title={t('reviewer.guidance.projectTitle')}
        help={t('reviewer.guidance.projectHelp')}
        placeholder={t('reviewer.guidance.projectPlaceholder')}
        value={value}
        onChange={onChange}
      />
      {global && (
        <div className="max-w-[80ch]">
          <p className="mb-1 text-meta text-ink-3">{t('reviewer.guidance.projectAfter')}</p>
          <p className="well code max-h-[160px] overflow-auto px-3 py-2 text-ink-2">{global}</p>
        </div>
      )}
      <ReviewerInstructions guidance={joinGuidance(global, value)} />
      {review.enabled ? (
        <TryReviewer settings={review} projectGuidance={value} />
      ) : (
        <p className="max-w-[80ch] text-meta text-ink-3">{t('reviewer.guidance.reviewerOff')}</p>
      )}
    </div>
  )
}

function Notices({ notices }: { notices: ProjectNotice[] }) {
  const { t } = useTranslation()
  return (
    <>
      <ul className="mt-3 space-y-1.5" aria-label={t('projects.notices')}>
        {notices.map((n, i) => {
          const reason = noticeReason(n) // one the daemon gives a reason for: in your language, not in its English
          const message = reason !== null ? t(reason) : n.message
          return (
            <li key={i} className={`flex items-start gap-2 text-meta ${n.level === 'error' ? 'text-crit-ink' : 'text-ink-2'}`}>
              <Icon name={n.level === 'error' ? 'x' : 'info'} size={13} className={`mt-0.5 flex-none ${n.level === 'error' ? '' : 'text-warn'}`} />
              <span>
                <span className="font-medium">{n.source === 'repo' ? t('projects.fromRepo') : t('projects.fromPersonal')}</span>
                {n.key && <code className="mono mx-1">{n.key}</code>}
                {n.level === 'ignored' ? t('projects.ignored', { message }) : t('projects.error', { message })}
              </span>
            </li>
          )
        })}
      </ul>
      {/* What a project file can set, and what it cannot. */}
      <p className="mt-1.5 text-meta">
        <DocsWhyLink target="why.projectNotices" />
      </p>
    </>
  )
}

function Effective({ project }: { project: ProjectDetail }) {
  const { t } = useTranslation()
  const e = project.effective
  const rows: [string, string][] = [
    [t('projects.effective.profile'), t(PROFILE_LABEL[e.profile])],
    [t('projects.effective.pii'), t('projects.effective.types', { count: e.pii.length })],
    [t('projects.effective.countries'), e.id_regions.join(', ') || t('common.none')],
    [t('projects.effective.terms'), String(e.terms)],
    [t('projects.effective.recognizers'), String(e.recognizers)],
    [t('projects.effective.exceptions'), String(e.allow_values + e.allow_patterns)],
    [
      t('projects.effective.policies'),
      Object.entries(e.policies)
        .map(([type, policy]) => `${type} ${policy}`)
        .join(', ') || t('common.none'),
    ],
    [t('projects.effective.data'), e.data === 'strict' ? t('projects.effective.strict') : t('projects.effective.normal')],
    [t('projects.effective.fields'), t('projects.effective.fieldCount', { count: e.fields.length })],
    [t('projects.effective.dumpGuard'), e.dump_guard === null ? t('common.none') : String(e.dump_guard)],
    [
      t('projects.effective.images'),
      e.images === 'block'
        ? t('projects.effective.imagesBlocked')
        : e.images === 'mask'
          ? t('projects.effective.imagesMasked')
          : t('projects.effective.imagesSent'),
    ],
    [t('projects.effective.reviewer'), t(REVIEW_MODE_LABEL[e.llm_review ?? 'off'])],
  ]
  return (
    <dl className="grid grid-cols-[160px_1fr] gap-x-4 gap-y-1.5 rounded-lg border border-line bg-surface p-3 text-ui">
      {!project.enabled && (
        <p className="col-span-2 mb-1 flex items-center gap-1.5 font-medium text-crit-ink">
          <Icon name="eyeOff" size={14} />
          {t('projects.effective.off')}
        </p>
      )}
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-ink-3">{label}</dt>
          <dd className="min-w-0 break-words text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * What the repository file sets, and only what it may, but its lists (terms, recognizers, policies), whose rows show in
 * their tables as latet.yaml's: the values never to mask, the tools strict data mode covers, fields kept in clear, the
 * Live Reviewer and the guidance to the local reviewer are each person's alone.
 */
function RepoEditors({
  draft,
  set,
  config,
}: {
  draft: SettingsDraft
  set: <K extends keyof SettingsDraft>(key: K, value: SettingsDraft[K]) => void
  config: ConfigView
}) {
  const { t } = useTranslation()
  return (
    <>
      <div className="space-y-4">
        <div>
          <h3 className="text-ui font-medium text-ink">{t('projects.repo.profile')}</h3>
          <p className="mb-2 text-meta text-ink-3">{t('projects.repo.profileHelp')}</p>
          <Segmented<'unset' | 'balanced' | 'max'>
            label={t('projects.repo.profile')}
            value={draft.profile === 'balanced' || draft.profile === 'max' ? draft.profile : 'unset'}
            onChange={(v) => set('profile', v === 'unset' ? null : v)}
            options={[
              { value: 'unset', label: t('projects.repo.profileUnset') },
              { value: 'balanced', label: t(PROFILE_LABEL.balanced) },
              { value: 'max', label: t(PROFILE_LABEL.max) },
            ]}
          />
        </div>
        <div>
          <h3 className="text-ui font-medium text-ink">{t('projects.repo.countries')}</h3>
          <p className="mb-2 text-meta text-ink-3">{t('projects.repo.countriesHelp')}</p>
          <ul className="flex flex-wrap gap-1.5">
            {config.id_regions_available.map((code) => (
              <li key={code}>
                <label className={`tag h-7 cursor-pointer gap-1.5 px-2 text-meta ${draft.id_regions.includes(code) ? 'border-ink text-ink' : 'text-ink-3'}`}>
                  <input
                    type="checkbox"
                    className="accent-[var(--ink)]"
                    checked={draft.id_regions.includes(code)}
                    onChange={(e) => set('id_regions', e.target.checked ? [...draft.id_regions, code] : draft.id_regions.filter((c) => c !== code))}
                  />
                  <span className="mono">{code}</span>
                </label>
              </li>
            ))}
          </ul>
        </div>
        <div className="flex flex-wrap gap-6">
          <Switch
            checked={draft.include_private_ips === true}
            onChange={(on) => set('include_private_ips', on ? true : null)}
            label={t('projects.repo.privateIps')}
            description={t('projects.repo.privateIpsHelp')}
          />
          <Switch
            checked={draft.ner === 'auto'}
            onChange={(on) => set('ner', on ? 'auto' : null)}
            label={t('projects.repo.names')}
            description={t('projects.repo.namesHelp')}
          />
        </div>
      </div>
      <div>
        <h3 className="text-ui font-medium text-ink">{t('projects.reviewer.title')}</h3>
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">{t('projects.reviewer.repoHelp')}</p>
        <ReviewModeChoice value={draft.llm_review} onChange={(mode) => set('llm_review', mode)} repo />
      </div>
      <div>
        <h3 className="text-ui font-medium text-ink">{t('data.title')}</h3>
        <p className="mb-3 max-w-[80ch] text-meta text-ink-3">{t('data.description')}</p>
        <DataEditor value={draft} set={(key, value) => set(key, value as never)} layer="repo" />
      </div>
    </>
  )
}

/** Suggest recognizers: drafted from the project's files, each opened in the Recognizers tab's panel, latet.yaml chosen. */
function Suggestions({ projectId, onAdd }: { projectId: string; onAdd: (entry: RecognizerEntry) => void }) {
  const { t } = useTranslation()
  const { me, unreachable } = useMe()
  const [result, setResult] = useState<SuggestResult | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  if (me === undefined || me.server) {
    return <p className="text-meta text-ink-3">{me === undefined ? t(pendingKey(unreachable)) : t('projects.suggest.unavailableServer')}</p>
  }
  const look = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.suggestProject(projectId))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 flex-1 text-meta text-ink-2">{t('projects.suggest.help')}</p>
        <button type="button" className="btn" onClick={() => void look()} disabled={busy}>
          <Icon name="search" size={14} />
          {busy ? t('common.loading') : t('projects.suggest.run')}
        </button>
      </div>
      {error !== null && <p className="mt-2 text-meta text-crit-ink">{errorText(t, error)}</p>}
      {result && (
        <div className="mt-3">
          <p className="mb-2 text-meta text-ink-3">{t('projects.suggest.looked', { count: result.files })}</p>
          {result.drafts.length === 0 ? (
            <p className="text-meta text-ink-3">{t('projects.suggest.none')}</p>
          ) : (
            <ul className="divide-y divide-line">
              {result.drafts.map((draft, i) => (
                <li key={i} className="flex flex-wrap items-center justify-between gap-3 py-2">
                  <span className="min-w-0">
                    <code className="mono text-ui text-ink">{String(draft.name ?? '')}</code>
                    <span className="mono ml-2 break-all text-meta text-ink-3">{String(draft.pattern ?? '')}</span>
                  </span>
                  <button type="button" className="btn btn-sm" onClick={() => onAdd(draft)}>
                    <Icon name="plus" size={13} />
                    {t('projects.suggest.add')}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
