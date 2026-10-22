// Strict data mode, guided: the columns of a tool's output, what Latet proposes for each (and, when
// you ask, the local reviewer's opinion), and one click to keep the chosen ones in clear, for this
// project or everywhere. Nothing applies by itself.

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { Agent, ColumnOpinions, ColumnProposal, ColumnReason, OutputColumns } from '../../api/types'
import { tk } from '../../i18n'
import { hasStrictValues, heldByProject, initialChecks, resends, rulesWith, whereChoices } from '../../lib/columns'
import { canKeep } from '../../lib/fields'
import { useMe } from '../../lib/me'
import { savedChangePending } from '../../lib/refused'
import { reviewerAskable } from '../../lib/tutorial'
import { useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { DocsSectionLink } from '../DocsLinks'
import { Icon } from '../Icon'
import { useToast } from '../Toast'
import { WhereChoice, type Where } from '../ValueScope'
import { errorText, InlineError, Loading } from '../ui'

const PROPOSAL: Record<ColumnProposal, string> = {
  keep: tk('exchange.columns.proposal.keep'),
  mask: tk('exchange.columns.proposal.mask'),
  ask: tk('exchange.columns.proposal.ask'),
}

const REASON: Record<ColumnReason, string> = {
  dates: tk('exchange.columns.reason.dates'),
  ids: tk('exchange.columns.reason.ids'),
  states: tk('exchange.columns.reason.states'),
  flags: tk('exchange.columns.reason.flags'),
  counts: tk('exchange.columns.reason.counts'),
  'secret-name': tk('exchange.columns.reason.secretName'),
  secret: tk('exchange.columns.reason.secret'),
  people: tk('exchange.columns.reason.people'),
  person: tk('exchange.columns.reason.person'),
  'free-text': tk('exchange.columns.reason.freeText'),
  'birth-dates': tk('exchange.columns.reason.birthDates'),
  sensitive: tk('exchange.columns.reason.sensitive'),
  amounts: tk('exchange.columns.reason.amounts'),
  'dates-unclear': tk('exchange.columns.reason.datesUnclear'),
  'flags-unclear': tk('exchange.columns.reason.flagsUnclear'),
  repeats: tk('exchange.columns.reason.repeats'),
  unclear: tk('exchange.columns.reason.unclear'),
}

const PROPOSAL_TAG: Record<ColumnProposal, string> = {
  keep: 'border-good/50 bg-good/10 text-good-ink',
  mask: 'border-line text-ink-2',
  ask: 'border-warn/60 bg-warn/10 text-ink',
}

interface ColumnsPanelProps {
  exchangeId: string
  /** The block's index in the exchange. */
  slot: number
  agent: Agent
}

/** Under a tool's output strict data mode masked: closed at first, read from the daemon when opened. */
export function ColumnsPanel({ exchangeId, slot, agent }: ColumnsPanelProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className="border-t border-line">
      <div className="flex items-center pr-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 px-3 py-1.5 text-left text-meta text-ink-2 hover:bg-wash"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((v) => !v)}
        >
          <Icon name={open ? 'chevronDown' : 'chevronRight'} size={12} />
          {open ? t('exchange.columns.hide') : t('exchange.columns.show')}
        </button>
        <DocsSectionLink target="section.columns" section={t('exchange.columns.show')} />
      </div>
      {open && (
        <div id={id} className="px-3 pb-3">
          <Columns exchangeId={exchangeId} slot={slot} agent={agent} />
        </div>
      )}
    </div>
  )
}

/**
 * The columns of the first tool output strict data mode masked in an exchange (or of its block `slot`),
 * open, for a page that shows them on their own: the tutorial's, which has only the exchange's id.
 */
export function ExchangeColumns({ exchangeId, slot }: { exchangeId: string; slot?: number }) {
  const { t } = useTranslation()
  const exchange = useAsync(`exchange:${exchangeId}`, (signal) => api.exchange(exchangeId, signal))
  if (exchange.error && !exchange.data) return <InlineError error={exchange.error} />
  if (!exchange.data) return <Loading />
  const { summary, request } = exchange.data
  const block = slot ?? request.slots.findIndex(hasStrictValues)
  if (block < 0 || !request.slots[block]) return <p className="text-meta text-ink-3">{t('exchange.columns.none')}</p>
  return <Columns exchangeId={exchangeId} slot={block} agent={summary.agent} />
}

function Columns({ exchangeId, slot, agent }: ColumnsPanelProps) {
  const state = useAsync(`columns:${exchangeId}:${slot}`, (signal) => api.columns(exchangeId, slot, signal))
  if (state.error && !state.data) return <InlineError error={state.error} />
  if (!state.data) return <Loading />
  // Saved: the columns come back with what is kept in clear now, and the choice starts again from them.
  const key = state.data.columns.map((c) => `${c.name}:${c.kept_now}`).join('|')
  return <ColumnsChoice key={key} data={state.data} exchangeId={exchangeId} slot={slot} agent={agent} onSaved={state.reload} />
}

function ColumnsChoice({ data, exchangeId, slot, agent, onSaved }: ColumnsPanelProps & { data: OutputColumns; onSaved: () => void }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const toast = useToast()
  const { me } = useMe()
  const [checked, setChecked] = useState(() => initialChecks(data.columns))
  const [where, setWhere] = useState<Where>('project')
  const [opinions, setOpinions] = useState<ColumnOpinions['opinions'] | null>(null)
  const [asking, setAsking] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Only the old read is blocked: a completed refresh (or reopening the panel) releases this choice.
  const [pendingFrom, setPendingFrom] = useState<OutputColumns | null>(null)
  const pending = pendingFrom === data
  const choices = whereChoices(data.project)
  const projectId = choices.project && where === 'project' ? (data.project?.id ?? null) : null
  const held = projectId ? [] : heldByProject(data.columns, checked)

  const toggle = (name: string, on: boolean) =>
    setChecked((current) => {
      const next = new Set(current)
      if (on) next.add(name)
      else next.delete(name)
      return next
    })

  const ask = async () => {
    setAsking(true)
    setProblem(null)
    try {
      setOpinions((await api.reviewColumns(exchangeId, slot)).opinions)
    } catch (error) {
      setProblem(errorText(t, error))
    } finally {
      setAsking(false)
    }
  }

  const apply = async () => {
    setSaving(true)
    try {
      if (projectId) {
        await api.changePersonal(projectId, (personal) => ({ ...personal, fields: rulesWith(personal.fields ?? [], data.columns, checked) }))
      } else {
        await api.changeConfig((config) => ({ fields: rulesWith(config.fields ?? [], data.columns, checked) }))
      }
      const count = checked.size
      toast.show(projectId ? t('exchange.columns.savedProject', { count }) : t('exchange.columns.saved', { count }))
      onSaved()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
      if (savedChangePending(error)) {
        setPendingFrom(data)
        setProblem(errorText(t, error))
        onSaved()
      }
    } finally {
      setSaving(false)
    }
  }

  if (data.columns.length === 0) return <p className="text-meta text-ink-3">{t('exchange.columns.none')}</p>
  return (
    <>
      <p className="mb-2 max-w-[90ch] text-meta text-ink-2">{t('exchange.columns.intro')}</p>
      {!data.strict && <p className="mb-2 max-w-[90ch] text-meta text-ink-3">{t('exchange.columns.strictOff')}</p>}
      <div className="overflow-x-auto rounded-md border border-line">
        <table className="data-table text-meta">
          <thead>
            <tr>
              <th scope="col" className="w-8">
                <span className="sr-only">{t('exchange.columns.keep')}</span>
              </th>
              <th scope="col">{t('exchange.columns.column')}</th>
              <th scope="col">{t('exchange.columns.proposal.title')}</th>
              {opinions && <th scope="col">{t('exchange.columns.reviewer')}</th>}
              <th scope="col">{t('exchange.columns.examples')}</th>
            </tr>
          </thead>
          <tbody>
            {data.columns.map((column) => {
              const starred = !canKeep(column.name)
              const opinion = opinions?.[column.name]
              return (
                <tr key={column.name}>
                  <td>
                    <input
                      type="checkbox"
                      className="accent-[var(--ink)]"
                      aria-label={t('exchange.columns.keepColumn', { name: column.name })}
                      checked={column.kept_now || checked.has(column.name)}
                      disabled={column.kept_now || starred || saving || pending}
                      onChange={(event) => toggle(column.name, event.target.checked)}
                    />
                  </td>
                  <td className="mono text-ink">
                    {column.name}
                    {column.masked > 0 && (
                      <span className="block font-sans text-micro text-ink-3">{t('exchange.columns.masked', { count: column.masked })}</span>
                    )}
                    {column.kept_now && <span className="block font-sans text-micro text-ink-3">{t('exchange.columns.keptNow')}</span>}
                    {starred && <span className="block font-sans text-micro text-ink-3">{t('exchange.columns.starred')}</span>}
                  </td>
                  <td>
                    <span className={`tag ${PROPOSAL_TAG[column.proposal]}`}>{t(PROPOSAL[column.proposal])}</span>
                    <span className="mt-0.5 block text-ink-2">{t(REASON[column.reason])}</span>
                    {column.detail && <span className="mono block break-all text-micro text-ink-3">{column.detail}</span>}
                  </td>
                  {opinions && (
                    <td className="max-w-[260px] text-ink-2">
                      {opinion
                        ? opinion.keep
                          ? t('exchange.columns.opinionKeep', { why: opinion.why })
                          : t('exchange.columns.opinionMask', { why: opinion.why })
                        : t('common.none')}
                    </td>
                  )}
                  <td className="mono max-w-[280px] break-all text-ink-3">{column.examples.join(', ')}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        {choices.project && <WhereChoice where={where} onChange={setWhere} />}
        <button type="button" className="btn btn-sm" disabled={saving || pending || checked.size === 0} onClick={() => void apply()}>
          <Icon name="eye" size={13} />
          {saving ? t('exchange.columns.saving') : t('exchange.columns.apply', { count: checked.size })}
        </button>
        {/* Asked from here on a real daemon only: the tutorial's sandbox refuses it, the Tutorial Reviewer's chapter
            included, and Rules cannot turn a reviewer on there. Nothing to ask or to turn on. */}
        {reviewerAskable(me) &&
          (data.reviewer ? (
            <button type="button" className="btn btn-sm" disabled={asking} onClick={() => void ask()}>
              <Icon name="question" size={13} />
              {asking ? t('exchange.columns.asking') : t('exchange.columns.ask')}
            </button>
          ) : (
            <span className="text-meta text-ink-3">{t('exchange.columns.reviewerOff')}</span>
          ))}
      </div>
      {problem && <p className="mt-2 text-meta text-crit-ink">{problem}</p>}
      <div className="mt-2 max-w-[90ch] space-y-1 text-meta text-ink-3">
        {choices.notLoaded && <p>{t('exchange.columns.notLoaded', { project: data.project?.name ?? '' })}</p>}
        {held.length > 0 && <p>{t('exchange.columns.held', { columns: fmt.list(held), count: held.length })}</p>}
        {resends(agent) && <p>{t('exchange.columns.resends')}</p>}
      </div>
    </>
  )
}
