import { useTranslation } from 'react-i18next'
import { Link, type To } from 'react-router'
import { pageNumbers } from '../lib/pages'
import { Icon } from './Icon'

/** The page links under a long list, as GitHub draws them: Previous, the numbers with gaps, Next. Each page has its own
 *  address (`to`), so a page can be opened in a new tab and Back returns to the page before. Hidden on a single page. */
export function Pagination({ page, pages, to }: { page: number; pages: number; to: (page: number) => To }) {
  const { t } = useTranslation()
  if (pages <= 1) return null
  const previous = (
    <>
      <Icon name="chevronLeft" size={13} />
      {t('pagination.previous')}
    </>
  )
  const next = (
    <>
      {t('pagination.next')}
      <Icon name="chevronRight" size={13} />
    </>
  )
  return (
    <nav aria-label={t('pagination.label')} className="mt-3 flex flex-wrap items-center justify-center gap-1">
      {page > 1 ? (
        <Link className="btn btn-ghost btn-sm" to={to(page - 1)} rel="prev">
          {previous}
        </Link>
      ) : (
        <span className="btn btn-ghost btn-sm" aria-disabled="true">
          {previous}
        </span>
      )}
      {pageNumbers(page, pages).map((number, i) =>
        number === 'gap' ? (
          <span key={`gap-${i}`} className="px-1 text-meta text-ink-3" aria-hidden="true">
            …
          </span>
        ) : number === page ? (
          <span key={number} className="btn btn-primary btn-sm tabular-nums" aria-current="page" aria-label={t('pagination.page', { page: number })}>
            {number}
          </span>
        ) : (
          <Link key={number} className="btn btn-ghost btn-sm tabular-nums" to={to(number)} aria-label={t('pagination.page', { page: number })}>
            {number}
          </Link>
        ),
      )}
      {page < pages ? (
        <Link className="btn btn-ghost btn-sm" to={to(page + 1)} rel="next">
          {next}
        </Link>
      ) : (
        <span className="btn btn-ghost btn-sm" aria-disabled="true">
          {next}
        </span>
      )}
    </nav>
  )
}
