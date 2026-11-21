import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { errorText } from '../../components/ui'
import { Icon } from '../../components/Icon'
import { useMe } from '../../lib/me'
import { useFormat } from '../../lib/useFormat'
import { peopleApi } from '../api'
import { canMarkSeen, markSeen, noticeLines } from '../lib/notices'
import { NOTICE_TEXT, signInNotice, type SignInNotice } from '../lib/signIn'

/**
 * Why a sign-in begun from the dashboard did not happen, for a person who has a session: the company's sign-in comes
 * back with `?sign-in=<reason>`, and a refused **Link my company account** is one. The reason is read once, from the
 * address the dashboard was opened with: the router moves on from it, and the query would go with it.
 */
export function SignInReason() {
  const { t } = useTranslation()
  const [reason, setReason] = useState<SignInNotice | null>(() =>
    typeof window === 'undefined' ? null : signInNotice(window.location.search),
  )
  if (reason === null) return null
  return (
    <div
      role="alert"
      className="mb-5 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink"
    >
      <p className="min-w-0 max-w-[80ch]">{t(NOTICE_TEXT[reason])}</p>
      <button type="button" className="btn btn-sm" onClick={() => setReason(null)}>
        {t('common.dismiss')}
      </button>
    </div>
  )
}

/**
 * The computers connected as the person and the codes made for them, for 14 days after each (or until they say they
 * saw them): so that a computer or a code they never made is not unseen. It can be marked as seen from a session the
 * company's sign-in opened alone; from any other it goes by itself, and it says so.
 */
export function ConnectedNotice() {
  const { t } = useTranslation()
  const toast = useToast()
  const fmt = useFormat()
  const { me, reload } = useMe()
  const [busy, setBusy] = useState(false)
  const connected = me?.edition?.connected
  if (!connected) return null
  const seen = async () => {
    setBusy(true)
    const came = await markSeen(connected.until, peopleApi.told, reload)
    if (!came.ok) toast.show(errorText(t, came.error), 'fail')
    setBusy(false)
  }
  return (
    <section
      aria-label={t('ee.notice.title')}
      className="mb-5 space-y-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2.5 text-ui text-ink"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="flex items-center gap-2 font-medium">
          <Icon name="info" size={15} className="flex-none text-warn-ink" />
          {t('ee.notice.title')}
        </p>
        {canMarkSeen(me) && (
          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void seen()}>
            {t('ee.notice.seen')}
          </button>
        )}
      </div>
      <ul className="list-disc space-y-0.5 pl-9">
        {noticeLines(connected).map((line) => (
          <li key={line.key}>{t(line.text, { label: line.label ?? '', when: fmt.relative(line.when) })}</li>
        ))}
      </ul>
      {connected.more > 0 && <p className="pl-6 text-ink-2">{t('ee.notice.more', { count: connected.more })}</p>}
      <p className="pl-6 text-ink-2">{t('ee.notice.advice')}</p>
      {!canMarkSeen(me) && <p className="pl-6 text-ink-2">{t('ee.notice.goesByItself')}</p>}
    </section>
  )
}
