import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams, type To } from 'react-router'
import { api } from '../api/client'
import type { VaultEntity } from '../api/types'
import { ClassLabel } from '../components/badges'
import { Icon } from '../components/Icon'
import { Pagination } from '../components/Pagination'
import { useToast } from '../components/Toast'
import { ConfirmDialog, EmptyState, ErrorState, errorText, Loading, PageHeader } from '../components/ui'
import { CLASS_LABEL, CLASS_ORDER, viewClass } from '../lib/labels'
import { pageRange } from '../lib/pages'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'
import { useDeploymentKey } from '../lib/me'

const REVEAL_SECONDS = 10

/** The address's search with page `number` (the first page has none). */
function withPage(params: URLSearchParams, number: number): URLSearchParams {
  const next = new URLSearchParams(params)
  if (number > 1) next.set('page', String(number))
  else next.delete('page')
  return next
}

export function VaultPage() {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const toast = useToast()
  const fmt = useFormat()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const type = params.get('type') ?? ''
  const cls = params.get('class') ?? ''
  const page = Math.max(1, Number.parseInt(params.get('page') ?? '', 10) || 1)
  const [draft, setDraft] = useState(q)
  const [forgetting, setForgetting] = useState<VaultEntity | null>(null)
  const [busy, setBusy] = useState(false)
  // A page at a time: the daemon filters, sorts and counts, and answers fifty values and every type it holds.
  const vault = useAsync(`vault:${q}|${type}|${cls}|${page}`, (signal) => api.vault({ q, type, class: cls, page }, signal))

  // Search as you type, a moment after typing stops, from the first page.
  useEffect(() => {
    if (draft === q) return
    const timer = setTimeout(() => {
      const next = new URLSearchParams(params)
      if (draft) next.set('q', draft)
      else next.delete('q')
      next.delete('page')
      setParams(next, { replace: true })
    }, 250)
    return () => clearTimeout(timer)
  }, [draft, q, params, setParams])

  // The filter's types: every type the vault holds, so the menu does not shrink as you filter.
  const types = useMemo(() => [...new Set([...Object.keys(vault.data?.types ?? {}), ...(type ? [type] : [])])].sort(), [vault.data, type])
  const items = vault.data?.items ?? []

  // A new filter shows its first page.
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    setParams(next, { replace: true })
  }

  const pageTo = (number: number): To => {
    const search = withPage(params, number).toString()
    return { search: search ? `?${search}` : '' }
  }

  // A page past the last (or past a page a forgotten value emptied): the daemon answers the last one, and the address
  // says that one, in place of the page asked for. Not while an answer is awaited or failed: it is another page's.
  const answered = vault.stale || vault.error ? null : (vault.data?.page ?? null)
  useEffect(() => {
    if (answered !== null && answered !== page) setParams(withPage(params, answered), { replace: true })
  }, [answered, page, params, setParams])

  const forget = async () => {
    if (!forgetting) return
    setBusy(true)
    try {
      await api.forget(forgetting.id)
      vault.mutate((data) => (data ? { ...data, items: data.items.filter((e) => e.id !== forgetting.id), total: data.total - 1 } : data))
      vault.reload() // the page fills up again from the next one
      toast.show(t('vault.forget.done', { type: forgetting.type }))
      setForgetting(null)
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }

  const filtered = Boolean(q || type || cls)
  const range = vault.data && pageRange(vault.data.page, vault.data.per_page, vault.data.total)

  return (
    <>
      <PageHeader title={t('vault.title')} description={t(key('vault.description'))} docs="page.vault" />

      <div className="mb-3 flex flex-wrap items-end gap-3" role="search">
        <label className="flex min-w-[260px] flex-1 flex-col gap-1 sm:max-w-[420px]">
          <span className="label">{t('vault.search.label')}</span>
          <span className="relative">
            <Icon name="search" size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-3" />
            <input
              type="search"
              className="field w-full pl-8"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('vault.search.placeholder')}
              autoComplete="off"
              spellCheck={false}
            />
          </span>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('vault.filters.type')}</span>
          <select className="field mono w-[200px]" value={type} onChange={(e) => setParam('type', e.target.value)}>
            <option value="">{t('vault.filters.anyType')}</option>
            {types.map((ty) => (
              <option key={ty} value={ty}>
                {ty}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">{t('vault.filters.class')}</span>
          <select className="field w-[170px]" value={cls} onChange={(e) => setParam('class', e.target.value)}>
            <option value="">{t('vault.filters.anyClass')}</option>
            {CLASS_ORDER.map((c) => (
              <option key={c} value={c}>
                {t(CLASS_LABEL[c])}
              </option>
            ))}
          </select>
        </label>
        {vault.data && (
          <p className="pb-1.5 text-meta text-ink-3" role="status">
            {range && vault.data.pages > 1
              ? t('vault.range', { from: fmt.number(range.from), to: fmt.number(range.to), total: fmt.number(vault.data.total) })
              : t('vault.count', { count: vault.data.total })}
          </p>
        )}
      </div>

      <div className={`panel overflow-x-auto ${vault.stale ? 'opacity-60' : ''}`}>
        {vault.loading ? (
          <Loading />
        ) : vault.error && !vault.data ? (
          <ErrorState error={vault.error} onRetry={vault.reload} />
        ) : items.length === 0 ? (
          filtered ? (
            <EmptyState icon="search" title={t('vault.emptyFiltered.title')}>
              {t('vault.emptyFiltered.body')}
            </EmptyState>
          ) : (
            <EmptyState icon="vault" title={t('vault.empty.title')}>
              {t('vault.empty.body')}
            </EmptyState>
          )
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('vault.columns.type')}</th>
                <th scope="col">{t('vault.columns.class')}</th>
                <th scope="col">{t('vault.columns.surrogate')}</th>
                <th scope="col">{t('vault.columns.original')}</th>
                <th scope="col">{t('vault.columns.created')}</th>
                <th scope="col" className="num">
                  {t('vault.columns.sessions')}
                </th>
                <th scope="col">
                  <span className="sr-only">{t('vault.columns.actions')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((entity) => (
                <tr key={entity.id}>
                  <td className="mono whitespace-nowrap text-ink">{entity.type}</td>
                  <td className="whitespace-nowrap">
                    <ClassLabel cls={viewClass(entity.class)} />
                  </td>
                  <td className="mono max-w-[340px] break-all text-ink">{entity.surrogate}</td>
                  <td className="min-w-[220px] max-w-[360px]">
                    <Reveal entity={entity} />
                  </td>
                  <td className="whitespace-nowrap text-ink-2" title={fmt.dateTime(entity.created)}>
                    {fmt.date(entity.created)}
                  </td>
                  <td className="num text-ink-2">{fmt.number(entity.sessions)}</td>
                  <td className="text-right whitespace-nowrap">
                    <button type="button" className="btn btn-ghost btn-sm btn-quiet-danger" onClick={() => setForgetting(entity)}>
                      <Icon name="trash" size={13} />
                      {t('vault.forget.action')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {vault.data && <Pagination page={vault.data.page} pages={vault.data.pages} to={pageTo} />}

      <ConfirmDialog
        open={forgetting !== null}
        title={t('vault.forget.title')}
        confirmLabel={t('vault.forget.confirm')}
        onConfirm={() => void forget()}
        onCancel={() => setForgetting(null)}
        busy={busy}
      >
        {forgetting && (
          <>
            <p>
              {t('vault.forget.body', { type: forgetting.type })} <code className="mono text-ink">{forgetting.surrogate}</code>
            </p>
            <p className="mt-2">{t('vault.forget.consequence')}</p>
          </>
        )}
      </ConfirmDialog>
    </>
  )
}

/** The original stays hidden until asked for, and hides itself again after a few seconds. */
function Reveal({ entity }: { entity: VaultEntity }) {
  const { t } = useTranslation()
  const [original, setOriginal] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])

  const hide = () => {
    clearTimeout(timer.current)
    setOriginal(null)
  }

  const reveal = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.reveal(entity.id)
      setOriginal(result.original)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => setOriginal(null), REVEAL_SECONDS * 1000)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (original !== null) {
    return (
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <code className="mono block break-all text-ink">{original}</code>
          <span className="mt-1 block h-0.5 overflow-hidden rounded-full bg-line" aria-hidden="true">
            <span className="countdown block h-full bg-brand" style={{ animationDuration: `${REVEAL_SECONDS}s` }} />
          </span>
          <span className="sr-only" role="status">
            {t('vault.reveal.shown', { seconds: REVEAL_SECONDS })}
          </span>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={hide}>
          <Icon name="eyeOff" size={13} />
          {t('vault.reveal.hide')}
        </button>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2">
      <span className="inline-block h-3 w-24 rounded-[2px] bg-ink/80" aria-label={t('vault.reveal.hidden')} role="img" />
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => void reveal()} disabled={busy}>
        <Icon name="eye" size={13} />
        {t('vault.reveal.action')}
      </button>
      {error !== null && <span className="text-meta text-crit-ink">{errorText(t, error)}</span>}
    </div>
  )
}
