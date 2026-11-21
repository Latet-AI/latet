// Latet Enterprise, as whoever waits sees it: what someone else decides on (the company's security team, or the people
// who control an API key), and who they are. Nothing shows on the Community Edition: there, everything waits for you.

import { useTranslation } from 'react-i18next'
import { decidersText, moreDeciders } from '../lib/approvals'
import { useMe } from '../lib/me'
import { useFormat } from '../lib/useFormat'
import { Icon } from './Icon'

/** Where your changes that lower protection go and, with `both`, your Live Reviewer's texts, when it is not you. */
export function WhoDecidesNote({ show }: { show: 'changes' | 'both' }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const { me } = useMe()
  const routes = me?.edition?.approvals
  if (!routes) return null
  const changeNames = decidersText(t, fmt.list, routes.deciders, routes.deciders_more ?? 0)
  const lines: string[] = []
  if (routes.changes === 'security') lines.push(t('approvals.whoDecides.changesSecurity', { names: changeNames }))
  if (routes.changes === 'controllers') lines.push(t('approvals.whoDecides.changesControllers', { names: changeNames }))
  if (show === 'both' && routes.live_review === 'security') {
    lines.push(t('approvals.whoDecides.texts', { names: decidersText(t, fmt.list, routes.text_deciders, routes.text_deciders_more ?? 0) }))
  }
  if (lines.length === 0) return null
  return (
    <div role="note" className="mb-4 flex items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-ui text-ink">
      <Icon name="shield" size={15} className="mt-0.5 flex-none" />
      <div className="space-y-1">
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
      </div>
    </div>
  )
}

/** Above a text your company's security team reviews: they see it, values included, and you may only stop it. */
export function TeamReviewsBanner({ deciders }: { deciders: readonly string[] | undefined }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const { me } = useMe()
  // The text names 20 at most: a full list is told with how many more /me counts for the texts' route.
  const more = moreDeciders(deciders, me?.edition?.approvals?.text_deciders_more)
  return (
    <p role="status" className="panel flex items-start gap-2 border-warn/60 bg-warn/10 px-3 py-2 text-ui text-ink">
      <Icon name="shield" size={15} className="mt-0.5 flex-none" />
      {t('approvals.whoDecides.batch', { names: decidersText(t, fmt.list, deciders, more) })}
    </p>
  )
}
