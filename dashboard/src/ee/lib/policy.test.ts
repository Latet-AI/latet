import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import i18n from '../../i18n'
import type { PolicyFloor, PolicyText, PolicyView } from './policy'
import {
  canSave,
  changePolicy,
  editsPolicy,
  floorRefusal,
  imagesOf,
  lineText,
  lockLines,
  locksOf,
  policyChanged,
  policyRefusal,
  profileAllowed,
  protectionAllowed,
  removable,
  storedText,
} from './policy'

const floor = (rules: PolicyFloor['rules'], extra: Partial<PolicyFloor> = {}): PolicyFloor => ({
  source: 'managed',
  origin: 'C:\\ProgramData\\Latet\\policy.yaml',
  received: 1791191520,
  exceptions: 'personal',
  rules,
  notices: 0,
  ...extra,
})
const view = (...floors: PolicyFloor[]): PolicyView => ({ floors, below: [], problems: [] })

describe('locksOf', () => {
  it('is null without a policy', () => {
    expect(locksOf(null)).toBeNull()
    expect(locksOf(view())).toBeNull()
  })

  it('reads what the policy locks, and what its profile brings with it', () => {
    const locks = locksOf(
      view(floor({ latet_rules: 1, profile: 'max', terms: 2, dump_guard: 10, policies: { IBAN: 'redact' }, data_files: 3, data_hosts: 1 }, { source: 'server', exceptions: 'company' })),
    )!
    expect(locks.profile).toBe('max')
    expect(locks.terms).toBe(2)
    expect(locks.dumpGuard).toBe(10)
    expect(locks.policies).toEqual({ IBAN: 'redact' })
    expect(locks.companyExceptions).toBe(true)
    expect(locks.privateIps && locks.strictData).toBe(true) // what max brings with it
    expect(locks.images).toBe('block')
    expect([locks.dataFiles, locks.dataHosts]).toEqual([3, 1])
  })

  it('counts the company\'s exceptions, never reads them', () => {
    const locks = locksOf(view(floor({ latet_rules: 1, allow_values: 2, allow_patterns: 1 }, { exceptions: 'company' })))!
    expect(locks.exceptions).toBe(3)
  })
})

describe('field rules', () => {
  it('names the ones that mask and counts the ones that keep a field in clear', () => {
    const locks = locksOf(view(floor({ latet_rules: 1, fields: ['falcon_notes'], fields_clear: 2 }, { exceptions: 'company' })))!
    expect([locks.fields, locks.fieldsClear]).toEqual([['falcon_notes'], 2])
    expect(lockLines(locks).data).toEqual([
      { key: 'ee.policy.lock.fields', vars: { names: 'falcon_notes' } },
      { key: 'ee.policy.lock.fieldsClear', vars: { count: 2 } },
    ])
  })
})

describe('new request members', () => {
  it('a policy that says unknown_members: refuse locks it, said under the production data, in every language', async () => {
    const locks = locksOf(view(floor({ latet_rules: 1, unknown_members: 'refuse' })))!
    expect(locks.unknownMembers).toBe(true)
    expect(lockLines(locks).data).toEqual([{ key: 'ee.policy.lock.unknownMembers' }])
    try {
      for (const lang of ['en', 'it', 'de', 'fr', 'es']) {
        await i18n.changeLanguage(lang)
        expect(i18n.exists('ee.policy.lock.unknownMembers')).toBe(true)
        if (lang !== 'en') expect(i18n.t('ee.policy.lock.unknownMembers')).not.toBe(i18n.getFixedT('en')('ee.policy.lock.unknownMembers'))
      }
    } finally {
      await i18n.changeLanguage('en')
    }
  })

  it('one that says check, or nothing of it, locks nothing', () => {
    for (const rules of [{ latet_rules: 1, unknown_members: 'check' as const }, { latet_rules: 1, profile: 'max' as const }]) {
      const locks = locksOf(view(floor(rules)))!
      expect(locks.unknownMembers).toBe(false)
      expect((lockLines(locks).data ?? []).map((line) => line.key)).not.toContain('ee.policy.lock.unknownMembers')
    }
  })
})

describe('imagesOf', () => {
  it('reads images as Latet does: the setting, else block with max', () => {
    expect(imagesOf({ images: 'mask', profile: 'max' })).toBe('mask')
    expect(imagesOf({ profile: 'max' })).toBe('block')
    expect(imagesOf({ images: 'allow' })).toBeNull()
  })
})

describe('profileAllowed and protectionAllowed', () => {
  it('offer the profiles at the floor or above, and never secrets when personal data is locked', () => {
    const balanced = locksOf(view(floor({ latet_rules: 1, profile: 'balanced' })))
    expect(profileAllowed(balanced, 'secrets')).toBe(false)
    expect(profileAllowed(balanced, 'max')).toBe(true)
    expect(profileAllowed(null, 'secrets')).toBe(true)
    expect(profileAllowed(locksOf(view(floor({ latet_rules: 1, pii: ['IBAN'] }))), 'secrets')).toBe(false)
  })

  it('keep Latet on in a project', () => {
    const termsOnly = locksOf(view(floor({ latet_rules: 1, terms: 1 })))
    expect(protectionAllowed(termsOnly, 'off')).toBe(false)
    expect(protectionAllowed(termsOnly, 'secrets')).toBe(true)
    expect(protectionAllowed(locksOf(view(floor({ latet_rules: 1, profile: 'max' }))), 'secrets')).toBe(false)
    expect(protectionAllowed(null, 'off')).toBe(true)
  })
})

describe('lockLines', () => {
  it('puts each line under its section of Rules', () => {
    const lines = lockLines(locksOf(view(floor({ latet_rules: 1, profile: 'max', ner: 'auto', terms: 1, data_hosts: 2 })))!)
    expect(lines.profile).toEqual([{ key: 'ee.policy.lock.profile', vars: { profile: 'max' } }])
    expect(lines.ner).toEqual([{ key: 'ee.policy.lock.ner' }])
    expect(lines.terms).toEqual([{ key: 'ee.policy.lock.terms', vars: { count: 1 } }])
    expect(lines.allow).toEqual([{ key: 'ee.policy.lock.termsStay' }]) // personal exceptions: the terms stay masked
    expect(lines.data?.map((line) => line.key)).toEqual(['ee.policy.lock.data', 'ee.policy.lock.dataSources', 'ee.policy.lock.images'])
    expect(lines.data?.[1]?.vars).toEqual({ files: 0, hosts: 2 })
  })

  it('names no value of the company\'s: a term is a number, a recognizer a name', () => {
    const rules: PolicyFloor['rules'] = { latet_rules: 1, terms: 2, recognizers: ['contract-number'], allow_values: 1 }
    const lines = lockLines(locksOf(view(floor(rules, { exceptions: 'company' })))!)
    const text = JSON.stringify(lines)
    expect(lines.terms?.[0]?.vars).toEqual({ count: 2 })
    expect(lines.recognizers?.[0]?.vars).toEqual({ names: 'contract-number' })
    expect(lines.allow?.map((line) => line.vars)).toEqual([undefined, { count: 1 }])
    expect(text).not.toContain('Falcon')
  })
})

describe('lineText', () => {
  it('says the profile, the images rule and the policies by the names Rules gives them', async () => {
    await i18n.changeLanguage('en')
    const t = i18n.t.bind(i18n)
    expect(lineText(t, { key: 'ee.policy.lock.profile', vars: { profile: 'max' } })).toBe(`At least ${t('rules.profile.max.label')}`)
    expect(lineText(t, { key: 'ee.policy.lock.images', vars: { images: 'mask' } })).toContain(t('data.images.mask'))
    expect(lineText(t, { key: 'ee.policy.lock.policies', vars: { policies: 'IBAN redact, EMAIL mask' } })).toBe(
      `At least: IBAN (${t('rules.policies.redact')}), EMAIL (${t('rules.policies.mask')})`,
    )
    expect(lineText(t, { key: 'ee.policy.lock.terms', vars: { count: 1 } })).toBe('1 term of your company stays masked')
    expect(lineText(t, { key: 'ee.policy.lock.terms', vars: { count: 2 } })).toBe('2 terms of your company stay masked')
  })
})

describe('floorRefusal', () => {
  it('reads the settings a refused save names', () => {
    const body = { error: 'Set by your company, not changed: profile: from max to balanced', floor: ['profile: from max to balanced'] }
    expect(floorRefusal(new ApiError(409, body.error, [], body))).toEqual(['profile: from max to balanced'])
    expect(floorRefusal(new ApiError(409, 'it is not waiting any more'))).toBeNull()
    expect(floorRefusal(new Error('the daemon did not answer'))).toBeNull()
  })
})

describe('locksOf with the keys its profile brings', () => {
  it('treats a key the server says its profile brings as locked', () => {
    const locks = locksOf(view(floor({ latet_rules: 1, profile: 'balanced' }, { brought: ['include_private_ips', 'data'] })))!
    expect(locks.privateIps && locks.strictData).toBe(true)
    expect(locksOf(view(floor({ latet_rules: 1, profile: 'balanced' })))!.privateIps).toBe(false)
  })
})

describe('the editor of the policy (a server, the security role)', () => {
  const stored: PolicyText = {
    etag: '3f1c9a7e52d04b18',
    policy: {
      etag: '3f1c9a7e52d04b18',
      set_at: 1791191520,
      set_by: 'u_priyashahx',
      keys: ['profile'],
      brought: ['pii', 'id_regions', 'include_private_ips', 'data', 'images'],
      exceptions: 'personal',
      notices: [],
      in_force: true,
      text: 'latet_rules: 1\nprofile: max\n',
    },
    problems: [],
  }
  const none: PolicyText = { etag: null, policy: null, problems: [] }
  // A policy stored that the server cannot read (a newer release's, another master key): no text, its problem said.
  const unreadable: PolicyText = { etag: 'e7d1c0b9a8f6e5d4', policy: null, problems: ["the server's policy cannot be opened with this master key (InvalidTag)"] }

  it('is for those whose roles allow policy.edit', () => {
    expect(editsPolicy({ actions: ['policy.read', 'policy.edit'] })).toBe(true)
    expect(editsPolicy({ actions: ['policy.read'] })).toBe(false)
    expect(editsPolicy(undefined)).toBe(false)
  })

  it('starts from the stored text and saves a text that changed', () => {
    expect(storedText(stored)).toBe('latet_rules: 1\nprofile: max\n')
    expect(storedText(none)).toBe('')
    expect(canSave(storedText(stored), stored)).toBe(false)
    expect(canSave('latet_rules: 1\nprofile: balanced\n', stored)).toBe(true)
    expect(canSave(' \n', none)).toBe(false)
  })

  it("shows the server's reason when it does not take a policy", () => {
    const refused = new ApiError(400, 'not changed: https://latet.acme.lan: not valid YAML (line 1, column 10)')
    expect(policyRefusal(refused)).toBe(refused.message)
    expect(policyRefusal(new ApiError(413, 'larger than a rules file may be'))).toBe('larger than a rules file may be')
    expect(policyRefusal(new ApiError(403, 'your roles on this server do not allow policy.edit'))).toBeNull()
    expect(policyRefusal(new Error('the daemon did not answer'))).toBeNull()
  })

  it('removes a stored policy, the one the server cannot read too', () => {
    expect(removable(stored)).toBe(true)
    expect(removable(unreadable)).toBe(true)
    expect(removable(none)).toBe(false)
    expect(storedText(unreadable)).toBe('')
  })

  it('knows a save refused because someone else changed the policy since its text was read', () => {
    const error = 'not changed: the policy was changed since its text was read here: read it again, then change it'
    expect(policyChanged(new ApiError(409, error, [], { error, code: 'policy-changed' }))).toBe(true)
    expect(policyRefusal(new ApiError(409, error, [], { error, code: 'policy-changed' }))).toBeNull()
    expect(policyChanged(new ApiError(409, 'x', [], { error: 'x', floor: ['profile: from max to balanced'] }))).toBe(false)
    expect(policyChanged(new ApiError(403, 'x', [], { error: 'x', code: 'policy-changed' }))).toBe(false)
    expect(policyChanged(new Error('policy-changed'))).toBe(false)
  })
})

describe('a change of the policy from the editor', () => {
  const changed = () => {
    const error = 'not changed: the policy was changed since its text was read here: read it again, then change it'
    return new ApiError(409, error, [], { error, code: 'policy-changed' })
  }

  it('is made, and says the problems the server found beside it', async () => {
    expect(await changePolicy(async () => ({ etag: 'e1', keys: ['profile'], exceptions: 'personal', notices: [], problems: ['2 instances do not apply it'] }))).toEqual({
      kind: 'done',
      problems: ['2 instances do not apply it'],
    })
    expect(await changePolicy(async () => ({ removed: true }))).toEqual({ kind: 'done', problems: [] }) // a removal answers none
    expect(await changePolicy(async () => null)).toEqual({ kind: 'done', problems: [] })
  })

  it("is shown again as it is now when someone else changed the policy meanwhile: a save's and a removal's alike", async () => {
    expect(await changePolicy(async () => Promise.reject(changed()))).toEqual({ kind: 'changed' })
  })

  it("is refused with the server's reason for a text it cannot read, shown under the editor", async () => {
    const reason = 'not changed: https://latet.acme.lan: not valid YAML (line 2, column 1)'
    expect(await changePolicy(async () => Promise.reject(new ApiError(400, reason)))).toEqual({ kind: 'refused', reason })
  })

  it('is a failure in a toast for any other refusal', async () => {
    for (const error of [new ApiError(403, 'your roles on this server do not allow policy.edit'), new ApiError(503, 'try again'), new Error('offline')]) {
      expect(await changePolicy(async () => Promise.reject(error))).toEqual({ kind: 'failed', error })
    }
  })
})
