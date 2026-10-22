import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import type {
  AutoModeReview,
  ConfigView,
  HistoryMode,
  Profile,
  ReviewChoice,
  ReviewDetail,
} from '../api/types'
import { ClearHistoryButton } from '../components/ClearHistory'
import { DocsWhyLink, TitleWithDocs } from '../components/DocsLinks'
import { RULES_DOCS } from '../lib/docsPlaces'
import { RulesTransfer } from '../components/RulesTransfer'
import { ScopeBar } from '../components/ScopeBar'
import { PendingChangeNotice } from '../components/SettingsChange'
import { ProjectRules } from './ProjectRules'
import { Icon } from '../components/Icon'
import { LeaveGuard } from '../components/LeaveGuard'
import { useToast } from '../components/Toast'
import { CommandLine, ErrorState, InlineCodeText, Loading, PageHeader, Segmented, Switch } from '../components/ui'
import i18n, { tk } from '../i18n'
import { CMD_APPLY_CLAUDE, CMD_NER_EXTRA, CMD_NER_INSTALL, CMD_OCR_EXTRA, CMD_OCR_INSTALL } from '../lib/commands'
import { pendingKey } from '../lib/deployment'
import { PROFILE_HELP, PROFILE_LABEL, PROFILES } from '../lib/labels'
import { GuidanceField, ReviewerEditor, ReviewerInstructions } from '../components/ReviewerEditor'
import { LiveReviewEditor } from '../components/LiveReviewEditor'
import { ANCHORS, DataEditor, type DataDraft } from '../components/rules'
import { allowSpec, AllowedTable } from '../components/rules/allowed'
import { PoliciesTable, policiesSpec } from '../components/rules/criteria'
import type { RulesScope } from '../components/rules/editing'
import { RecognizersTable, recognizersSpec, type Drafting } from '../components/rules/recognizers'
import { foundIn } from '../components/rules/RuleList'
import { TermsTable, termsSpec } from '../components/rules/terms'
import { formatList } from '../lib/format'
import { useFormat } from '../lib/useFormat'
import { RulesTabs, shownSections, TAB_LABEL, useRulesTabs, type RulesSection } from '../components/RulesTabs'
import { SaveBar, SaveRow } from '../components/SaveBar'
import { dirtyTabs, FIELD_SECTIONS, lostAfter, RULES_TABS, SAVE_BAR_TABS, tabOf, type RulesAnchor, type RulesTab } from '../lib/rulesTabs'
import { StringList } from '../components/StringList'
import { useDeploymentKey, useMe } from '../lib/me'
import { keepUnsaved } from '../lib/panelSave'
import { defaultPii, groupByCountry } from '../lib/pii'
import { savedChangePending, stale } from '../lib/refused'
import { editable, remadeFrom, type Editable } from '../lib/saves'
import { identityOffered, reviewerAskable, reviewerOffered } from '../lib/tutorial'
import { useUnsaved } from '../lib/unsaved'
import { useAsync } from '../lib/useAsync'

const PII_HELP: Record<string, string> = {
  EMAIL: tk('rules.pii.types.EMAIL'),
  PHONE: tk('rules.pii.types.PHONE'),
  IBAN: tk('rules.pii.types.IBAN'),
  CREDIT_CARD: tk('rules.pii.types.CREDIT_CARD'),
  IP_ADDRESS: tk('rules.pii.types.IP_ADDRESS'),
  CRYPTO_WALLET: tk('rules.pii.types.CRYPTO_WALLET'),
  VAT_NUMBER: tk('rules.pii.types.VAT_NUMBER'),
  IT_FISCAL_CODE: tk('rules.pii.types.IT_FISCAL_CODE'),
  IT_VAT_CODE: tk('rules.pii.types.IT_VAT_CODE'),
  ES_DNI: tk('rules.pii.types.ES_DNI'),
  ES_NIE: tk('rules.pii.types.ES_NIE'),
  FR_NIR: tk('rules.pii.types.FR_NIR'),
  DE_TAX_ID: tk('rules.pii.types.DE_TAX_ID'),
  NL_BSN: tk('rules.pii.types.NL_BSN'),
  BE_NATIONAL_NUMBER: tk('rules.pii.types.BE_NATIONAL_NUMBER'),
  PT_NIF: tk('rules.pii.types.PT_NIF'),
  PL_PESEL: tk('rules.pii.types.PL_PESEL'),
  CH_AHV: tk('rules.pii.types.CH_AHV'),
  SE_PERSONNUMMER: tk('rules.pii.types.SE_PERSONNUMMER'),
  FI_HETU: tk('rules.pii.types.FI_HETU'),
  GB_NINO: tk('rules.pii.types.GB_NINO'),
  US_SSN: tk('rules.pii.types.US_SSN'),
  US_ITIN: tk('rules.pii.types.US_ITIN'),
  CA_SIN: tk('rules.pii.types.CA_SIN'),
  LV_PERSONAL_CODE: tk('rules.pii.types.LV_PERSONAL_CODE'),
}

/** The section each setting is edited in, for the dot on a tab with unsaved changes: it must name every setting. */
const SECTION_OF: { readonly [K in keyof Editable]: RulesAnchor } = FIELD_SECTIONS

/** The ids of the tabs of Rules and of their panels (`tabIds`). */
const TABS_ID = 'rules'

type ReviewMode = 'block' | 'allow' | 'choose'

const REVIEW_MODES: { value: ReviewMode; label: string; help: string }[] = [
  { value: 'block', label: tk('rules.review.block'), help: tk('rules.review.blockHelp') },
  { value: 'allow', label: tk('rules.review.allow'), help: tk('rules.review.allowHelp') },
  { value: 'choose', label: tk('rules.review.choose'), help: tk('rules.review.chooseHelp') },
]

const REVIEW_DETAILS: { value: ReviewDetail; label: string; help: string }[] = [
  { value: 'user_name', label: tk('rules.review.details.user_name'), help: tk('rules.review.details.user_nameHelp') },
  { value: 'paths', label: tk('rules.review.details.paths'), help: tk('rules.review.details.pathsHelp') },
  { value: 'host_name', label: tk('rules.review.details.host_name'), help: tk('rules.review.details.host_nameHelp') },
  { value: 'git_author', label: tk('rules.review.details.git_author'), help: tk('rules.review.details.git_authorHelp') },
]

const everyDetail = (choice: ReviewChoice): AutoModeReview => ({
  user_name: choice,
  paths: choice,
  host_name: choice,
  git_author: choice,
})

function reviewMode(review: AutoModeReview): ReviewMode {
  const choices = new Set(REVIEW_DETAILS.map((d) => review[d.value]))
  if (choices.size === 1 && choices.has('block')) return 'block'
  if (choices.size === 1 && choices.has('allow')) return 'allow'
  return 'choose'
}

/** Auto mode's reviewer: every detail blocked, every detail allowed, or one by one. */
function ReviewSettings({ value, onChange }: { value: AutoModeReview; onChange: (value: AutoModeReview) => void }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  // "Choose" stays open while the choices happen to be all one way.
  const [choosing, setChoosing] = useState(() => reviewMode(value) === 'choose')
  const mode = choosing ? 'choose' : reviewMode(value)
  const pick = (next: ReviewMode) => {
    setChoosing(next === 'choose')
    if (next !== 'choose') onChange(everyDetail(next))
  }
  const choiceOptions = (detail: ReviewDetail) => [
    { value: 'block' as const, label: t('rules.review.choice.block') },
    { value: 'allow' as const, label: t('rules.review.choice.allow') },
    ...(detail === 'user_name' ? [{ value: 'surrogate' as const, label: t('rules.review.choice.surrogate') }] : []),
  ]
  return (
    <>
      <div role="radiogroup" aria-label={t('rules.review.title')} className="mt-3 space-y-2.5">
        {REVIEW_MODES.map((option) => (
          <label key={option.value} className="flex cursor-pointer items-start gap-2.5">
            <input
              type="radio"
              name="auto_mode_review"
              value={option.value}
              checked={mode === option.value}
              onChange={() => pick(option.value)}
              className="mt-0.5 accent-[var(--ink)]"
            />
            <span className="max-w-[80ch]">
              <span className="block text-ui text-ink">{t(key(option.label))}</span>
              <span className="block text-meta text-ink-3">{t(key(option.help))}</span>
            </span>
          </label>
        ))}
      </div>
      {mode === 'choose' && (
        <ul className="mt-3 max-w-[720px] divide-y divide-line rounded-lg border border-line bg-surface">
          {REVIEW_DETAILS.map((detail) => (
            <li key={detail.value} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 px-3 py-2.5">
              <span className="min-w-0 flex-1">
                <span className="block text-ui text-ink">{t(detail.label)}</span>
                <span className="block text-meta text-ink-3">{t(key(detail.help))}</span>
              </span>
              <Segmented
                size="sm"
                label={t(detail.label)}
                value={value[detail.value]}
                options={choiceOptions(detail.value)}
                onChange={(choice) => onChange({ ...value, [detail.value]: choice })}
              />
            </li>
          ))}
        </ul>
      )}
      {value.user_name === 'surrogate' && (
        <div className="mt-3 max-w-[720px] space-y-2 rounded-lg border border-line bg-surface p-3">
          <p className="text-ui text-ink">{t('rules.review.surrogate')}</p>
          <CommandLine command={CMD_APPLY_CLAUDE} />
          <p className="text-meta text-ink-3">{t('rules.review.surrogateNext')}</p>
        </div>
      )}
    </>
  )
}

/** Personal data: the countries whose ID numbers to recognise, then the types, grouped by country. */
function PersonalData({
  config,
  draft,
  set,
}: {
  config: ConfigView
  draft: Editable
  set: <K extends keyof Editable>(key: K, value: Editable[K]) => void
}) {
  const { t, i18n } = useTranslation()
  const language = i18n.resolvedLanguage ?? i18n.language ?? 'en'
  const regionNames = useMemo(() => {
    try {
      return new Intl.DisplayNames([language], { type: 'region' })
    } catch {
      return null
    }
  }, [language])
  const countryName = (code: string) => regionNames?.of(code) ?? code
  const secretsOnly = draft.profile === 'secrets'
  const everyRegion = draft.profile === 'max'
  const catalog = config.pii_catalog ?? config.pii_available.map((type) => ({ type, country: null }))
  const available = config.id_regions_available ?? []
  const defaults = defaultPii(catalog, config.pii_base ?? config.pii_available, draft.id_regions, everyRegion)
  const useDefault = draft.pii === null
  const enabled = draft.pii ?? defaults
  // By default only what is in use; when choosing, every country.
  const shown = useDefault ? (everyRegion ? available : draft.id_regions) : available
  const groups = groupByCountry(catalog, [...available].sort((a, b) => countryName(a).localeCompare(countryName(b), language)))
    .filter((group) => group.country === null || shown.includes(group.country))
  const toggleRegion = (code: string, on: boolean) =>
    set('id_regions', on ? [...draft.id_regions, code] : draft.id_regions.filter((c) => c !== code))

  return (
    <fieldset disabled={secretsOnly} className="space-y-5 disabled:opacity-55">
      <legend className="sr-only">{t('rules.pii.title')}</legend>
      <div>
        <h3 className="text-ui font-medium text-ink">{t('rules.pii.countries')}</h3>
        <p className="mb-2 max-w-[80ch] text-meta text-ink-3">
          {everyRegion ? t('rules.pii.countriesMax') : t('rules.pii.countriesHelp')}
        </p>
        <ul className="flex flex-wrap gap-1.5">
          {[...available]
            .sort((a, b) => countryName(a).localeCompare(countryName(b), language))
            .map((code) => {
              const on = everyRegion || draft.id_regions.includes(code)
              return (
                <li key={code}>
                  <label
                    className={`tag h-7 cursor-pointer gap-1.5 px-2 text-meta ${on ? 'border-ink text-ink' : 'text-ink-3'} ${
                      everyRegion ? 'cursor-not-allowed' : ''
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="accent-[var(--ink)]"
                      checked={on}
                      disabled={everyRegion || secretsOnly}
                      onChange={(e) => toggleRegion(code, e.target.checked)}
                    />
                    {countryName(code)}
                    <span className="mono text-micro text-ink-3">{code}</span>
                  </label>
                </li>
              )
            })}
        </ul>
      </div>
      <Switch
        checked={useDefault}
        onChange={(checked) => set('pii', checked ? null : [...defaults])}
        label={t('rules.pii.useDefault')}
        description={t('rules.pii.useDefaultHelp')}
        disabled={secretsOnly}
      />
      <div className="space-y-4">
        {groups.map((group) => (
          <div key={group.country ?? 'general'}>
            <h4 className="label mb-1.5">{group.country === null ? t('rules.pii.general') : countryName(group.country)}</h4>
            <ul className="grid grid-cols-2 gap-x-6 gap-y-2 max-md:grid-cols-1">
              {group.types.map((type) => (
                <li key={type}>
                  <label className={`flex items-start gap-2.5 ${useDefault ? 'opacity-60' : ''}`}>
                    <input
                      type="checkbox"
                      className="mt-0.5 accent-[var(--ink)]"
                      checked={enabled.includes(type)}
                      disabled={useDefault || secretsOnly}
                      onChange={(e) => set('pii', e.target.checked ? [...enabled, type] : enabled.filter((x) => x !== type))}
                    />
                    <span>
                      <span className="mono block text-ui text-ink">{type}</span>
                      {PII_HELP[type] && <span className="block text-meta text-ink-3">{t(PII_HELP[type])}</span>}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </fieldset>
  )
}

/** Whether the text in images is read, and the one command that moves it forward. */
function OcrStatusNote({
  config,
  draft,
  set,
}: {
  config: ConfigView
  draft: Editable
  set: <K extends keyof Editable>(key: K, value: Editable[K]) => void
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { me, unreachable } = useMe()
  const status = config.ocr_status
  if (!status || draft.images === 'block' || (draft.profile === 'max' && draft.images !== 'mask')) return null
  if (!status.runtime || !status.model) {
    if (me === undefined || me.server) {
      return <p className="mt-3 text-ui text-ink-3">{me === undefined ? t(pendingKey(unreachable)) : t('deployment.ocrUnavailable')}</p>
    }
    return (
      <div className="mt-3 max-w-[560px] space-y-2 rounded-lg border border-line bg-surface p-3">
        <p className="text-ui text-ink">{status.runtime ? t('rules.ocr.noModel') : t('rules.ocr.noRuntime')}</p>
        <CommandLine command={status.runtime ? CMD_OCR_INSTALL : CMD_OCR_EXTRA} />
        {!status.runtime && <p className="text-meta text-ink-3">{t('rules.ocr.noRuntimeNext')}</p>}
      </div>
    )
  }
  return (
    <div className="mt-3 max-w-[640px]">
      <Switch
        checked={draft.ocr === 'auto'}
        onChange={(on) => set('ocr', on ? 'auto' : 'off')}
        label={t('rules.ocr.toggle')}
        description={draft.ocr === 'auto' ? t(key('rules.ocr.on')) : t('rules.ocr.off')}
      />
    </div>
  )
}

/** Where local name detection stands, and the one command that moves it forward. */
function NerStatusNote({ config, draft, dirty }: { config: ConfigView; draft: Editable; dirty: boolean }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { me, unreachable } = useMe()
  const status = config.ner_status
  if (draft.ner === 'off') return <p className="text-ui text-ink-3">{t('rules.ner.off')}</p>
  if (draft.profile === 'secrets') return <p className="text-ui text-ink-3">{t('rules.ner.secretsOnly')}</p>
  if (!status) return null
  if ((!status.runtime || !status.model) && (me === undefined || me.server)) {
    return <p className="text-ui text-ink-3">{me === undefined ? t(pendingKey(unreachable)) : t('deployment.nerUnavailable')}</p>
  }
  if (!status.runtime) {
    return (
      <div className="max-w-[560px] space-y-2 rounded-lg border border-line bg-surface p-3">
        <p className="text-ui text-ink">{t('rules.ner.noRuntime')}</p>
        <CommandLine command={CMD_NER_EXTRA} />
        <p className="text-meta text-ink-3">{t('rules.ner.noRuntimeNext')}</p>
      </div>
    )
  }
  if (!status.model) {
    return (
      <div className="max-w-[560px] space-y-2 rounded-lg border border-line bg-surface p-3">
        <p className="text-ui text-ink">{t('rules.ner.noModel')}</p>
        <CommandLine command={CMD_NER_INSTALL} />
      </div>
    )
  }
  if (status.active && !dirty) {
    return (
      <div className="max-w-[640px] space-y-1.5 rounded-lg border border-line bg-surface p-3 text-ui">
        <p className="flex items-center gap-1.5 font-medium text-good-ink">
          <Icon name="check" size={14} />
          {t('rules.ner.active')}
        </p>
        <p className={draft.profile === 'balanced' ? 'text-ink' : 'text-ink-3'}>{t('rules.ner.balanced')}</p>
        <p className={draft.profile === 'max' ? 'text-ink' : 'text-ink-3'}>{t('rules.ner.max')}</p>
      </div>
    )
  }
  return (
    <p className="text-ui text-ink-2">
      <InlineCodeText text={dirty ? t('rules.ner.onSave') : t(key('rules.ner.inactive'))} />
    </p>
  )
}

const HISTORY_MODES: { value: HistoryMode; label: string; help: string }[] = [
  { value: 'full', label: tk('rules.history.full'), help: tk('rules.history.fullHelp') },
  { value: 'metadata', label: tk('rules.history.metadata'), help: tk('rules.history.metadataHelp') },
  { value: 'off', label: tk('rules.history.off'), help: tk('rules.history.offHelp') },
]

export function RulesPage() {
  const { t } = useTranslation()
  const [params] = useSearchParams()
  const projectId = params.get('project')
  if (projectId) {
    return (
      <>
        <PageHeader title={t('rules.title')} description={t('projects.description')} docs="page.projectRules" />
        <ScopeBar current={projectId} />
        {/* A project of its own: what another project's form said (a save refused) stays with that one. */}
        <ProjectRules key={projectId} id={projectId} />
        <LeaveGuard />
      </>
    )
  }
  return (
    <>
      <GlobalRules />
      <LeaveGuard />
    </>
  )
}

function GlobalRules() {
  const { t } = useTranslation()
  const config = useAsync('rules', (signal) => api.config(signal))
  const header = (actions?: ReactNode) => (
    <>
      <PageHeader title={t('rules.title')} description={t('rules.description')} actions={actions} docs="page.rules" />
      <ScopeBar current={null} />
      <PendingChangeNotice />
    </>
  )
  if (config.loading) return <>{header()}<Loading /></>
  if (!config.data) return <>{header()}<ErrorState error={config.error} onRetry={config.reload} /></>
  return <RulesForm config={config.data} onSaved={config.reload} header={header} />
}

function RulesForm({
  config,
  onSaved,
  header,
}: {
  config: ConfigView
  onSaved: () => void
  header: (actions?: ReactNode) => ReactNode
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const toast = useToast()
  const { me, reload: reloadMe, reloadConfig } = useMe()
  const [base, setBase] = useState<Editable>(() => editable(config))
  const [draft, setDraft] = useState<Editable>(() => editable(config))
  // Of the GET /config the form was made from: the etag a save sends back. Made again from a fresh read after a save,
  // an import, and a save refused because the settings changed meanwhile.
  const [etag, setEtag] = useState(config.etag)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<unknown>(null)
  const [version, setVersion] = useState<number | string | null>(null)
  // The draft as last shown, which a save that comes back compares with what it sent (`remadeFrom`).
  const latest = useRef(draft)
  useEffect(() => {
    latest.current = draft
  }, [draft])
  // The settings as last read, which the tables show: a panel saves its change on them as they are now.
  const [saved, setSaved] = useState(config)
  const savedBase = useRef(base)
  useEffect(() => {
    savedBase.current = base
  }, [base])
  // The tabs whose unsaved edits a panel's save could not keep: their settings changed meanwhile.
  const [lost, setLost] = useState<readonly RulesTab[]>([])
  const dirty = JSON.stringify(base) !== JSON.stringify(draft)
  useUnsaved('rules', dirty)
  const set = <K extends keyof Editable>(key: K, value: Editable[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const setData = <K extends keyof DataDraft>(key: K, value: DataDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value ?? (key === 'images' ? 'allow' : value) }))

  /** The form made again from `fresh`: what it shows, and the etag a save sends back. */
  const remake = (fresh: ConfigView) => {
    setBase(editable(fresh))
    setDraft(editable(fresh))
    setEtag(fresh.etag)
    setSaved(fresh)
    setLost([])
  }

  // A panel saved a row, or an action changed the rows selected: the settings are read again, and the save bar's edits
  // not saved yet are carried onto them where nothing they touch changed meanwhile; the others are dropped, and said.
  const afterPanelSave = () =>
    void api.config().then(
      (fresh) => {
        const now = editable(fresh)
        const { kept, lost: dropped } = keepUnsaved(savedBase.current, latest.current, now, Object.keys(SECTION_OF) as (keyof Editable)[])
        setBase(now)
        setDraft(kept)
        setEtag(fresh.etag)
        setSaved(fresh)
        setLost((said) => lostAfter(said, dropped.flatMap((field) => (SECTION_OF[field] ? [tabOf(SECTION_OF[field])] : []))))
        reloadMe()
        reloadConfig()
        onSaved()
      },
      () => undefined,
    )

  // The tab shown is the one that holds the section the address names (`/rules#reviewer`), else Protection.
  const tabs = useRulesTabs(RULES_TABS)

  const secretsOnly = draft.profile === 'secrets'
  // The tables show the global rules as last read; while the search at the top holds text, each says how many of its
  // rows it finds, and its section shows when any.
  const scope: RulesScope = { kind: 'global', settings: saved }
  const format = useFormat()
  // A draft asks the local reviewer: the tutorial's sandbox refuses it, and cannot turn the reviewer on.
  const drafting: Drafting = reviewerAskable(me) ? (saved.llm_review?.enabled ? 'on' : 'off') : null

  const save = async () => {
    setSaving(true)
    setSaveError(null)
    const sent = draft
    try {
      const result = await api.saveConfig(sent, etag)
      setBase(sent)
      setVersion(result.rules_version)
      toast.show(t('rules.saved', { version: result.rules_version }))
      // Made again from the settings as they are now, with their etag, while the form is what it sent (its editors are
      // off while a save runs). Otherwise, or when they cannot be read now, it keeps the etag it was made from, which
      // this save made old: its next save is refused as stale and the form made again, so that a draft never meets a
      // newer etag (a value the administrators stopped letting through meanwhile is never saved back as hers).
      const fresh = remadeFrom(await api.config().catch(() => null), latest.current, sent)
      if (fresh !== null) remake(fresh)
      reloadMe()
      reloadConfig()
      onSaved()
    } catch (error) {
      setSaveError(error)
      if (savedChangePending(error)) {
        // The submitted draft is durable. Keep its notice, and keep the old etag if the read fails or the draft changed.
        setBase(sent)
        const fresh = remadeFrom(await api.config().catch(() => null), latest.current, sent)
        if (fresh !== null) remake(fresh)
        reloadMe()
        reloadConfig()
        onSaved()
      } else if (stale(error)) {
        // Your settings changed since the page read them (another tab, a change applied meanwhile): nothing was saved,
        // and the form shows them as they are now, for the change to be made again.
        await api.config().then((fresh) => remake(fresh), () => undefined)
        reloadConfig()
        onSaved()
      }
    } finally {
      setSaving(false)
    }
  }

  // Rules imported from a file: the form starts again from the settings as they are now.
  const imported = () =>
    void api.config().then((fresh) => {
      remake(fresh)
      reloadMe()
      reloadConfig()
      onSaved()
    }, () => undefined)

  // Each section of the rules, by its anchor: the tabs show them, and the search reads their titles and descriptions.
  const sections: Record<RulesAnchor, RulesSection> = {
    [ANCHORS.profile]: {
      title: t('rules.profile.title'),
      description: t('rules.profile.description'),
      body: (
        <div role="radiogroup" aria-label={t('rules.profile.title')} className="grid grid-cols-3 gap-2 max-xl:grid-cols-1">
          {PROFILES.map((profile: Profile) => {
            const checked = draft.profile === profile
            return (
              <label
                key={profile}
                className={`relative flex cursor-pointer flex-col gap-1 rounded-lg border p-3 ${
                  checked ? 'border-ink bg-surface shadow-[inset_0_0_0_1px_var(--ink)]' : 'border-line bg-surface hover:border-line-2'
                }`}
              >
                <input type="radio" name="profile" value={profile} checked={checked} onChange={() => set('profile', profile)} className="peer sr-only" />
                <span className="pointer-events-none absolute inset-0 rounded-lg peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[var(--focus)]" />
                <span className="flex items-center justify-between gap-2">
                  <span className="text-ui font-semibold text-ink">{t(PROFILE_LABEL[profile])}</span>
                  {checked && <Icon name="check" size={15} className="text-ink" />}
                </span>
                <span className="text-meta text-ink-2">{t(key(PROFILE_HELP[profile]))}</span>
              </label>
            )
          })}
        </div>
      ),
    },
    [ANCHORS.pii]: {
      title: t('rules.pii.title'),
      description: secretsOnly ? t('rules.pii.secretsOnly') : t('rules.pii.description'),
      body: <PersonalData config={config} draft={draft} set={set} />,
    },
    [ANCHORS.ner]: {
      title: t('rules.ner.title'),
      description: t(key('rules.ner.description')),
      body: (
        <div className="space-y-3">
          <Switch
            checked={draft.ner === 'auto'}
            onChange={(on) => set('ner', on ? 'auto' : 'off')}
            label={t('rules.ner.toggle')}
            description={t(key('rules.ner.toggleHelp'))}
          />
          <NerStatusNote config={config} draft={draft} dirty={draft.ner !== base.ner || draft.profile !== base.profile} />
        </div>
      ),
    },
    [ANCHORS.phones]: {
      title: t('rules.phones.title'),
      description: t('rules.phones.description'),
      body: (
        <StringList
          values={draft.phone_regions}
          onChange={(v) => set('phone_regions', v)}
          placeholder={t('rules.phones.placeholder')}
          addLabel={t('rules.phones.add')}
          transform={(v) => v.toUpperCase()}
          validate={(v) => (/^[A-Z]{2}$/.test(v) ? null : t('rules.phones.invalid'))}
          emptyLabel={t('rules.phones.empty')}
        />
      ),
    },
    [ANCHORS.privateIps]: {
      title: t('rules.privateIps.title'),
      description: t('rules.privateIps.description'),
      body: (
        <Segmented<'default' | 'mask' | 'keep'>
          label={t('rules.privateIps.title')}
          value={draft.include_private_ips === null ? 'default' : draft.include_private_ips ? 'mask' : 'keep'}
          onChange={(v) => set('include_private_ips', v === 'default' ? null : v === 'mask')}
          options={[
            { value: 'default', label: t('rules.privateIps.default') },
            { value: 'mask', label: t('rules.privateIps.mask') },
            { value: 'keep', label: t('rules.privateIps.keep') },
          ]}
        />
      ),
    },
    [ANCHORS.terms]: {
      title: t('rules.terms.title'),
      description: t('rules.terms.description'),
      body: <TermsTable scope={scope} onSaved={afterPanelSave} search={tabs.query} shown={tabs.active === 'terms'} />,
      found: foundIn(t, termsSpec(t, format.number), scope, tabs.query),
      wide: true,
    },
    [ANCHORS.recognizers]: {
      title: t('rules.recognizers.title'),
      description: t('rules.recognizers.description'),
      body: (
        <RecognizersTable scope={scope} onSaved={afterPanelSave} drafting={drafting} search={tabs.query} shown={tabs.active === 'recognizers'} />
      ),
      wide: true,
      found: foundIn(t, recognizersSpec(t, key, scope), scope, tabs.query),
    },
    [ANCHORS.allow]: {
      title: t('rules.allow.title'),
      description: t('rules.allow.description'),
      body: <AllowedTable scope={scope} onSaved={afterPanelSave} search={tabs.query} shown={tabs.active === 'values'} />,
      found: foundIn(t, allowSpec(t), scope, tabs.query),
      wide: true,
    },
    [ANCHORS.policies]: {
      title: t('rules.policies.title'),
      description: t('rules.policies.description'),
      body: <PoliciesTable scope={scope} onSaved={afterPanelSave} search={tabs.query} shown={tabs.active === 'policies'} />,
      found: foundIn(t, policiesSpec(t, scope), scope, tabs.query),
      wide: true,
    },
    [ANCHORS.reviewer]: {
      title: t('reviewer.title'),
      description: t(key('reviewer.description')),
      body: (
        <>
          {/* The tutorial's sandbox reaches nothing outside it, the local reviewer's server included, and refuses it:
              but in a chapter with the Tutorial Reviewer, which is simulated, and takes your guidance only. */}
          {reviewerAskable(me) ? (
            <ReviewerEditor value={draft.llm_review} onChange={(review) => set('llm_review', review)} />
          ) : reviewerOffered(me) ? (
            <>
              <p className="mb-3 text-ui text-ink-2">{t('reviewer.simulated')}</p>
              <div className="space-y-2">
                <GuidanceField
                  title={t('reviewer.guidance.title')}
                  help={t('reviewer.guidance.help')}
                  placeholder={t('reviewer.guidance.placeholder')}
                  value={draft.llm_review.guidance ?? null}
                  onChange={(guidance) => set('llm_review', { ...draft.llm_review, guidance })}
                />
                <ReviewerInstructions guidance={draft.llm_review.guidance ?? null} />
              </div>
            </>
          ) : (
            <p className="text-ui text-ink-2">{t('reviewer.sandbox')}</p>
          )}
        </>
      ),
    },
    [ANCHORS.live]: {
      title: t('liveReview.title'),
      description: t('liveReview.description'),
      body: <LiveReviewEditor value={draft.live_review} onChange={(live) => set('live_review', live)} />,
    },
    [ANCHORS.data]: {
      title: t('data.title'),
      description: t('data.description'),
      body: (
        <>
          <DataEditor value={draft} set={setData} layer="global" />
          <OcrStatusNote config={config} draft={draft} set={set} />
        </>
      ),
    },
    [ANCHORS.identity]: {
      title: t('rules.identity.title'),
      description: t('rules.identity.description'),
      body: (
        <div className="space-y-4">
          {/* The tutorial's sandbox has nobody's identity, and refuses to turn it on. */}
          {identityOffered(me) ? (
            <Switch
              checked={draft.identity.enabled}
              onChange={(enabled) => set('identity', { enabled })}
              label={t(key('rules.identity.toggle'))}
              description={secretsOnly ? t('rules.identity.secretsOnly') : t('rules.identity.toggleHelp')}
            />
          ) : (
            <p className="text-ui text-ink-2">{t('rules.identity.sandbox')}</p>
          )}
          <dl className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-2 rounded-lg border border-line bg-surface p-3 text-ui">
            <dt className="text-ink-3">{t('rules.identity.usernames')}</dt>
            <dd className="mono text-ink">{config.identity.usernames.join(', ') || t('common.none')}</dd>
            {me?.server ? (
              // On a Latet server: the host name of each of your computers, as each sent it.
              <>
                <dt className="text-ink-3">{t('rules.identity.hostnames')}</dt>
                <dd className="mono text-ink">
                  {[config.identity.hostname, ...(config.identity.hostnames ?? [])].filter(Boolean).join(', ') || t('common.none')}
                </dd>
              </>
            ) : (
              <>
                <dt className="text-ink-3">{t('rules.identity.hostname')}</dt>
                <dd className="mono text-ink">{config.identity.hostname ?? t('common.none')}</dd>
              </>
            )}
            <dt className="text-ink-3">{t('rules.identity.fullName')}</dt>
            <dd className="text-ink">{config.identity.full_name ?? t('common.none')}</dd>
            <dt className="text-ink-3">{t('rules.identity.emails')}</dt>
            <dd className="mono text-ink">{config.identity.emails.join(', ') || t('common.none')}</dd>
          </dl>
          <p className="text-meta text-ink-3">{t(key('rules.identity.detected'))}</p>

          <div className="mt-2 border-t border-line pt-4">
            <TitleWithDocs title={t('rules.review.title')} docs="section.autoModeReview" className="text-ui font-semibold text-ink" />
            <p className="mt-1 max-w-[80ch] text-ui text-ink-2">{t('rules.review.description')}</p>
            <ReviewSettings value={draft.auto_mode_review} onChange={(review) => set('auto_mode_review', review)} />
            <p className="mt-3 flex max-w-[80ch] items-start gap-1.5 text-meta text-ink-2">
              <Icon name="shield" size={13} className="mt-0.5 flex-none text-good-ink" />
              {t('rules.review.never')}
            </p>
          </div>
        </div>
      ),
    },
    [ANCHORS.history]: {
      title: t('rules.history.title'),
      description: t('rules.history.description'),
      body: (
        <div className="space-y-5">
          <div role="radiogroup" aria-label={t('rules.history.keep')} className="space-y-2">
            <p className="label">{t('rules.history.keep')}</p>
            {HISTORY_MODES.map((mode) => (
              <label key={mode.value} className="flex cursor-pointer items-start gap-2.5">
                <input
                  type="radio"
                  name="history"
                  value={mode.value}
                  checked={draft.history === mode.value}
                  onChange={() => set('history', mode.value)}
                  className="mt-0.5 accent-[var(--ink)]"
                />
                <span>
                  <span className="block text-ui text-ink">{t(mode.label)}</span>
                  <span className="block text-meta text-ink-3">{t(mode.help)}</span>
                </span>
              </label>
            ))}
          </div>
          <label className="flex flex-col gap-1">
            <span className="label">{t('rules.history.retention')}</span>
            <span className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                max={365}
                step={1}
                className="field w-[96px] tabular-nums"
                value={draft.retention_days}
                disabled={draft.history === 'off'}
                onChange={(e) => set('retention_days', Math.round(Number(e.target.value)))}
              />
              <span className="text-ui text-ink-2">{t('rules.history.days', { count: draft.retention_days })}</span>
            </span>
            <span className="text-meta text-ink-3">{t('rules.history.retentionHelp')}</span>
          </label>
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface p-3">
            <p className="min-w-0 flex-1 text-ui text-ink-2">{t('rules.history.clearHelp')}</p>
            <ClearHistoryButton variant="normal" />
          </div>
        </div>
      ),
    },
  }

  // The save bar shows with the tabs it saves (those without tables), and with any of their sections a search finds.
  const shown = shownSections(RULES_TABS, sections, tabs.active, tabs.query)
  const withBar = RULES_TABS.filter((tab) => SAVE_BAR_TABS.includes(tab.id)).some((tab) => tab.anchors.some((anchor) => shown.has(anchor)))
  const lostTabs = formatList(i18n.resolvedLanguage ?? 'en', lost.map((tab) => t(TAB_LABEL[tab])))

  // Nothing changes while a save runs, so the form it comes back to is what it sent.
  return (
    <>
      {header(<RulesTransfer dirty={dirty} onImported={imported} />)}

      {lost.length > 0 && (
        <div role="status" className="mb-4 flex items-start gap-2 rounded-lg border border-warn/60 bg-warn/10 p-3 text-ui text-ink">
          <Icon name="info" size={14} className="mt-0.5 flex-none text-warn-ink" />
          <p className="min-w-0 flex-1">
            {t('rules.lost', { tabs: lostTabs })} <DocsWhyLink target="why.lostRules" />
          </p>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setLost([])}>
            {t('common.dismiss')}
          </button>
        </div>
      )}

      <RulesTabs
        state={tabs}
        tabs={RULES_TABS}
        sections={sections}
        docs={RULES_DOCS}
        labels={{
          protection: t(TAB_LABEL.protection),
          terms: t(TAB_LABEL.terms),
          recognizers: t(TAB_LABEL.recognizers),
          values: t(TAB_LABEL.values),
          policies: t(TAB_LABEL.policies),
          reviewers: t(TAB_LABEL.reviewers),
          advanced: t(TAB_LABEL.advanced),
        }}
        counts={{
          terms: saved.terms.length,
          recognizers: (saved.recognizers ?? []).length,
          values: saved.allow_values.length + saved.allow_patterns.length,
          policies: Object.keys(saved.policies).length,
        }}
        dirty={dirtyTabs(base, draft, SECTION_OF)}
        idPrefix={TABS_ID}
        disabled={saving}
      />

      {withBar && (
      <SaveBar>
        <SaveRow
          dirty={dirty}
          saving={saving}
          error={saveError}
          saved={
            version !== null ? (
              <span className="flex items-center gap-1.5 text-good-ink">
                <Icon name="check" size={14} />
                {t('rules.savedVersion', { version })}
              </span>
            ) : undefined
          }
          saveLabel={t('rules.save')}
          onDiscard={() => setDraft(base)}
          onSave={() => void save()}
        />
      </SaveBar>
      )}
    </>
  )
}
