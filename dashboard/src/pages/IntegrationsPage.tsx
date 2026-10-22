import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { AgentIntegration, CheckStatus, DestinationUse } from '../api/types'
import { DocsSectionLink } from '../components/DocsLinks'
import { Icon, StatusGlyph } from '../components/Icon'
import { useToast } from '../components/Toast'
import { CommandLine, EmptyState, ErrorState, errorText, InlineCodeText, Loading, PageHeader } from '../components/ui'
import { tk } from '../i18n'
import { CMD_DOCTOR, CMD_GLOBAL_INSTALL, fixCommand } from '../lib/commands'
import { pendingKey } from '../lib/deployment'
import { extractCommands } from '../lib/inlineCode'
import { CHECK_LABEL } from '../lib/labels'
import { useDeploymentKey, useMe } from '../lib/me'
import { useAsync } from '../lib/useAsync'
import { useFormat } from '../lib/useFormat'

// The agent whose settings named a destination.
const READ_FROM: Record<string, string> = { claude: tk('agents.claude-code'), codex: tk('agents.codex') }

const AGENT_STATUS: Record<string, { label: string; status: CheckStatus | null }> = {
  applied: { label: tk('integrations.agents.applied'), status: 'ok' },
  partial: { label: tk('integrations.agents.partial'), status: 'warn' },
  wrapper: { label: tk('integrations.agents.wrapper'), status: 'ok' },
  'not applied': { label: tk('integrations.agents.notApplied'), status: 'warn' },
  'not installed': { label: tk('integrations.agents.notInstalled'), status: null },
}

function agentStatus(agent: AgentIntegration): { label: string; status: CheckStatus | null } {
  if (!agent.installed) return AGENT_STATUS['not installed']!
  return AGENT_STATUS[agent.status] ?? { label: agent.status, status: 'warn' }
}

export function IntegrationsPage() {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { me, unreachable } = useMe()
  const data = useAsync('integrations', (signal) => api.integrations(signal))

  const header = (
    <PageHeader
      title={t('integrations.title')}
      description={t(key('integrations.description'))}
      docs="page.agents"
      actions={
        <button type="button" className="btn" onClick={data.reload} disabled={data.stale}>
          <Icon name="refresh" size={14} />
          {t('integrations.recheck')}
        </button>
      }
    />
  )
  if (data.loading) return <>{header}<Loading /></>
  if (!data.data) return <>{header}<ErrorState error={data.error} onRetry={data.reload} /></>
  const { checks, agents, destinations = [] } = data.data
  const order: CheckStatus[] = ['fail', 'warn', 'ok']
  const sorted = [...checks].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status))
  const problems = checks.filter((c) => c.status !== 'ok').length

  return (
    <div className={data.stale ? 'opacity-60' : undefined}>
      {header}

      <section aria-labelledby="checks-title">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="checks-title" className="text-title font-semibold text-ink">
            {t('integrations.checks.title')}
          </h2>
          <p className="text-ui text-ink-2" role="status">
            {problems === 0 ? t('integrations.checks.allGood') : t('integrations.checks.problems', { count: problems })}
          </p>
        </div>
        {checks.length === 0 ? (
          <div className="panel">
            <EmptyState icon="integrations" title={t('integrations.checks.empty')} />
          </div>
        ) : (
          <ul className="panel divide-y divide-line">
            {sorted.map((check, i) => {
              const commands = extractCommands(check.detail)
              return (
                <li key={`${check.title}:${i}`} className="flex items-start gap-3 px-4 py-3">
                  <span className="mt-0.5">
                    <StatusGlyph status={check.status} label={t(CHECK_LABEL[check.status])} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-ui font-medium text-ink">{check.title}</p>
                    {check.detail && (
                      <p className="mt-0.5 text-ui text-ink-2">
                        <InlineCodeText text={check.detail} />
                      </p>
                    )}
                    {check.status !== 'ok' && commands.length > 0 && (
                      <div className="mt-2 grid max-w-[520px] gap-1.5">
                        <p className="text-meta text-ink-3">{t('integrations.fixWith')}</p>
                        {commands.map((command) => (
                          <CommandLine key={command} command={command} />
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby="agents-title" className="mt-8">
        <h2 id="agents-title" className="mb-3 text-title font-semibold text-ink">
          {t('integrations.agents.title')}
        </h2>
        {me === undefined ? (
          <p className="panel px-4 py-3 text-ui text-ink-2">{t(pendingKey(unreachable))}</p>
        ) : me.server ? (
          // On a Latet server: the agents are on the person's computers, which the server does not look at.
          <div className="panel space-y-2 px-4 py-3">
            <p className="text-ui text-ink-2">{t('integrations.server')}</p>
            <div className="max-w-[520px]">
              <CommandLine command={CMD_DOCTOR} />
            </div>
          </div>
        ) : agents.length === 0 ? (
          <div className="panel">
            <EmptyState icon="integrations" title={t('integrations.agents.empty')} />
          </div>
        ) : (
          <>
            <p className="mb-3 max-w-[760px] text-meta text-ink-3">{t('integrations.agents.statusHelp')}</p>
            <div className="panel overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">{t('integrations.agents.agent')}</th>
                    <th scope="col">{t('integrations.agents.installed')}</th>
                    <th scope="col">{t('integrations.agents.version')}</th>
                    <th scope="col">{t('integrations.agents.status')}</th>
                    <th scope="col">{t('integrations.agents.route')}</th>
                    <th scope="col">{t('integrations.agents.notes')}</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.map((agent) => {
                    const status = agentStatus(agent)
                    const fix = fixCommand(agent)
                    const notes = [...agent.notes, ...(agent.route?.notes ?? [])]
                    const noteCommands = notes.flatMap(extractCommands)
                    return (
                      <tr key={agent.id}>
                        <td className="font-medium whitespace-nowrap text-ink">{agent.name}</td>
                        <td className="whitespace-nowrap text-ink-2">{agent.installed ? t('common.yes') : t('common.no')}</td>
                        <td className="mono whitespace-nowrap text-ink-2">{agent.version ?? t('common.none')}</td>
                        <td className="whitespace-nowrap">
                          <span className="inline-flex items-center gap-1.5 text-ink">
                            {status.status ? <StatusGlyph status={status.status} size={14} /> : <Icon name="minus" size={14} className="text-ink-3" />}
                            {t(status.label)}
                          </span>
                        </td>
                        <td className="min-w-[220px] text-ui">
                          {agent.route?.problem ? (
                            <span className="inline-flex items-start gap-1.5 text-ink">
                              <StatusGlyph status="warn" size={14} />
                              <span>
                                {t('integrations.agents.routeProblem')} <InlineCodeText text={agent.route.problem} />
                              </span>
                            </span>
                          ) : agent.route?.summary ? (
                            <span className="text-ink-2">
                              <InlineCodeText text={agent.route.summary} />
                            </span>
                          ) : (
                            <span className="text-ink-3">{t('common.none')}</span>
                          )}
                        </td>
                        <td className="min-w-[280px]">
                          {notes.length === 0 && !fix ? (
                            <span className="text-ink-3">{t('common.none')}</span>
                          ) : (
                            <div className="space-y-2">
                              {notes.map((note, i) => (
                                <p key={i} className="text-ui text-ink-2">
                                  <InlineCodeText text={note} />
                                </p>
                              ))}
                              {fix && (
                                <div className="max-w-[420px] space-y-1">
                                  <p className="text-meta text-ink-3">{t('integrations.agents.fix')}</p>
                                  <CommandLine command={fix} />
                                </div>
                              )}
                              {noteCommands.map((command) => (
                                <div key={command} className="max-w-[420px]">
                                  <CommandLine command={command} />
                                </div>
                              ))}
                            </div>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <Destinations destinations={destinations} onChange={data.reload} />

      <section className="mt-8 grid max-w-[640px] gap-2">
        <h2 className="text-title font-semibold text-ink">{t('integrations.cli.title')}</h2>
        <p className="text-ui text-ink-2">{t(key('integrations.cli.body'))}</p>
        {me !== undefined && (
          <>
            <CommandLine command={CMD_DOCTOR} />
            <CommandLine command={CMD_GLOBAL_INSTALL} />
          </>
        )}
      </section>
    </div>
  )
}

/** Your company's endpoints the agents reach through Latet: where each goes, which agent's settings
 * named it, when it was last used; one no agent uses may be removed, that one alone (the daemon refuses
 * one still in use, whatever the list on the page says). */
function Destinations({ destinations, onChange }: { destinations: DestinationUse[]; onChange: () => void }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const fmt = useFormat()
  const toast = useToast()
  const [busy, setBusy] = useState<string | null>(null)

  const remove = async (name: string) => {
    if (busy) return
    setBusy(name)
    try {
      await api.removeDestination(name)
      toast.show(t('integrations.destinations.removed', { name }))
      onChange()
    } catch (err) {
      toast.show(errorText(t, err), 'fail')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section aria-labelledby="destinations-title" className="mt-8">
      <div className="mb-1 flex items-center gap-1">
        <h2 id="destinations-title" className="text-title font-semibold text-ink">
          {t('integrations.destinations.title')}
        </h2>
        <DocsSectionLink target="section.destinations" section={t('integrations.destinations.title')} />
      </div>
      <p className="mb-3 max-w-[720px] text-ui text-ink-2">{t(key('integrations.destinations.help'))}</p>
      {destinations.length === 0 ? (
        <div className="panel">
          <EmptyState icon="integrations" title={t(key('integrations.destinations.empty'))} />
        </div>
      ) : (
        <div className="panel overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('integrations.destinations.name')}</th>
                <th scope="col">{t('integrations.destinations.address')}</th>
                <th scope="col">{t('integrations.destinations.from')}</th>
                <th scope="col">{t('integrations.destinations.lastUsed')}</th>
                <th scope="col">{t('integrations.destinations.use')}</th>
                <th scope="col">
                  <span className="sr-only">{t('integrations.destinations.remove')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {destinations.map((destination) => (
                <tr key={destination.name}>
                  <td className="mono whitespace-nowrap text-ink">{destination.name}</td>
                  <td className="mono break-all text-ink-2">{destination.url}</td>
                  <td className="whitespace-nowrap text-ink-2">
                    {destination.shared
                      ? t('integrations.destinations.shared')
                      : destination.agent
                        ? t(READ_FROM[destination.agent] ?? destination.agent)
                        : t('integrations.destinations.fromYou')}
                  </td>
                  <td className="whitespace-nowrap text-ink-2">
                    {destination.last_used ? fmt.relative(destination.last_used) : t('integrations.destinations.never')}
                  </td>
                  <td className="whitespace-nowrap text-ink-2">
                    {destination.in_use ? t(key('integrations.destinations.inUse')) : t(key('integrations.destinations.unused'))}
                  </td>
                  <td className="whitespace-nowrap text-right">
                    {!destination.in_use && !destination.shared && (
                      <button type="button" className="btn btn-sm" disabled={busy !== null} onClick={() => void remove(destination.name)}>
                        <Icon name="x" size={13} />
                        {busy === destination.name ? t('integrations.destinations.removing') : t('integrations.destinations.remove')}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
