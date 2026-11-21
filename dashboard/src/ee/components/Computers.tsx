import { useTranslation } from 'react-i18next'
import { useMe } from '../../lib/me'
import { useFormat } from '../../lib/useFormat'
import { computerLines } from '../lib/computers'

/**
 * The computers connected to the person's account now, with whether an administrator made the code each was connected
 * with (`/me.edition.computers`, Latet Enterprise on a server): listed for good, beside the notice that tells of a new
 * one for 14 days. Nothing where the server gives no list: an API key's space, a computer, the Community Edition.
 */
export function YourComputers() {
  const { t } = useTranslation()
  const fmt = useFormat()
  const { me } = useMe()
  const computers = me?.edition?.computers
  if (!computers) return null
  const lines = computerLines(computers)
  return (
    <div className="mt-2 border-t border-line pt-4" data-your-computers>
      <p className="text-ui font-semibold text-ink">{t('ee.computers.title')}</p>
      <p className="mt-1 max-w-[80ch] text-ui text-ink-2">{t('ee.computers.description')}</p>
      {lines.length === 0 ? (
        <p className="mt-2 text-ui text-ink-3">{t('ee.computers.none')}</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {lines.map((line) => (
            <li key={line.key} className="rounded-md border border-line bg-surface px-3 py-2 text-ui">
              <p className={line.label === null ? 'text-ink-2' : 'mono text-ink'}>{line.label ?? t('ee.computers.unnamed')}</p>
              <p className="text-meta text-ink-3">{t(line.how, { when: fmt.relative(line.connected) })}</p>
              <p className="text-meta text-ink-3">{t(line.used, { when: line.lastUsed === null ? '' : fmt.relative(line.lastUsed) })}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
