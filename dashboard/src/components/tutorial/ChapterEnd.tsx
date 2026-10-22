// The end of a chapter: what it taught, where that is in the dashboard, and the chapter after it (or, after
// the last, how to use Latet for real, and the chapters).

import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { TutorialEnd } from '../../api/types'
import { isDashboardPath } from '../../lib/tutorial'
import { Icon, StatusGlyph } from '../Icon'
import { PrimaryButton } from './PrimaryButton'
import { RealUse } from './RealUse'

export function ChapterEnd({ end, nextTitle, onNext }: { end: TutorialEnd; nextTitle: string | null; onNext: () => void }) {
  const { t } = useTranslation()
  const titleId = useId()
  const where = end.where.filter((place) => isDashboardPath(place.to))
  const label = end.next
    ? nextTitle
      ? t('tutorial.done.next', { title: nextTitle })
      : t('tutorial.done.nextUntitled')
    : t('tutorial.done.menu')
  return (
    <section aria-labelledby={titleId} className="panel p-5">
      <p className="flex items-center gap-2 text-ui font-medium text-good-ink">
        <StatusGlyph status="ok" size={16} />
        {end.next ? t('tutorial.done.badge') : t('tutorial.done.badgeLast')}
      </p>
      <h2 id={titleId} className="mt-3 text-title font-semibold text-ink">
        {t('tutorial.done.learned')}
      </h2>
      <ul className="mt-2 space-y-1.5 text-body text-ink">
        {end.learned.map((line, i) => (
          <li key={i} className="flex items-start gap-2">
            <Icon name="check" size={15} className="mt-[3px] flex-none text-good-ink" />
            {line}
          </li>
        ))}
      </ul>

      {where.length > 0 && (
        <>
          <h3 className="mt-5 text-ui font-semibold text-ink">{t('tutorial.done.where')}</h3>
          <ul className="mt-1.5 space-y-1 text-ui">
            {where.map((place) => (
              <li key={place.to}>
                <Link to={place.to} className="link">
                  {place.label}
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}

      {!end.next && (
        <>
          <h3 className="mt-5 text-ui font-semibold text-ink">{t('tutorial.done.tryReal')}</h3>
          <div className="mt-2">
            <RealUse />
          </div>
        </>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-line pt-4">
        <PrimaryButton label={label} icon={end.next ? 'chevronRight' : 'book'} onClick={onNext} />
        {end.next && (
          <Link to="/tutorial" className="btn">
            {t('tutorial.done.menu')}
          </Link>
        )}
      </div>
    </section>
  )
}
