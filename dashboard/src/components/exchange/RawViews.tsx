import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { ExchangeDetail, Header } from '../../api/types'
import { buildFromPaths, toJson } from '../../lib/jsonpath'
import { CopyButton, Segmented } from '../ui'

function HeaderTable({ headers, caption }: { headers: Header[]; caption: string }) {
  const { t } = useTranslation()
  return (
    <div className="min-w-0">
      <h4 className="mb-1.5 text-meta font-medium text-ink-2">{caption}</h4>
      {headers.length === 0 ? (
        <p className="text-meta text-ink-3">{t('exchange.headers.none')}</p>
      ) : (
        <div className="well overflow-x-auto">
          <table className="w-full text-left">
            <caption className="sr-only">{caption}</caption>
            <tbody className="mono text-meta">
              {headers.map(([name, value], i) => (
                <tr key={`${name}:${i}`} className="border-b border-line last:border-0">
                  <th scope="row" className="py-1 pr-4 pl-3 align-top font-normal whitespace-nowrap text-ink-2">
                    {name}
                  </th>
                  <td className={`py-1 pr-3 break-all ${value.startsWith('<redacted') ? 'text-ink-3' : 'text-ink'}`}>{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function HeadersView({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  return (
    <div className="space-y-2">
      <p className="text-ui text-ink-2">{t('exchange.headers.note')}</p>
      <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
        <HeaderTable headers={detail.request.headers} caption={t('exchange.headers.request')} />
        <HeaderTable headers={detail.response?.headers ?? []} caption={t('exchange.headers.response')} />
      </div>
    </div>
  )
}

type RawKind = 'payload' | 'record'

/** The sent text fields rebuilt in their JSON places, or the whole exchange record. */
export function RawJsonView({ detail }: { detail: ExchangeDetail }) {
  const { t } = useTranslation()
  const [kind, setKind] = useState<RawKind>('payload')
  const payload = useMemo(() => buildFromPaths(detail.request.slots.map((s) => ({ path: s.path, value: s.masked }))), [detail])
  const text = useMemo(() => (kind === 'payload' ? toJson(payload.root) : JSON.stringify(detail, null, 2)), [kind, payload, detail])
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented<RawKind>
          label={t('exchange.raw.label')}
          value={kind}
          onChange={setKind}
          options={[
            { value: 'payload', label: t('exchange.raw.payload') },
            { value: 'record', label: t('exchange.raw.record') },
          ]}
        />
        <CopyButton text={text} label={t('exchange.raw.copy')} />
      </div>
      <p className="max-w-[90ch] text-meta text-ink-3">
        {kind === 'payload' ? t('exchange.raw.payloadNote') : t('exchange.raw.recordNote')}
        {kind === 'payload' && payload.skipped.length > 0 && ` ${t('exchange.raw.skipped', { count: payload.skipped.length })}`}
      </p>
      <pre className="well code max-h-[560px] overflow-auto px-3 py-2.5 text-ink">{text}</pre>
    </div>
  )
}
