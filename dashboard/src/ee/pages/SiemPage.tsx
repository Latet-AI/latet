import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { Icon } from '../../components/Icon'
import { ConfirmDialog, EmptyState, InlineCodeText, Loading, PageHeader, Segmented } from '../../components/ui'
import { tk } from '../../i18n'
import { hasPage } from '../../lib/edition'
import { useMe } from '../../lib/me'
import { useFormat } from '../../lib/useFormat'
import { ChangedMeanwhile, CompanySignInWay } from '../components/CompanyWay'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { SIEM_PAUSED_TEXT, draftErrors, localOutput, statusOf, validSiemView, type Draft, type DraftError } from '../lib/siem'
import { useSiem } from '../lib/useSiem'
import type { SiemKind, SiemOutputView } from '../types'

const KINDS: SiemKind[] = ['webhook', 'splunk', 'syslog']
const KIND_LABEL = { webhook: tk('ee.reports.siem.kinds.webhook'), splunk: tk('ee.reports.siem.kinds.splunk'), syslog: tk('ee.reports.siem.kinds.syslog') }
const KIND_HELP = { webhook: tk('ee.reports.siem.help.webhook'), splunk: tk('ee.reports.siem.help.splunk'), syslog: tk('ee.reports.siem.help.syslog') }
const SECRET_TEXT = { moved: tk('ee.reports.siem.fields.secretMoved'), kept: tk('ee.reports.siem.fields.secretKept'), none: tk('ee.reports.siem.fields.secretNone') }
const ERROR_TEXT: Record<DraftError, string> = {
  name: tk('ee.reports.siem.errors.name'), nameTaken: tk('ee.reports.siem.errors.nameTaken'), url: tk('ee.reports.siem.errors.url'), local: tk('ee.reports.siem.errors.local'),
  token: tk('ee.reports.siem.errors.token'), header: tk('ee.reports.siem.errors.header'), credential: tk('ee.reports.siem.errors.credential'), secretNeeded: tk('ee.reports.siem.errors.secretNeeded'),
  secret: tk('ee.reports.siem.errors.secret'), outputs: tk('ee.reports.siem.errors.outputs'),
}
const FAILURE_TEXT = { read: tk('ee.reports.siem.failure.read'), failed: tk('ee.reports.siem.failure.failed'), forbidden: tk('ee.reports.siem.failure.forbidden'), byCommand: tk('ee.reports.siem.failure.byCommand'), large: tk('ee.reports.siem.failure.large'), rate: tk('ee.reports.siem.failure.rate') }

export function SiemPage() {
  const { t } = useTranslation(), { me, unreachable } = useMe()
  const readOnly = useEnterpriseReadOnly()
  const siem = useSiem(readOnly)
  const malformed = siem.data !== undefined && !validSiemView(siem.data)
  const error = malformed ? 'read' : siem.error
  const view = !error && !siem.busy && !siem.company ? siem.data : undefined
  const editable = !!view?.editable && !!view.etag && !view.paused && !readOnly && siem.editable
  const draft = editable ? siem.draft : null, removing = editable ? siem.removing : null
  const reports = !unreachable && hasPage(me, 'reports')
  return <>
    <PageHeader title={t('ee.reports.siem.title')} description={t('ee.reports.siem.description')} actions={<>
      {reports && <Link to="/reports" className="btn"><Icon name="chevronLeft" size={14} />{t('ee.reports.siem.back')}</Link>}
      {editable && !draft && !removing && <button type="button" className="btn btn-primary" disabled={view!.outputs.length >= 50} onClick={() => siem.edit()}><Icon name="plus" size={14} />{t('ee.reports.siem.add')}</button>}
    </>} />
    {siem.company && <CompanySignInWay />}
    {siem.changed && <ChangedMeanwhile text={t('ee.reports.siem.changedMeanwhile')} />}
    {error || siem.company ? <div role="alert" className="panel space-y-3 p-4">
      {error && <p><InlineCodeText text={t(FAILURE_TEXT[error])} /></p>}
      <button type="button" className="btn" onClick={siem.reload}>{t('common.retry')}</button>
    </div> : siem.busy || !view ? <Loading /> : <div className="space-y-3">
      {view.paused && <p role="status" className="panel px-4 py-3 text-ui text-ink-2"><InlineCodeText text={t(SIEM_PAUSED_TEXT[view.paused.why], { date: view.paused.expires ?? t('ee.reports.siem.unknownDate') })} /></p>}
      {!editable && <p className="text-meta text-ink-3">{t('ee.reports.siem.readOnly')}</p>}
      {draft && <OutputForm draft={draft} others={view.outputs.filter(o => o.name !== draft.original)} onChange={siem.change} onCancel={siem.cancel} onSave={siem.save} />}
      {view.outputs.length === 0 && !draft ? <div className="panel"><EmptyState icon="siem" title={t('ee.reports.siem.empty.title')}>{t('ee.reports.siem.empty.body')}</EmptyState></div>
        : view.outputs.map(output => <OutputCard key={output.name} output={output} editable={editable} disabled={!!draft || !!removing} onEdit={() => siem.edit(output.name)} onRemove={() => siem.remove(output.name)} onTest={() => siem.test(output.name)} />)}
    </div>}
    <ConfirmDialog open={removing !== null} title={t('ee.reports.siem.remove.title', { name: removing?.name ?? '' })} confirmLabel={t('ee.reports.siem.remove.confirm')} busy={siem.busy} onCancel={siem.cancel} onConfirm={siem.confirmRemove}>
      {removing && removing.queued > 0 ? t('ee.reports.siem.remove.body', { count: removing.queued }) : t('ee.reports.siem.remove.nothing')}
    </ConfirmDialog>
  </>
}

function OutputCard({ output, editable, disabled, onEdit, onRemove, onTest }: { output: SiemOutputView; editable: boolean; disabled: boolean; onEdit: () => void; onRemove: () => void; onTest: () => void }) {
  const { t } = useTranslation(), fmt = useFormat(), status = statusOf(output)
  const time = output.failing_since === null ? t('ee.reports.siem.unknownDate') : fmt.dateTime(output.failing_since)
  const doing = status === 'held' ? t(output.blocked ? tk('ee.reports.siem.status.heldAdmin') : tk('ee.reports.siem.status.held'), { time })
    : status === 'failing' ? t('ee.reports.siem.status.failing', { time }) : status === 'ok' ? t('ee.reports.siem.status.ok', { time: fmt.dateTime(output.last_ok!) }) : t('ee.reports.siem.status.idle')
  const parts = [doing,
    ...(output.queued > 0 ? [t('ee.reports.siem.status.queued', { count: output.queued })] : []),
    ...(output.dropped > 0 ? [t('ee.reports.siem.status.dropped', { count: output.dropped })] : []),
    ...(output.collapsed > 0 ? [t('ee.reports.siem.status.collapsed', { count: output.collapsed })] : []),
  ]
  const local = localOutput(output)
  return <section aria-label={output.name} className="panel space-y-2 px-4 py-3">
    <div className="flex flex-wrap items-center gap-2"><h2 className="text-title font-semibold text-ink">{output.name}</h2><span className="tag">{t(KIND_LABEL[output.kind])}</span></div>
    <p className="mono truncate text-meta text-ink-2">{output.url}</p>
    <p className={`text-meta ${status === 'failing' || status === 'held' || output.collapsed > 0 ? 'text-crit-ink' : 'text-ink-3'}`}>{parts.join(' · ')}</p>
    {output.last_error && (status === 'held' || status === 'failing') && <p className="text-meta text-crit-ink break-words">{output.last_error}</p>}
    {status === 'held' && <p className="text-meta text-ink-2"><InlineCodeText text={t('ee.reports.siem.help.skip', { command: `latet server siem skip ${output.name}` })} /></p>}
    <p className={`text-meta ${output.secret_destination_changed ? 'text-crit-ink' : 'text-ink-3'}`}>{t(output.secret_destination_changed ? SECRET_TEXT.moved : output.has_secret ? SECRET_TEXT.kept : SECRET_TEXT.none)}</p>
    {local && <p className="text-meta text-ink-2"><InlineCodeText text={t('ee.reports.siem.help.local')} /></p>}
    {editable && <div className="flex flex-wrap gap-2">
      {!local && <><button type="button" className="btn" disabled={disabled} onClick={onTest}>{t('ee.reports.siem.test')}</button><button type="button" className="btn" disabled={disabled} onClick={onEdit}>{t('ee.reports.siem.edit')}</button></>}
      <button type="button" className="btn" disabled={disabled} onClick={onRemove}><Icon name="trash" size={14} />{t('ee.reports.siem.remove.action')}</button>
    </div>}
  </section>
}

function OutputForm({ draft, others, onChange, onCancel, onSave }: { draft: Draft; others: readonly SiemOutputView[]; onChange: (draft: Draft) => void; onCancel: () => void; onSave: () => void }) {
  const { t } = useTranslation(), errors = draftErrors(draft, others)
  const put = <K extends keyof Draft>(key: K, value: Draft[K]) => onChange({ ...draft, [key]: value })
  const header = (index: number, at: 0 | 1, value: string) => put('headers', draft.headers.map((row, i): [string, string] => i !== index ? row : at === 0 ? [value, row[1]] : [row[0], value]))
  return <form className="panel space-y-3 px-4 py-4" autoComplete="off" onSubmit={event => { event.preventDefault(); if (!errors.length) onSave() }}>
    <label className="flex flex-col gap-1"><span className="label">{t('ee.reports.siem.fields.name')}</span><input className="field mono" value={draft.name} readOnly={draft.original !== null} maxLength={40} onChange={e => put('name', e.target.value)} /><span className="text-meta text-ink-3">{t(draft.original ? tk('ee.reports.siem.help.immutableName') : tk('ee.reports.siem.help.name'))}</span></label>
    <Segmented<SiemKind> label={t('ee.reports.siem.fields.kind')} value={draft.kind} onChange={kind => onChange({ ...draft, kind })} options={KINDS.map(kind => ({ value: kind, label: t(KIND_LABEL[kind]) }))} />
    <label className="flex flex-col gap-1"><span className="label">{t('ee.reports.siem.fields.url')}</span><input className="field mono" value={draft.url} spellCheck={false} onChange={e => put('url', e.target.value)} /><span className="text-meta text-ink-3"><InlineCodeText text={t(KIND_HELP[draft.kind])} /></span></label>
    {draft.kind === 'webhook' && <>
      <fieldset className="space-y-2"><legend className="label">{t('ee.reports.siem.fields.headers')}</legend>
        {draft.headers.map(([name, value], index) => <div key={index} className="flex gap-2">
          <input className="field mono w-[200px]" aria-label={t('ee.reports.siem.fields.headerName')} value={name} onChange={e => header(index, 0, e.target.value)} />
          <input className="field mono min-w-0 flex-1" aria-label={t('ee.reports.siem.fields.headerValue')} value={value} onChange={e => header(index, 1, e.target.value)} />
          <button type="button" className="btn" aria-label={t('ee.reports.siem.fields.removeHeader')} onClick={() => put('headers', draft.headers.filter((_, i) => i !== index))}><Icon name="x" size={14} /></button>
        </div>)}
        <button type="button" className="btn" disabled={draft.headers.length >= 32} onClick={() => put('headers', [...draft.headers, ['', '']])}><Icon name="plus" size={14} />{t('ee.reports.siem.fields.addHeader')}</button>
      </fieldset>
      <Segmented<'json' | 'ndjson'> label={t('ee.reports.siem.fields.format')} value={draft.format} onChange={format => onChange({ ...draft, format })} options={[{ value: 'json', label: t('ee.reports.siem.fields.json') }, { value: 'ndjson', label: t('ee.reports.siem.fields.ndjson') }]} />
    </>}
    <label className="flex flex-col gap-1"><span className="label">{t('ee.reports.siem.fields.secret')}</span>
      <input type="password" autoComplete="off" spellCheck={false} className="field mono" placeholder={t('ee.reports.siem.fields.secretPlaceholder')} value={draft.secret} onChange={e => put('secret', e.target.value)} />
      <span className="text-meta text-ink-3">{t(draft.hasSecret ? SECRET_TEXT.kept : SECRET_TEXT.none)} {t('ee.reports.siem.help.secret')}</span>
    </label>
    {draft.secretName !== null && <label className="flex items-center gap-2 text-ui"><input type="checkbox" checked={draft.removeSecret} onChange={e => put('removeSecret', e.target.checked)} />{t('ee.reports.siem.fields.removeSecret')}</label>}
    {errors.length > 0 && <ul role="alert" className="space-y-1 text-meta text-crit-ink">{errors.map(error => <li key={error}>{t(ERROR_TEXT[error])}</li>)}</ul>}
    <div className="flex gap-2"><button type="submit" className="btn btn-primary" disabled={errors.length > 0}>{t('ee.reports.siem.save')}</button><button type="button" className="btn" onClick={onCancel}>{t('common.cancel')}</button></div>
  </form>
}
