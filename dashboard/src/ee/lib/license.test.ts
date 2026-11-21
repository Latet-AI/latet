import { describe, expect, it } from 'vitest'
import de from '../../locales/de.json'
import en from '../../locales/en.json'
import es from '../../locales/es.json'
import fr from '../../locales/fr.json'
import itLocale from '../../locales/it.json'
import { ApiError } from '../../api/client'
import type { LicenseSummary, Me } from '../../api/types'
import type { LicenseStanding } from '../types'
import {
  changeLicense,
  daysText,
  featureLabel,
  formatLicenseDay,
  installBody,
  licenseBanner,
  licenseBodySize,
  licenseChangeable,
  licenseErrorCode,
  licenseStops,
  MAX_LICENSE_BODY,
  noticeTexts,
  readLicenseFile,
  reasonKey,
  stateTextKey,
  stopsKeys,
  weighedOf,
  type LicenseCalls,
  type PendingLicense,
} from './license'

const day = (iso: string) => iso
const VALID: LicenseSummary = {
  state: 'valid',
  licensee: 'Acme Corp',
  users: 300,
  users_in_use: null,
  expires: '2027-10-31',
  days_left: 244,
  trial: false,
  notices: [],
  pauses_on: null,
}

function meWith(license: LicenseSummary | null, extra: Record<string, unknown> = {}, roles: string[] = []): Me {
  return {
    version: '1.0.0',
    profile: 'balanced',
    port: 5283,
    data_dir: '/data',
    recording: true,
    tutorial: null,
    server: null,
    edition: { name: 'enterprise', license, features: [], pages: license ? ['license'] : [], roles, actions: [] },
    ...extra,
  } as unknown as Me
}

describe('the notices of a license', () => {
  it('has none while it is valid and far from its end', () => {
    expect(noticeTexts(VALID, day)).toEqual([])
  })

  it('says when a paid license ends', () => {
    const expiring: LicenseSummary = { ...VALID, state: 'expiring', days_left: 12, notices: ['expiring'] }
    expect(noticeTexts(expiring, day)).toEqual([
      { key: 'ee.license.notices.expiring', values: { date: '2027-10-31', count: 12 }, tone: 'warn' },
    ])
  })

  it('warns a week before a trial ends, louder from the day before, then says that it ended', () => {
    const trial: LicenseSummary = { ...VALID, trial: true, expires: '2026-12-02', state: 'expiring' }
    expect(noticeTexts({ ...trial, days_left: 7, notices: ['trial-week'] }, day)).toEqual([
      { key: 'ee.license.notices.trialWeek', values: { date: '2026-12-02', count: 7 }, tone: 'warn' },
    ])
    expect(noticeTexts({ ...trial, days_left: 1, notices: ['trial-day'] }, day)).toEqual([
      { key: 'ee.license.notices.trialTomorrow', values: { date: '2026-12-02' }, tone: 'crit' },
    ])
    expect(noticeTexts({ ...trial, days_left: 0, notices: ['trial-day'] }, day)[0]?.key).toBe('ee.license.notices.trialToday')
    const ended: LicenseSummary = { ...trial, state: 'trial-ended', days_left: -1, notices: ['trial-ended'] }
    expect(noticeTexts(ended, day)).toEqual([{ key: 'ee.license.notices.trialEnded', values: { date: '2026-12-02' }, tone: 'crit' }])
  })

  it('says the day what produces something new pauses, after the end and over the users', () => {
    const grace: LicenseSummary = { ...VALID, state: 'grace', days_left: -5, pauses_on: '2027-12-01', notices: ['grace'] }
    expect(noticeTexts(grace, day)[0]).toEqual({
      key: 'ee.license.notices.grace',
      values: { date: '2027-10-31', pausesOn: '2027-12-01' },
      tone: 'warn',
    })
    const over: LicenseSummary = { ...VALID, state: 'grace', users_in_use: 312, pauses_on: '2027-03-31', notices: ['users-over'] }
    expect(noticeTexts(over, day)[0]).toEqual({
      key: 'ee.license.notices.usersOver',
      values: { inUse: 312, users: 300, pausesOn: '2027-03-31' },
      tone: 'warn',
    })
  })

  it('puts what is paused, or a file that cannot be used, first', () => {
    const paused: LicenseSummary = { ...VALID, state: 'paused', users_in_use: 312, notices: ['users-over', 'paused'] }
    expect(noticeTexts(paused, day).map((n) => [n.key, n.tone])).toEqual([['ee.license.notices.usersOverPaused', 'crit']])
    const ended: LicenseSummary = { ...VALID, state: 'paused', days_left: -42, users_in_use: 280, notices: ['users-near', 'paused'] }
    expect(noticeTexts(ended, day).map((n) => n.key)).toEqual(['ee.license.notices.expiredPaused', 'ee.license.notices.usersNear'])
    const invalid: LicenseSummary = { ...VALID, state: 'invalid', licensee: null, users: null, expires: null, days_left: null, notices: ['invalid'] }
    expect(noticeTexts(invalid, day)).toEqual([{ key: 'ee.license.notices.invalid', values: {}, tone: 'crit' }])
  })

  it('says nothing of the users to someone the server does not tell how many there are', () => {
    // On a server a person gets the notices but not the count (the administrators'): a sentence with no number is not said.
    const told: LicenseSummary = { ...VALID, state: 'grace', users_in_use: null, pauses_on: null, notices: ['users-over'] }
    expect(noticeTexts(told, day)).toEqual([])
    expect(noticeTexts({ ...told, notices: ['users-near'] }, day)).toEqual([])
  })
})

describe('the notice bar', () => {
  // A server's administrators are told the users' count; a person on it is not, and sees no bar.
  const expiring: LicenseSummary = { ...VALID, state: 'expiring', days_left: 12, notices: ['expiring'] }
  const over: LicenseSummary = { ...VALID, state: 'grace', users_in_use: 312, pauses_on: '2027-03-31', notices: ['users-over'] }
  const server = { url: 'https://latet.acme.lan', identity: { id: 'u_7f3a2b9c1d', name: 'Emily Carter', kind: 'person' } }

  it('shows the notice bar to the servers administrators alone', () => {
    expect(licenseBanner(meWith(expiring, { server }), day)).toBeNull()
    expect(licenseBanner(meWith(expiring, { server }, ['admin']), day)?.key).toBe('ee.license.notices.expiring')
  })

  it('shows a server administrator the most serious notice, with the users counted', () => {
    expect(licenseBanner(meWith(over, { server }), day)).toBeNull()
    expect(licenseBanner(meWith(over, { server }, ['admin']), day)?.key).toBe('ee.license.notices.usersOver')
  })

  it('shows nothing without a license, or without a notice', () => {
    expect(licenseBanner(meWith(null), day)).toBeNull()
    expect(licenseBanner(meWith(VALID), day)).toBeNull()
    expect(licenseBanner(undefined, day)).toBeNull()
  })
})

describe('what the License page says', () => {
  it('tells a grace with no pause day (a person on a server) from one with a day', () => {
    expect(stateTextKey({ state: 'grace', pauses_on: '2027-03-31' })).toBe('ee.license.stateText.grace')
    expect(stateTextKey({ state: 'grace', pauses_on: null })).toBe('ee.license.stateText.graceUndated')
    expect(stateTextKey({ state: 'trial-ended', pauses_on: null })).toBe('ee.license.stateText.trialEnded')
  })

  it('says a file that cannot be used as the server keeps it', () => {
    expect(stateTextKey({ state: 'invalid', pauses_on: null, source: 'server', reason_code: 'signature' })).toBe('ee.license.stateText.invalidServer')
    expect(stateTextKey({ state: 'invalid', pauses_on: null, source: 'server', reason_code: 'unreadable' })).toBe('ee.license.stateText.invalidServerDown')
    expect(stateTextKey({ state: 'invalid', pauses_on: null })).toBe('ee.license.stateText.invalidServer')
  })

  it('weighs the standing of the page as the notices need it', () => {
    const standing: LicenseStanding = {
      state: 'grace',
      license: { id: 'L-1', licensee: 'Acme Corp', users: 300, features: [], issued: '2026-03-01', expires: '2027-02-28', trial: true },
      users_in_use: 312,
      days_left: -3,
      over_since: null,
      pauses_on: '2027-03-31',
      reason: null,
      reason_code: null,
      notices: ['users-over'],
      source: 'server',
    }
    expect(weighedOf(standing)).toEqual({
      expires: '2027-02-28',
      users: 300,
      trial: true,
      days_left: -3,
      users_in_use: 312,
      notices: ['users-over'],
      pauses_on: '2027-03-31',
    })
    expect(weighedOf({ ...standing, license: null, users_in_use: null }).users).toBeNull()
  })

  it('names a feature, and leaves one it does not know as it is', () => {
    const t = (key: string) => `<${key}>`
    expect(featureLabel(t, 'locked-rules')).toBe('<ee.license.features.lockedRules>')
    expect(featureLabel(t, 'from-a-newer-latet')).toBe('from-a-newer-latet')
  })

  it('turns a reason code into its sentence, and a code it does not know into "not a license"', () => {
    expect(reasonKey('signature')).toBe('ee.license.reasons.signature')
    expect(reasonKey('from-a-newer-latet')).toBe('ee.license.reasons.format')
    expect(reasonKey(null)).toBe('ee.license.reasons.format')
  })

  it('counts the days to the end, and since it', () => {
    expect(daysText(12)).toEqual({ key: 'ee.license.daysLeft', count: 12 })
    expect(daysText(0)).toEqual({ key: 'ee.license.endsToday', count: 0 })
    expect(daysText(-3)).toEqual({ key: 'ee.license.endedAgo', count: 3 })
  })

  it('writes a license day with its year, the same in every timezone', () => {
    expect(formatLicenseDay('en', '2027-10-31')).toBe('October 31, 2027')
    expect(formatLicenseDay('de', '2027-10-31')).toBe('31. Oktober 2027')
  })
})

describe('a license file that cannot be used', () => {
  // M1: what manages, produces or decides is off, never what protects; the CLI says the same (describe()).
  const locales = { en, it: itLocale, de, fr, es }
  const off: Record<keyof typeof locales, RegExp> = {
    en: /features are off/i,
    it: /funzionalità enterprise sono disattivate/i,
    de: /enterprise-funktionen ausgeschaltet/i,
    fr: /fonctionnalités enterprise sont désactivées/i,
    es: /funciones enterprise están desactivadas/i,
  }
  const protects: Record<keyof typeof locales, RegExp> = {
    en: /what protects/i,
    it: /ciò che protegge/i,
    de: /was schützt/i,
    fr: /ce qui protège/i,
    es: /lo que protege/i,
  }
  const restarted: Record<keyof typeof locales, RegExp> = {
    en: /restarted or not/i,
    it: /anche dopo un riavvio/i,
    de: /auch nach einem Neustart/i,
    fr: /même après un redémarrage/i,
    es: /incluso después de reiniciar/i,
  }

  for (const [lang, strings] of Object.entries(locales) as [keyof typeof locales, typeof en][]) {
    it(`never says the Enterprise features are off, and says what happens to what protects (${lang})`, () => {
      const said = strings.ee.license.stateText
      const texts = [
        said.invalidServer,
        said.invalidServerDown,
        strings.ee.license.notices.invalid,
      ]
      for (const text of texts) {
        expect(text).not.toMatch(off[lang])
        expect(text).toMatch(protects[lang])
      }
      // A server's restart is not what keeps it: the server says its instances; the notice bar, which knows no
      // source, points to the License page.
      expect(said.invalidServer).not.toMatch(restarted[lang])
    })
  }
})

describe('the texts of a trial that ended', () => {
  // What protects keeps working, and the page's own text says the locked rules stay in force: in every language.
  const locales = { en, it: itLocale, de, fr, es }
  const protects: Record<keyof typeof locales, RegExp> = {
    en: /what protects keeps working/i,
    it: /ciò che protegge continua a funzionare/i,
    de: /was schützt, arbeitet weiter/i,
    fr: /ce qui protège continue de fonctionner/i,
    es: /lo que protege sigue funcionando/i,
  }
  const locked: Record<keyof typeof locales, RegExp> = {
    en: /locked rules stay in force/i,
    it: /regole bloccate restano in vigore/i,
    de: /gesperrte regeln bleiben in kraft/i,
    fr: /règles verrouillées restent en vigueur/i,
    es: /reglas bloqueadas siguen en vigor/i,
  }

  for (const [lang, strings] of Object.entries(locales) as [keyof typeof locales, typeof en][]) {
    it(`says what protects keeps working, and the locked rules stay (${lang})`, () => {
      expect(strings.ee.license.notices.trialEnded).toMatch(protects[lang])
      expect(strings.ee.trialEnded.body).toMatch(locked[lang])
    })
  }
})

describe('licenseStops', () => {
  const stops = (status: number, body: unknown) => new ApiError(status, 'not changed: the policy stops', [], body)

  it('knows a change of the license refused because the policy stops being in force with it', () => {
    expect(licenseStops(stops(409, { error: 'not changed', policy: 'stops' }))).toEqual({ policy: true, terms: false, approvals: false })
  })

  it('knows no other refusal as that one', () => {
    expect(licenseStops(stops(409, { error: 'not changed' }))).toBeNull()
    expect(licenseStops(stops(400, { error: 'not a license', policy: 'stops' }))).toBeNull()
    expect(licenseStops(stops(409, { error: 'not changed', policy: 'other' }))).toBeNull()
    expect(licenseStops(new ApiError(409, 'not changed'))).toBeNull()
    expect(licenseStops(new Error('stops'))).toBeNull()
    expect(licenseStops(stops(400, { error: 'not a license', terms: 'stop' }))).toBeNull()
    expect(licenseStops(stops(409, { error: 'not changed', terms: 'stops' }))).toBeNull()
  })

  // E6 (R29): a license without term sync while synced terms are kept stops them being masked; the server's 409 says
  // `terms: "stop"`, alone or beside `policy: "stops"`, and the same confirmation covers what stops (`changeLicense`
  // waits for the person's word whenever `licenseStops` names something).
  it('knows a change that stops the synced terms being masked, alone or with the policy, and asks the same word', () => {
    const terms = stops(409, { error: 'not changed', terms: 'stop' })
    const both = stops(409, { error: 'not changed', policy: 'stops', terms: 'stop' })
    expect(licenseStops(terms)).toEqual({ policy: false, terms: true, approvals: false })
    expect(licenseStops(both)).toEqual({ policy: true, terms: true, approvals: false })
    expect(licenseStops(stops(409, { error: 'not changed', policy: 'stops' }))).toEqual({ policy: true, terms: false, approvals: false })
    expect(licenseStops(stops(409, { error: 'not changed' }))).toBeNull()
  })

  it('names in the question what stops, never the policy when only the synced terms stop', () => {
    expect(stopsKeys({ policy: true, terms: false, approvals: false })).toEqual({
      title: 'ee.license.change.stopsTitle',
      body: 'ee.license.change.stopsBody',
    })
    expect(stopsKeys({ policy: false, terms: true, approvals: false })).toEqual({
      title: 'ee.license.change.termsStopTitle',
      body: 'ee.license.change.termsStopBody',
    })
    expect(stopsKeys({ policy: true, terms: true, approvals: false })).toEqual({
      title: 'ee.license.change.bothStopTitle',
      body: 'ee.license.change.bothStopBody',
    })
  })

  // The final security review's L1: a license without approvals while the security team decides on changes or texts
  // sends each back to whoever asks; the server's 409 says `approvals: "stop"`, alone or beside the others, and the
  // same confirmation covers it.
  it('knows a change that stops central approvals, alone or with the others, and asks the same word', () => {
    const approvals = stops(409, { error: 'not changed', approvals: 'stop' })
    const all = stops(409, { error: 'not changed', policy: 'stops', terms: 'stop', approvals: 'stop' })
    expect(licenseStops(approvals)).toEqual({ policy: false, terms: false, approvals: true })
    expect(licenseStops(all)).toEqual({ policy: true, terms: true, approvals: true })
    expect(licenseStops(stops(409, { error: 'not changed', approvals: 'stops' }))).toBeNull()
    expect(licenseStops(stops(400, { error: 'not a license', approvals: 'stop' }))).toBeNull()
  })

  it('names central approvals in the question: alone in their own words, beside the others in a paragraph of their own', () => {
    expect(stopsKeys({ policy: false, terms: false, approvals: true })).toEqual({
      title: 'ee.license.change.approvalsStopTitle',
      body: 'ee.license.change.approvalsStopBody',
    })
    const also = 'ee.license.change.approvalsStopAlso'
    expect(stopsKeys({ policy: true, terms: false, approvals: true })).toEqual({
      title: 'ee.license.change.stopsTitle',
      body: 'ee.license.change.stopsBody',
      also,
    })
    expect(stopsKeys({ policy: false, terms: true, approvals: true })).toMatchObject({ title: 'ee.license.change.termsStopTitle', also })
    expect(stopsKeys({ policy: true, terms: true, approvals: true })).toMatchObject({ title: 'ee.license.change.bothStopTitle', also })
    expect(stopsKeys({ policy: true, terms: false, approvals: false })).not.toHaveProperty('also')
  })

  const languages = { en, it: itLocale, de, fr, es }
  for (const [lang, strings] of Object.entries(languages)) {
    it(`asks in ${lang} about the synced terms and central approvals in its own words`, () => {
      const change = strings.ee.license.change as Record<string, string>
      for (const key of ['termsStopTitle', 'termsStopBody', 'bothStopTitle', 'bothStopBody', 'approvalsStopTitle', 'approvalsStopBody', 'approvalsStopAlso']) {
        expect(change[key], key).toBeTruthy()
        if (lang !== 'en') expect(change[key], key).not.toBe((en.ee.license.change as Record<string, string>)[key])
      }
    })
  }
})

describe('the license changed from the page', () => {
  const manager = ['license.manage', 'audit.read']

  it('is offered to whoever manages the license, while the license can manage', () => {
    for (const state of ['valid', 'expiring', 'grace'] as const) expect(licenseChangeable(state, manager), state).toBe(true)
    // The server refuses any other standing: the license is installed from its command line then.
    for (const state of ['paused', 'trial-ended', 'invalid', 'none'] as const) expect(licenseChangeable(state, manager), state).toBe(false)
  })

  it('is offered to nobody who does not manage it', () => {
    expect(licenseChangeable('valid', ['people.manage', 'sign-in.edit'])).toBe(false)
    expect(licenseChangeable('valid', [])).toBe(false)
  })

  it('knows why the server took a file for no license, by its code', () => {
    const refused = (status: number, body: unknown) => new ApiError(status, 'the file is no license', [], body)
    expect(licenseErrorCode(refused(400, { error: 'x', code: 'signature' }))).toBe('signature')
    expect(licenseErrorCode(refused(400, { error: 'x', code: 'no-keys' }))).toBe('no-keys')
    expect(licenseErrorCode(refused(400, { error: 'x', code: 'a-code-from-a-newer-server' }))).toBeNull()
    expect(licenseErrorCode(refused(400, { error: 'x', code: 'toString' }))).toBeNull()
    expect(licenseErrorCode(refused(403, { error: 'x', code: 'signature' }))).toBeNull()
    expect(licenseErrorCode(new ApiError(400, 'x'))).toBeNull()
    expect(licenseErrorCode(new Error('x'))).toBeNull()
  })
})

// A change of the license from the page: what the page does with each answer, without a page.
describe('a license installed or removed from the page', () => {
  const stops = () => new ApiError(409, 'not changed: the policy stops', [], { error: 'not changed', policy: 'stops' })
  const COMPANY = 'sign in with your company account to change what applies to everyone'
  const company = () => new ApiError(403, COMPANY, [], { error: COMPANY, code: 'company-sign-in' })
  const install: PendingLicense = { kind: 'install', text: 'LICENSE FILE' }
  const remove: PendingLicense = { kind: 'remove' }
  const POLICY = { policy: true, terms: false, approvals: false }

  /** The calls the page would make: answered in turn by `answers` (an error is thrown, anything else is the answer). */
  function server(...answers: unknown[]): { api: LicenseCalls; log: string[] } {
    const log: string[] = []
    const next = async () => {
      const answer = answers.shift()
      if (answer instanceof Error) throw answer
      return answer
    }
    const api: LicenseCalls = {
      installLicense: async (text, options) => {
        log.push(`install ${text} seen=${options?.policyStops === true}`)
        return next()
      },
      removeLicense: async (options) => {
        log.push(`remove seen=${options?.policyStops === true}`)
        return next()
      },
    }
    return { api, log }
  }

  it('is made, and says which change it was', async () => {
    const { api, log } = server({ state: 'valid' }, { state: 'none', removed: true })
    expect(await changeLicense(install, false, api)).toEqual({ kind: 'done', pending: install })
    expect(await changeLicense(remove, false, api)).toEqual({ kind: 'done', pending: remove })
    expect(log).toEqual(['install LICENSE FILE seen=false', 'remove seen=false'])
  })

  it("asks before a change that stops the server's policy, and makes the same change again once the person said they saw it", async () => {
    const { api, log } = server(stops(), { state: 'valid' })
    const asked = await changeLicense(install, false, api)
    expect(asked).toEqual({ kind: 'stops', pending: install, stopping: POLICY }) // nothing changed; the page asks, holding the same file
    if (asked.kind !== 'stops') throw new Error('the change did not ask')
    expect(await changeLicense(asked.pending, true, api)).toEqual({ kind: 'done', pending: install })
    // The first call never says it was seen; the second, with the file the person chose, does.
    expect(log).toEqual(['install LICENSE FILE seen=false', 'install LICENSE FILE seen=true'])
  })

  it('asks the same about a removal, and removes with the word that it was seen', async () => {
    const { api, log } = server(stops(), { state: 'none', removed: true })
    const asked = await changeLicense(remove, false, api)
    expect(asked).toEqual({ kind: 'stops', pending: remove, stopping: POLICY })
    if (asked.kind !== 'stops') throw new Error('the change did not ask')
    expect(await changeLicense(asked.pending, true, api)).toEqual({ kind: 'done', pending: remove })
    expect(log).toEqual(['remove seen=false', 'remove seen=true'])
  })

  it('asks again if the policy stops after it was seen, and never makes a third call by itself', async () => {
    const { api, log } = server(stops(), stops())
    const first = await changeLicense(remove, false, api)
    if (first.kind !== 'stops') throw new Error('the change did not ask')
    expect(await changeLicense(first.pending, true, api)).toEqual({ kind: 'stops', pending: remove, stopping: POLICY })
    expect(log).toEqual(['remove seen=false', 'remove seen=true'])
  })

  it('asks before a change that stops only the synced terms, says so, and makes it once the person said they saw it', async () => {
    const terms = new ApiError(409, 'not changed: the synced terms stop', [], { error: 'not changed', terms: 'stop' })
    const { api, log } = server(terms, { state: 'valid' })
    const asked = await changeLicense(install, false, api)
    expect(asked).toEqual({ kind: 'stops', pending: install, stopping: { policy: false, terms: true, approvals: false } })
    if (asked.kind !== 'stops') throw new Error('the change did not ask')
    expect(await changeLicense(asked.pending, true, api)).toEqual({ kind: 'done', pending: install })
    expect(log).toEqual(['install LICENSE FILE seen=false', 'install LICENSE FILE seen=true'])
  })

  it('leads to the company sign-in when the session was not opened with it, with nothing else said', async () => {
    const { api } = server(company())
    expect(await changeLicense(install, false, api)).toEqual({ kind: 'company' })
  })

  it('says why a file is no license by its code, and any other refusal as the server says it', async () => {
    const signature = new ApiError(400, 'its signature does not match', [], { error: 'its signature does not match', code: 'signature' })
    const { api } = server(signature, new ApiError(403, 'trial ended', [], { error: 'trial ended', code: 'trial-ended' }), new Error('offline'))
    expect(await changeLicense(install, false, api)).toEqual({ kind: 'refused', code: 'signature', error: signature })
    expect(await changeLicense(remove, false, api)).toMatchObject({ kind: 'refused', code: null })
    expect(await changeLicense(install, false, api)).toMatchObject({ kind: 'refused', code: null })
  })

  it('sends the license in the body, and says the policy stopping was seen only when it was', () => {
    expect(installBody('LICENSE FILE')).toEqual({ license: 'LICENSE FILE' })
    expect(installBody('LICENSE FILE', true)).toEqual({ license: 'LICENSE FILE', policy_stops: true })
  })
})

// The server reads at most 256 KiB of a request's body (ee/api.py MAX_BODY) and counts the JSON the page sends: the
// file's text in quotes, with each quote, backslash and control character escaped, and the word that the policy stopping
// was seen on a second call.
describe('the size of a license file the page sends', () => {
  const file = (size: number, text: () => Promise<string>) => ({ size, text })
  const OVERHEAD = licenseBodySize('') // the body around the text

  it("is the JSON's bytes, not the file's: the same limit as the server's", () => {
    expect(MAX_LICENSE_BODY).toBe(256 * 1024)
    expect(licenseBodySize('abc')).toBe(OVERHEAD + 3)
    expect(licenseBodySize('"')).toBe(OVERHEAD + 2) // a quote is written with a backslash before it
    expect(licenseBodySize('\\')).toBe(OVERHEAD + 2) // so is a backslash
    expect(licenseBodySize('\n')).toBe(OVERHEAD + 2) // and a newline is two characters
    expect(licenseBodySize(String.fromCharCode(1))).toBe(OVERHEAD + 6) // a control character is six
    expect(licenseBodySize(String.fromCodePoint(0xe9))).toBe(OVERHEAD + 2) // UTF-8 bytes, as the server counts them
    expect(licenseBodySize(String.fromCodePoint(0x20ac))).toBe(OVERHEAD + 3)
  })

  it('counts the word that the policy stopping was seen, so that a file that fits the first call fits the second', () => {
    expect(OVERHEAD).toBe(new TextEncoder().encode(JSON.stringify(installBody('', true))).length)
    expect(OVERHEAD).toBeGreaterThan(new TextEncoder().encode(JSON.stringify(installBody(''))).length)
  })

  it('reads a file whose body is at the limit, and refuses one a byte over', async () => {
    const room = MAX_LICENSE_BODY - OVERHEAD
    const atLimit = 'a'.repeat(room)
    expect(await readLicenseFile(file(room, async () => atLimit))).toEqual({ text: atLimit })
    expect(await readLicenseFile(file(room + 1, async () => atLimit + 'a'))).toEqual({ refused: 'tooLarge' })
  })

  it('refuses a file smaller than the limit whose JSON is larger: quotes and newlines are written with a backslash', async () => {
    const quoted = '"'.repeat(MAX_LICENSE_BODY / 2) // a file of 128 KiB: its body is over 256 KiB
    expect(await readLicenseFile(file(quoted.length, async () => quoted))).toEqual({ refused: 'tooLarge' })
    const lines = '{\n  "id": 1\n}\n'.repeat(16_000) // a pretty-printed file: each newline and quote costs a byte more
    expect(lines.length).toBeLessThan(MAX_LICENSE_BODY)
    expect(licenseBodySize(lines)).toBeGreaterThan(MAX_LICENSE_BODY)
    expect(await readLicenseFile(file(lines.length, async () => lines))).toEqual({ refused: 'tooLarge' })
  })

  it('does not read a file larger than the limit at all', async () => {
    let read = false
    const huge = file(MAX_LICENSE_BODY + 1, async () => {
      read = true
      return ''
    })
    expect(await readLicenseFile(huge)).toEqual({ refused: 'tooLarge' })
    expect(read).toBe(false)
  })

  it('says a file that cannot be read, and a text bigger than its size said', async () => {
    const broken = file(10, async () => Promise.reject(new DOMException('not readable', 'NotReadableError')))
    expect(await readLicenseFile(broken)).toEqual({ refused: 'unreadable' })
    expect(await readLicenseFile(file(10, async () => 'a'.repeat(MAX_LICENSE_BODY)))).toEqual({ refused: 'tooLarge' })
  })
})
