import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { EmptyState } from '../components/ui'

export function NotFoundPage() {
  const { t } = useTranslation()
  return (
    <EmptyState
      icon="question"
      title={t('notFound.title')}
      action={
        <Link to="/live" className="btn">
          {t('notFound.action')}
        </Link>
      }
    >
      {t('notFound.body')}
    </EmptyState>
  )
}
