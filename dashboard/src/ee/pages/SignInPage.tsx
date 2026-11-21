import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { Icon } from '../../components/Icon'
import { Section } from '../../components/rules'
import { CommandLine, ConfirmDialog, CopyButton, ErrorState, InlineCodeText, Loading, PageHeader } from '../../components/ui'
import { tk } from '../../i18n'
import { CMD_SERVER_COMPANY_CHANGES, CMD_SERVER_SIGN_IN_SET } from '../../lib/commands'
import { useMe } from '../../lib/me'
import { useAsync } from '../../lib/useAsync'
import { useFormat } from '../../lib/useFormat'
import { peopleApi } from '../api'
import { Banner } from '../components/Banner'
import { ChangedMeanwhile, CompanySignInWay } from '../components/CompanyWay'
import { connectShown, usePanels } from '../components/Shown'
import { useEnterpriseReadOnly } from '../components/TrialEnded'
import { useRun } from '../components/useRun'
import {
  adminByName,
  connectRefused,
  draftProblems,
  editsSettings,
  fromDraft,
  googleIssuer,
  LINK_START,
  saveFrom,
  seesSettings,
  toDraft,
  type DraftProblem,
  type GroupRow,
  type SignInDraft,
} from '../lib/signIn'
import type { Group, ScimTokenInfo, SignInTest, SignInView, SsoSettings } from '../types'

const PROBLEM: Record<DraftProblem, string> = {
  issuer: tk('ee.signIn.problems.issuer'),
  clientId: tk('ee.signIn.problems.clientId'),
  openid: tk('ee.signIn.problems.openid'),
  claimEmpty: tk('ee.signIn.problems.claimEmpty'),
  claimTwice: tk('ee.signIn.problems.claimTwice'),
  groupName: tk('ee.signIn.problems.groupName'),
  accessGroup: tk('ee.signIn.problems.accessGroup'),
  accessGroupOpen: tk('ee.signIn.problems.accessGroupOpen'),
  domains: tk('ee.signIn.problems.domains'),
  domainsNotGoogle: tk('ee.signIn.problems.domainsNotGoogle'),
  domain: tk('ee.signIn.problems.domain'),
}

const ACCOUNTS: Record<SsoSettings['accounts'], { label: string; help: string }> = {
  scim: { label: tk('ee.signIn.sso.accountsScim'), help: tk('ee.signIn.sso.accountsScimHelp') },
  group: { label: tk('ee.signIn.sso.accountsGroup'), help: tk('ee.signIn.sso.accountsGroupHelp') },
  open: { label: tk('ee.signIn.sso.accountsOpen'), help: tk('ee.signIn.sso.accountsOpenHelp') },
}

const COMPANY_CHANGES = {
  sso: tk('ee.signIn.company.sso'),
  'any-session': tk('ee.signIn.company.anySession'),
  none: tk('ee.signIn.company.none'),
}

export function SignInPage() {
  const { t } = useTranslation()
  const { me } = useMe()
  const edition = me?.edition
  const readOnly = useEnterpriseReadOnly() // a trial that ended: read, not changed; no code to connect (R3, R16)
  const sees = edition ? seesSettings(edition) : false
  const edits = !readOnly && (edition ? editsSettings(edition) : false)
  const view = useAsync(sees ? 'ee-sign-in' : null, (signal) => peopleApi.signIn(signal))
  const groups = useAsync(edits ? 'ee-groups' : null, (signal) => peopleApi.groups(signal)) // to warn of a group that gives Admin by its name
  // A save refused because someone else changed the settings meanwhile: they are read again and the form starts from
  // them anew, so what says it is kept here, above the form.
  const [changed, setChanged] = useState(false)
  return (
    <div className={view.stale ? 'opacity-60' : undefined}>
      <PageHeader title={t('ee.signIn.title')} description={t('ee.signIn.description')} />
      {!readOnly && (!sees || view.data?.sso) && <ConnectComputer />}
      {!readOnly && <LinkAccount />}
      {sees && view.loading && <Loading />}
      {sees && !view.loading && !view.data && <ErrorState error={view.error} onRetry={view.reload} />}
      {view.data && (
        <>
          {changed && <ChangedMeanwhile text={t('ee.signIn.changed')} />}
          <SingleSignOn
            key={JSON.stringify(view.data.sso)}
            view={view.data}
            edits={edits}
            reload={view.reload}
            groups={groups.data?.groups ?? []}
            onChanged={setChanged}
          />
          <Provisioning view={view.data} edits={edits} reload={view.reload} />
          <CompanyChanges view={view.data} />
        </>
      )}
    </div>
  )
}

/**
 * A one-time code for `latet connect`, for the person signed in. Past the server's limits (the most computers connected
 * to their account, the most codes of an hour) it says so above the button, in the reader's words, and no code is made.
 */
function ConnectComputer() {
  const { t } = useTranslation()
  const [busy, run, company] = useRun(() => undefined)
  const [refused, setRefused] = useState<string | null>(null)
  const { show, panels, held } = usePanels() // the code stays until its own Close, and a second is made once it is closed
  return (
    <Section title={t('ee.signIn.connect.title')} description={t('ee.signIn.connect.description')}>
      {company && <CompanySignInWay />}
      {refused && (
        <Banner key={refused} className="mb-4 rounded-md border border-warn/60 bg-warn/10 px-4 py-3 text-ui text-ink">
          <p className="max-w-[80ch]">
            <InlineCodeText text={refused} />
          </p>
        </Banner>
      )}
      {panels}
      {held === 0 && (
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              setRefused(null)
              let made
              try {
                made = await peopleApi.connectCode()
              } catch (error) {
                const said = connectRefused(t, error)
                if (said === null) throw error // said as every refusal is
                setRefused(said) // the banner is all it says: no toast repeats it
                return
              }
              show(connectShown(t('ee.signIn.connect.shown'), made, { command: t('ee.shown.command'), code: t('ee.shown.code') }))
            })
          }
        >
          {t('ee.signIn.connect.make')}
        </button>
      )}
    </Section>
  )
}

/**
 * **Link my company account**, for a person who opened this dashboard with `latet dashboard`'s code and whose account is
 * not linked to the company's sign-in yet: the sign-in begins from here (a page of the server's own), and comes back
 * with the reason when it is refused. A session the company sign-in opened is a linked account's already. `/me` says
 * whether the person's own account is linked (`edition.linked`): only `false` offers it, so that nothing is offered
 * to a person who is linked, to an API key, or while the server could not tell.
 */
function LinkAccount() {
  const { t } = useTranslation()
  const { me } = useMe()
  const serving = useAsync('company-sign-in', (signal) => api.companySignIn(signal))
  if (!serving.data || me?.session?.via !== 'code' || me.server?.identity?.kind !== 'person' || me.edition?.linked !== false) return null
  return (
    <Section title={t('ee.signIn.link.title')} description={t('ee.signIn.link.description')}>
      <a className="btn btn-primary" href={LINK_START}>
        {t('ee.signIn.link.button')}
      </a>
    </Section>
  )
}

function Field({ label, help, value, onChange, wide = false }: { label: string; help?: string; value: string; onChange: (value: string) => void; wide?: boolean }) {
  const id = useId()
  return (
    <div className={wide ? 'sm:col-span-2' : undefined}>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input id={id} className="field w-full" value={value} spellCheck={false} onChange={(event) => onChange(event.target.value)} />
      {help && <p className="mt-1 text-meta text-ink-3">{help}</p>}
    </div>
  )
}

function SingleSignOn({
  view,
  edits,
  reload,
  groups,
  onChanged,
}: {
  view: SignInView
  edits: boolean
  reload: () => void
  groups: Group[]
  onChanged: (changed: boolean) => void
}) {
  const { t } = useTranslation()
  const accountsId = useId()
  const [busy, run, company] = useRun(reload, () => onChanged(true))
  const [draft, setDraft] = useState<SignInDraft>(() => toDraft(view.sso))
  const [secret, setSecret] = useState('')
  const [test, setTest] = useState<SignInTest | null>(null)
  const [turningOff, setTurningOff] = useState(false)
  const problems = draftProblems(draft)
  const risky = adminByName(draft.rows, groups)
  const set = <K extends keyof SignInDraft>(key: K, value: SignInDraft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const setRow = (index: number, row: GroupRow) => set('rows', draft.rows.map((other, i) => (i === index ? row : other)))
  // A save names the etag of the settings the form was made from: a change made meanwhile refuses it (useRun reads them again).
  const save = (sso: SsoSettings | null, done: string) => {
    onChanged(false)
    return run(() => saveFrom(view, sso, peopleApi.saveSignIn), done)
  }
  const runTest = () => run(async () => setTest(await peopleApi.testSignIn(fromDraft(draft, view.sso))))
  return (
    <Section title={t('ee.signIn.sso.title')} description={t('ee.signIn.sso.description')}>
      <div className="space-y-5">
        {company && <CompanySignInWay />}
        {!edits && <p className="text-ui text-ink-3">{t('ee.signIn.readOnly')}</p>}
        {view.sso === null && <p className="text-ui text-ink-2">{t('ee.signIn.sso.off')}</p>}
        <div>
          <p className="label">{t('ee.signIn.sso.redirect')}</p>
          <p className="flex items-center gap-2">
            <code className="mono text-ui text-ink">{view.redirect_uri}</code>
            <CopyButton text={view.redirect_uri} compact />
          </p>
        </div>
        <fieldset disabled={!edits || busy} className="grid gap-3 sm:grid-cols-2">
          <Field wide label={t('ee.signIn.sso.issuer')} help={view.sso ? t('ee.signIn.sso.issuerHelp') : undefined} value={draft.issuer} onChange={(value) => set('issuer', value)} />
          <Field label={t('ee.signIn.sso.clientId')} value={draft.clientId} onChange={(value) => set('clientId', value)} />
          <Field label={t('ee.signIn.sso.scopes')} value={draft.scopes} onChange={(value) => set('scopes', value)} />
          <Field
            label={t('ee.signIn.sso.userNameClaim')}
            help={view.sso ? t('ee.signIn.sso.userNameClaimHelp') : undefined}
            value={draft.userNameClaim}
            onChange={(value) => set('userNameClaim', value)}
          />
          <Field label={t('ee.signIn.sso.groupsClaim')} value={draft.groupsClaim} onChange={(value) => set('groupsClaim', value)} />
          <div className="sm:col-span-2">
            <label className="label" htmlFor={accountsId}>
              {t('ee.signIn.sso.accounts')}
            </label>
            <select id={accountsId} className="field w-full" value={draft.accounts} onChange={(event) => set('accounts', event.target.value as SsoSettings['accounts'])}>
              {(Object.keys(ACCOUNTS) as SsoSettings['accounts'][]).map((option) => (
                <option key={option} value={option}>
                  {t(ACCOUNTS[option].label)}
                </option>
              ))}
            </select>
            <p className={`mt-1 text-meta ${draft.accounts === 'open' ? 'text-warn-ink' : 'text-ink-3'}`}>{t(ACCOUNTS[draft.accounts].help)}</p>
          </div>
          <Field wide label={t('ee.signIn.sso.accessGroup')} help={t('ee.signIn.sso.accessGroupHelp')} value={draft.accessGroup} onChange={(value) => set('accessGroup', value)} />
          {(googleIssuer(draft.issuer) || draft.allowedDomains.trim() !== '') && (
            <Field wide label={t('ee.signIn.sso.allowedDomains')} help={t('ee.signIn.sso.allowedDomainsHelp')} value={draft.allowedDomains} onChange={(value) => set('allowedDomains', value)} />
          )}
        </fieldset>
        <div>
          <p className="label">{t('ee.signIn.sso.groups')}</p>
          <p className="mb-2 text-meta text-ink-3">{t('ee.signIn.sso.groupsHelp')}</p>
          {draft.rows.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('ee.signIn.sso.claim')}</th>
                  <th scope="col">{t('ee.signIn.sso.group')}</th>
                  <th scope="col">
                    <span className="sr-only">{t('ee.signIn.sso.removeGroup')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {draft.rows.map((row, index) => (
                  <tr key={index}>
                    <td>
                      <input className="field mono w-full" aria-label={t('ee.signIn.sso.claim')} value={row.claim} disabled={!edits || busy} spellCheck={false} onChange={(event) => setRow(index, { ...row, claim: event.target.value })} />
                    </td>
                    <td>
                      <input className="field w-full" aria-label={t('ee.signIn.sso.group')} value={row.group} disabled={!edits || busy} onChange={(event) => setRow(index, { ...row, group: event.target.value })} />
                    </td>
                    <td>
                      {edits && (
                        <button type="button" className="btn btn-ghost btn-sm btn-icon" aria-label={t('ee.signIn.sso.removeGroup')} disabled={busy} onClick={() => set('rows', draft.rows.filter((_, i) => i !== index))}>
                          <Icon name="x" size={12} />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {risky.map((row, index) => (
            <p key={index} role="alert" className="mt-2 flex items-start gap-1.5 text-ui text-warn-ink">
              <Icon name="info" size={14} className="mt-0.5 flex-none" />
              {t('ee.signIn.sso.adminByName', { claim: row.claim.trim(), group: row.group.trim() })}
            </p>
          ))}
          {edits && (
            <button type="button" className="btn btn-sm mt-2" disabled={busy} onClick={() => set('rows', [...draft.rows, { claim: '', group: '' }])}>
              <Icon name="plus" size={12} />
              {t('ee.signIn.sso.addGroup')}
            </button>
          )}
        </div>
        <div>
          <p className="label">{t('ee.signIn.sso.secret')}</p>
          <p className="text-ui text-ink-2">
            {view.secret ? t('ee.signIn.sso.secretSaved') : view.secret_issuer_changed ? t('ee.signIn.sso.secretOtherIssuer') : t('ee.signIn.sso.secretMissing')}
          </p>
          {edits && (
            <div className="mt-2 flex flex-wrap gap-2">
              <input type="password" className="field" autoComplete="off" aria-label={t('ee.signIn.sso.secretNew')} placeholder={t('ee.signIn.sso.secretNew')} value={secret} onChange={(event) => setSecret(event.target.value)} />
              <button type="button" className="btn" disabled={busy || !secret.trim()} onClick={() => void run(() => peopleApi.saveSecret(secret.trim()), t('ee.signIn.sso.secretSavedDone')).then((ok) => ok && setSecret(''))}>
                {t('ee.signIn.sso.saveSecret')}
              </button>
              {view.secret && (
                <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => void run(() => peopleApi.removeSecret(), t('ee.signIn.sso.secretRemoved'))}>
                  {t('ee.signIn.sso.removeSecret')}
                </button>
              )}
            </div>
          )}
        </div>
        {edits && problems.length > 0 && (
          <ul className="list-disc pl-5 text-ui text-crit-ink">
            {problems.map((problem) => (
              <li key={problem}>{t(PROBLEM[problem])}</li>
            ))}
          </ul>
        )}
        {edits && (
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn btn-primary" disabled={busy || problems.length > 0} onClick={() => void save(fromDraft(draft, view.sso), t('ee.signIn.sso.saved'))}>
              {t('ee.signIn.sso.save')}
            </button>
            <button type="button" className="btn" disabled={busy || problems.length > 0} onClick={() => void runTest()}>
              {t('ee.signIn.sso.test')}
            </button>
            {view.sso && view.company_changes !== 'sso' && (
              <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => setTurningOff(true)}>
                {t('ee.signIn.sso.turnOff')}
              </button>
            )}
          </div>
        )}
        {edits && view.sso && view.company_changes === 'sso' && <p className="text-meta text-ink-3">{t('ee.signIn.sso.turnOffCommand')}</p>}
        {test && (
          <p role="status" className={`text-ui ${test.ok ? 'text-good-ink' : 'text-crit-ink'}`}>
            {test.ok ? t('ee.signIn.sso.testOk', { issuer: test.issuer, count: test.keys }) : t('ee.signIn.sso.testFailed', { error: test.error })}
          </p>
        )}
      </div>
      <ConfirmDialog
        open={turningOff}
        title={t('ee.signIn.sso.turnOffTitle')}
        confirmLabel={t('ee.signIn.sso.turnOff')}
        busy={busy}
        onCancel={() => setTurningOff(false)}
        onConfirm={() => void save(null, t('ee.signIn.sso.turnedOff')).then(() => setTurningOff(false))}
      >
        {t('ee.signIn.sso.turnOffBody')}
      </ConfirmDialog>
    </Section>
  )
}

/** Who made a SCIM token, in words: a person (and whether they still administer), or the server's command line. */
function maker(t: (key: string, values?: Record<string, unknown>) => string, token: ScimTokenInfo): { text: string; left: boolean } | null {
  const who = token.maker
  if (!who) return null
  if (who.kind === 'administration') return { text: t('ee.signIn.scim.madeByAdministration'), left: false }
  const name = who.name ?? who.id
  const left = !who.admin || who.disabled
  return { text: t('ee.signIn.scim.madeBy', { name }), left }
}

function Provisioning({ view, edits, reload }: { view: SignInView; edits: boolean; reload: () => void }) {
  const { t } = useTranslation()
  const fmt = useFormat()
  const [busy, run, company] = useRun(reload)
  const { show, panels } = usePanels() // a token shown stays until its own Close, another made meanwhile is shown beside it
  const [removing, setRemoving] = useState<string | null>(null)
  const tokens = view.scim.tokens
  return (
    <Section title={t('ee.signIn.scim.title')} description={t('ee.signIn.scim.description')}>
      <div className="space-y-3">
        {company && <CompanySignInWay />}
        <div>
          <p className="label">{t('ee.signIn.scim.url')}</p>
          <p className="flex items-center gap-2">
            <code className="mono text-ui text-ink">{view.scim.url}</code>
            <CopyButton text={view.scim.url} compact />
          </p>
        </div>
        <p className="text-meta text-warn-ink">{t('ee.groups.scimRole')}</p>
        {tokens.length === 0 ? (
          <p className="text-ui text-ink-2">{t('ee.signIn.scim.noToken')}</p>
        ) : (
          <ul className="space-y-1.5">
            {tokens.map((token) => {
              const made = maker(t, token)
              return (
                <li key={token.id} className="text-ui text-ink-2">
                  <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="mono text-ink">{token.id}</span>
                    <span>{t('ee.signIn.scim.tokenMade', { date: fmt.date(token.created) })}</span>
                    {made && <span>{made.text}</span>}
                    <span>{token.expires ? t('ee.signIn.scim.tokenEnds', { date: fmt.date(token.expires) }) : t('ee.signIn.scim.tokenNeverEnds')}</span>
                    <span>{token.last_used ? t('ee.signIn.scim.tokenUsed', { when: fmt.relative(token.last_used) }) : t('ee.signIn.scim.tokenUnused')}</span>
                    {edits && (
                      <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setRemoving(token.id)}>
                        {t('ee.signIn.scim.remove')}
                      </button>
                    )}
                  </span>
                  {made?.left && <span className="block text-meta text-warn-ink">{t('ee.signIn.scim.makerLeft')}</span>}
                </li>
              )
            })}
          </ul>
        )}
        {panels}
        {edits && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const made = await peopleApi.makeScimToken()
                show({ title: t('ee.signIn.scim.newToken'), lines: [{ label: t('ee.shown.token'), text: made.token }] })
              })
            }
          >
            {t('ee.signIn.scim.make')}
          </button>
        )}
      </div>
      <ConfirmDialog
        open={removing !== null}
        title={t('ee.signIn.scim.removeTitle', { id: removing ?? '' })}
        confirmLabel={t('ee.signIn.scim.remove')}
        busy={busy}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const id = removing
          if (id) void run(() => peopleApi.removeScimToken(id), t('ee.signIn.scim.removed')).then(() => setRemoving(null))
        }}
      >
        {t('ee.signIn.scim.removeBody')}
      </ConfirmDialog>
    </Section>
  )
}

/**
 * Whether the dashboard's changes that apply to everyone take a session the company sign-in opened: the setting, read
 * only (a server's administrators change it with a command on the server), what it means for the sessions `latet
 * dashboard` opens, and the way to a session that can make them.
 */
function CompanyChanges({ view }: { view: SignInView }) {
  const { t } = useTranslation()
  const { me } = useMe()
  const mode = view.company_changes ?? 'none'
  const code = me?.session?.via === 'code'
  return (
    <Section title={t('ee.signIn.company.title')} description={t('ee.signIn.company.description')}>
      <div className="space-y-3">
        <p className="text-ui text-ink-2">{t(COMPANY_CHANGES[mode])}</p>
        {mode === 'sso' && code && <CompanySignInWay quiet text={t('ee.signIn.company.thisSession')} />}
        <p className="text-ui text-ink-2">{t('ee.signIn.company.command')}</p>
        <CommandLine command={CMD_SERVER_COMPANY_CHANGES} />
        <p className="text-meta text-ink-3">{t('ee.signIn.company.issuer')}</p>
        <CommandLine command={CMD_SERVER_SIGN_IN_SET} />
      </div>
    </Section>
  )
}
