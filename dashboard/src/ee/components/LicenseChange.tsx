import { useRef, useState, type ChangeEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { CommandLine, ConfirmDialog, errorText } from '../../components/ui'
import { CMD_SERVER_LICENSE_INSTALL } from '../../lib/commands'
import { useMe } from '../../lib/me'
import { eeApi } from '../api'
import {
  changeLicense,
  licenseChangeable,
  readLicenseFile,
  reasonKey,
  stopsKeys,
  type PendingLicense,
  type Stopping,
} from '../lib/license'
import type { LicenseStanding } from '../types'
import { CompanySignInWay } from './CompanyWay'

const REMOVE: PendingLicense = { kind: 'remove' }

/**
 * Under the License page's standing: for whoever manages the license, while it can manage (`licenseChangeable`), the
 * license file to install in its place, and the license's removal; else, as before, the command that does it from the
 * server's command line. A change that stops the server's policy, the synced terms being masked, or central approvals,
 * asks first, naming what stops (the server answers 409 `policy: "stops"`, `terms: "stop"`, `approvals: "stop"` or
 * several, and nothing changes until it is said again with the person's word: `changeLicense`, `stopsKeys`). The file
 * is read here and sent as text, if the JSON that carries it fits what the server reads (`readLicenseFile`); the
 * server judges it, and says why it is none in a code (`reasonKey` says it in the reader's language).
 */
export function LicenseChange({ standing, reload }: { standing: LicenseStanding; reload: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { me, reload: reloadMe } = useMe()
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [company, setCompany] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [stops, setStops] = useState<{ pending: PendingLicense; stopping: Stopping } | null>(null)
  const actions = me?.edition?.actions ?? []
  const manager = actions.includes('license.manage')

  if (!manager) {
    return (
      <div className="mt-5 max-w-[72ch] space-y-2 text-ui text-ink-2">
        <p>{t('ee.license.elsewhere.server')}</p>
        <CommandLine command={CMD_SERVER_LICENSE_INSTALL} />
      </div>
    )
  }
  if (!licenseChangeable(standing.state, actions)) {
    return (
      <div className="mt-5 max-w-[72ch] space-y-2 text-ui text-ink-2">
        <p>{t('ee.license.change.commandOnly')}</p>
        <CommandLine command={CMD_SERVER_LICENSE_INSTALL} />
      </div>
    )
  }

  const change = async (pending: PendingLicense, seen: boolean) => {
    setBusy(true)
    setCompany(false)
    const came = await changeLicense(pending, seen, eeApi)
    setRemoving(false)
    switch (came.kind) {
      case 'done':
        toast.show(came.pending.kind === 'install' ? t('ee.license.change.installed') : t('ee.license.change.removed'), 'ok')
        setStops(null)
        reload()
        reloadMe() // /me tells the license and the pages it opens
        break
      case 'stops': // nothing changed: the dialog asks, and the same change is made again once the person confirms
        setStops({ pending: came.pending, stopping: came.stopping })
        break
      case 'company': // the banner below says it, with the way to the company sign-in: no toast repeats it
        setStops(null)
        setCompany(true)
        break
      case 'refused':
        setStops(null)
        toast.show(came.code ? t('ee.license.change.notLicense', { reason: t(reasonKey(came.code)) }) : errorText(t, came.error), 'fail')
        break
    }
    setBusy(false)
  }

  const choose = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = '' // so that the same file can be chosen again
    if (!file) return
    const read = await readLicenseFile(file)
    if ('refused' in read) {
      toast.show(read.refused === 'tooLarge' ? t('ee.license.change.tooLarge') : t('ee.license.change.unreadable'), 'fail')
      return
    }
    await change({ kind: 'install', text: read.text }, false)
  }

  // The question names what stops.
  const asked = stopsKeys(stops?.stopping ?? { policy: true, terms: false, approvals: false })

  return (
    <section className="panel mt-5 space-y-3 p-5" aria-labelledby="license-change">
      <h2 id="license-change" className="text-title font-semibold text-ink">
        {t('ee.license.change.title')}
      </h2>
      <p className="max-w-[72ch] text-ui text-ink-2">{t('ee.license.change.description')}</p>
      <p className="max-w-[72ch] text-ui text-ink-2">{t('ee.license.change.rolesNote')}</p>
      {company && <CompanySignInWay />}
      <div className="flex flex-wrap gap-2">
        <input ref={input} type="file" accept=".json,application/json" hidden onChange={(event) => void choose(event)} />
        <button type="button" className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}>
          {t('ee.license.change.install')}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => setRemoving(true)}>
          {t('ee.license.change.remove')}
        </button>
      </div>
      <ConfirmDialog
        open={removing}
        title={t('ee.license.change.removeTitle')}
        confirmLabel={t('ee.license.change.remove')}
        busy={busy}
        onCancel={() => setRemoving(false)}
        onConfirm={() => void change(REMOVE, false)}
      >
        {t('ee.license.change.removeBody')}
      </ConfirmDialog>
      <ConfirmDialog
        open={stops !== null}
        title={t(asked.title)}
        confirmLabel={stops?.pending.kind === 'install' ? t('ee.license.change.stopsInstall') : t('ee.license.change.stopsRemove')}
        busy={busy}
        onCancel={() => setStops(null)}
        onConfirm={() => {
          if (stops) void change(stops.pending, true)
        }}
      >
        <p>{t(asked.body)}</p>
        {asked.also && <p className="mt-2">{t(asked.also)}</p>}
      </ConfirmDialog>
    </section>
  )
}
