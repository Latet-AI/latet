// In the tutorial's sandbox every page of the dashboard says so, with the way back to the chapter in
// progress: explore freely, and come back. It sits at the top of the page and scrolls with it, so it
// never covers what a page keeps in view (a list, a save bar).

import { useTranslation } from 'react-i18next'
import { Link, useLocation } from 'react-router'
import { useMe } from '../../lib/me'
import { resumePath } from '../../lib/tutorial'
import { Icon } from '../Icon'

export function TutorialBar() {
  const { t } = useTranslation()
  const { me } = useMe()
  const { pathname } = useLocation()
  const to = resumePath(me)
  // The tutorial's own pages are where the bar leads.
  if (to === null || pathname === '/tutorial' || pathname.startsWith('/tutorial/')) return null
  const current = me?.tutorial?.current ?? null
  return (
    <div
      role="region"
      aria-label={t('tutorial.bar.label')}
      className="mb-5 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-md border border-brand/50 bg-brand/10 px-3 py-2 text-ui text-ink"
    >
      <p className="flex items-center gap-2">
        <Icon name="book" size={15} className="flex-none text-brand-ink" />
        {t('tutorial.bar.text')}
      </p>
      <Link to={to} className="btn btn-sm">
        <Icon name="arrowLeft" size={13} />
        {current ? t('tutorial.bar.chapter', { number: current.number, title: current.title }) : t('tutorial.bar.menu')}
      </Link>
    </div>
  )
}
