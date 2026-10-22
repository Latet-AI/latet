// Ending the tutorial (`POST /tutorial/end`): the sandbox stops and its folder is deleted, and the page
// says so, since nothing answers it any more. On a real daemon, where it does not run, how to start it.

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { CMD_TUTORIAL } from '../../lib/commands'
import { useDeploymentKey } from '../../lib/me'
import { DocsPageLink } from '../DocsLinks'
import { Icon } from '../Icon'
import { useToast } from '../Toast'
import { CommandLine, ConfirmDialog, EmptyState, errorText } from '../ui'

export function EndTutorialButton({ onEnded }: { onEnded: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const end = async () => {
    setBusy(true)
    try {
      await api.tutorial.end()
      setOpen(false)
      onEnded()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className="btn btn-ghost" onClick={() => setOpen(true)}>
        <Icon name="x" size={14} />
        {t('tutorial.end.action')}
      </button>
      <ConfirmDialog
        open={open}
        title={t('tutorial.end.title')}
        confirmLabel={t('tutorial.end.action')}
        onConfirm={() => void end()}
        onCancel={() => setOpen(false)}
        busy={busy}
      >
        <p>{t('tutorial.end.body')}</p>
      </ConfirmDialog>
    </>
  )
}

export function TutorialEnded() {
  const { t } = useTranslation()
  return (
    <div className="panel">
      <EmptyState icon="check" title={t('tutorial.ended.title')}>
        <p>{t('tutorial.ended.body')}</p>
        <p className="mt-3">{t('tutorial.ended.again')}</p>
        <div className="mt-2 text-left">
          <CommandLine command={CMD_TUTORIAL} />
        </div>
      </EmptyState>
    </div>
  )
}

/** The tutorial's pages on your own daemon: the tutorial runs in a sandbox of its own, started from a terminal. */
export function TutorialElsewhere() {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  return (
    <>
      {/* The documentation of the tutorial, at the top right as on every page. */}
      <div className="flex justify-end pb-3">
        <DocsPageLink target="page.tutorial" />
      </div>
      <div className="panel">
        <EmptyState icon="info" title={t('tutorial.elsewhere.title')}>
          <p>{t(key('tutorial.elsewhere.body'))}</p>
          <div className="mt-3 text-left">
            <CommandLine command={CMD_TUTORIAL} />
          </div>
        </EmptyState>
      </div>
    </>
  )
}
