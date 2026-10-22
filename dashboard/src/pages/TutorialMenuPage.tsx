// The tutorial's menu: one card per chapter (what you learn, how long, done or not), and how to use Latet
// for real afterwards. The chapter named on the command line (`latet tutorial terms`) opens at once.

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { EndTutorialButton, TutorialEnded } from '../components/tutorial/EndTutorial'
import { RealUse } from '../components/tutorial/RealUse'
import { ErrorState, Loading, PageHeader } from '../components/ui'
import { CMD_OCR_INSTALL } from '../lib/commands'
import { cannotStart, menuCards, nextCard, openPath, requestedCard, type MenuCard } from '../lib/tutorial'
import { useAsync } from '../lib/useAsync'

export function TutorialMenuPage() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const lang = i18n.resolvedLanguage ?? 'en'
  const menu = useAsync(`tutorial:menu:${lang}`, (signal) => api.tutorial.chapters(lang, signal))
  const [ended, setEnded] = useState(false)
  const requested = menu.data ? (requestedCard(menu.data)?.id ?? null) : null

  useEffect(() => {
    if (requested) navigate(`/tutorial/${encodeURIComponent(requested)}`, { replace: true })
  }, [requested, navigate])

  if (ended) return <TutorialEnded />
  const header = (
    <PageHeader
      title={t('tutorial.title')}
      description={t('tutorial.description')}
      actions={<EndTutorialButton onEnded={() => setEnded(true)} />}
      docs="page.tutorial"
    />
  )
  if (menu.error && !menu.data) return <>{header}<ErrorState error={menu.error} onRetry={menu.reload} /></>
  if (!menu.data || requested) return <>{header}<Loading /></>
  const cards = menuCards(menu.data)
  const next = nextCard(cards) // stands out: the chapter in progress, else the first not done yet

  return (
    <>
      {header}
      <p className="mb-5 flex max-w-[100ch] items-start gap-2 rounded-md border border-brand/50 bg-brand/10 px-3 py-2 text-ui text-ink">
        <Icon name="shield" size={15} className="mt-0.5 flex-none text-brand-ink" />
        {t('tutorial.sandbox')}
      </p>
      <ol className="grid grid-cols-[repeat(auto-fill,minmax(270px,1fr))] gap-3" aria-label={t('tutorial.menu.label')}>
        {cards.map((card) => (
          <ChapterCard key={card.id} card={card} primary={card === next} />
        ))}
      </ol>
      <section aria-labelledby="tutorial-real" className="panel mt-8 max-w-[880px] p-5">
        <h2 id="tutorial-real" className="text-title font-semibold text-ink">
          {t('tutorial.real.title')}
        </h2>
        <p className="mt-1 mb-4 text-ui text-ink-2">{t('tutorial.real.intro')}</p>
        <RealUse />
      </section>
    </>
  )
}

function ChapterCard({ card, primary }: { card: MenuCard; primary: boolean }) {
  const { t } = useTranslation()
  const titleId = `chapter-${card.id}`
  return (
    <li className={`panel flex flex-col p-4 ${card.state === 'current' ? 'border-brand/60' : ''}`}>
      <div className="flex items-center justify-between gap-2 text-meta text-ink-3">
        <span>
          {t('tutorial.menu.chapter', { number: card.position })} · {t('tutorial.menu.minutes', { count: card.minutes })}
        </span>
        {card.state === 'done' && (
          <span className="tag border-good/50 bg-good/10 text-good-ink">
            <Icon name="check" size={12} />
            {t('tutorial.menu.done')}
          </span>
        )}
        {card.state === 'current' && <span className="tag border-brand/60 bg-brand/10 text-ink">{t('tutorial.menu.current')}</span>}
      </div>
      <h2 id={titleId} className="mt-1.5 text-title font-semibold text-ink">
        {card.title}
      </h2>
      <p className="mt-2 text-meta font-medium text-ink-2">{t('tutorial.menu.learn')}</p>
      <ul className="mt-1 space-y-1 text-ui text-ink-2">
        {card.learn.map((line, i) => (
          <li key={i} className="flex items-start gap-2">
            <span className="mt-[7px] size-1 flex-none rounded-full bg-ink-3" aria-hidden="true" />
            {line}
          </li>
        ))}
      </ul>
      <div className="mt-auto pt-4">
        {cannotStart(card) ? (
          // What it needs is not on this computer yet: in place of Start, what to install. Opening the chapter looks again.
          // A chapter in progress keeps its Continue: its run opens as it stands.
          <div className="space-y-2 text-ui text-ink-2">
            <p>
              {t('tutorial.menu.needs')} <code className="mono whitespace-nowrap text-ink">{CMD_OCR_INSTALL}</code>
            </p>
            <Link to={openPath(card)} className="link" aria-describedby={titleId}>
              {t('tutorial.needs.again')}
            </Link>
          </div>
        ) : (
          <Link to={openPath(card)} className={`btn ${primary ? 'btn-primary' : ''}`} aria-describedby={titleId}>
            <Icon name={card.state === 'current' ? 'chevronRight' : card.state === 'done' ? 'refresh' : 'play'} size={13} />
            {card.state === 'current' ? t('tutorial.menu.continue') : card.state === 'done' ? t('tutorial.menu.again') : t('tutorial.menu.start')}
          </Link>
        )}
      </div>
    </li>
  )
}
