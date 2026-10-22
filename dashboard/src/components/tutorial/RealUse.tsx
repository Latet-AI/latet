// After the sandbox: the commands that put Latet in front of your own agent (the menu, and the last
// chapter's end).

import { useTranslation } from 'react-i18next'
import { CMD_CLAUDE, CMD_CODEX, CMD_COMPANY_UPSTREAM, CMD_GLOBAL_INSTALL } from '../../lib/commands'
import { CommandLine, InlineCodeText } from '../ui'

export function RealUse() {
  const { t } = useTranslation()
  return (
    <div className="space-y-4 text-ui text-ink-2">
      <div className="space-y-2">
        <p>{t('tutorial.real.agents')}</p>
        <div className="grid gap-2 sm:grid-cols-2">
          <CommandLine command={CMD_CLAUDE} />
          <CommandLine command={CMD_CODEX} />
        </div>
      </div>
      <div className="space-y-2">
        <p>{t('tutorial.real.global')}</p>
        <CommandLine command={CMD_GLOBAL_INSTALL} />
      </div>
      <div className="space-y-2">
        <p>
          <InlineCodeText text={t('tutorial.real.company')} />
        </p>
        <CommandLine command={CMD_COMPANY_UPSTREAM} />
      </div>
    </div>
  )
}
