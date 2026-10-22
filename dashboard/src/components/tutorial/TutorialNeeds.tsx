// A chapter that needs what this computer does not have yet, text recognition's models: what to install, and a
// button that looks again, as the sandbox looks for them each time it is asked.

import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { TutorialChapter } from '../../api/types'
import { CMD_OCR_INSTALL } from '../../lib/commands'
import { Icon } from '../Icon'
import { CommandLine, EmptyState, PageHeader } from '../ui'

export function TutorialNeeds({ chapter, onAgain }: { chapter: TutorialChapter; onAgain: () => void }) {
  const { t } = useTranslation()
  return (
    <>
      <Link to="/tutorial" className="mb-3 inline-flex items-center gap-1.5 text-ui text-ink-2 hover:text-ink">
        <Icon name="arrowLeft" size={14} />
        {t('tutorial.chapter.back')}
      </Link>
      <PageHeader title={chapter.title} docs="page.tutorial" />
      <div className="panel">
        <EmptyState
          icon="image"
          title={t('tutorial.needs.title')}
          action={
            <button type="button" className="btn btn-primary" onClick={onAgain}>
              <Icon name="refresh" size={14} />
              {t('tutorial.needs.again')}
            </button>
          }
        >
          <p>{t('tutorial.needs.body')}</p>
          <div className="mt-3 text-left">
            <CommandLine command={CMD_OCR_INSTALL} />
          </div>
        </EmptyState>
      </div>
    </>
  )
}
