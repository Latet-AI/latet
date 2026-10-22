import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { ProjectNotice } from '../api/types'
import i18n, { LANGUAGES } from '../i18n'
import { detailReason, detailValues, noticeReason, refusedSave, refusedWords, stale, whenStale } from './refused'

const answered = (status: number, reason: string, more: Record<string, unknown> = {}) =>
  new ApiError(status, 'the server words it in English', [], { error: 'the server words it in English', reason, ...more })

/** Every key exists in every language, none falling back to English. */
async function inEveryLanguage(keys: (string | null)[]): Promise<void> {
  for (const language of LANGUAGES) {
    await i18n.changeLanguage(language)
    for (const key of keys) expect(i18n.exists(key as string, { lng: language, fallbackLng: [] }), `${language}: ${key}`).toBe(true)
  }
  await i18n.changeLanguage('en')
}

describe('refusedSave', () => {
  it('says why a server did not keep a save of your settings, in each language', async () => {
    // The reasons the dashboard's saves meet (E0b's and E0c's `reason` codes), each pinned to its words.
    const said = {
      stale: refusedSave(answered(409, 'settings_stale')),
      large: refusedSave(answered(413, 'settings_too_large')),
      many: refusedSave(answered(409, 'settings_too_many_rules')),
      projects: refusedSave(answered(409, 'settings_too_many_projects')),
      drops: refusedSave(answered(409, 'settings_drops_shared', { dropped: { upstreams: 1 } })),
      down: refusedSave(answered(503, 'settings_unavailable')),
      waits: refusedSave(answered(409, 'settings_change_waits')),
    }
    expect(said).toEqual({
      stale: 'errors.settingsStale',
      large: 'errors.settingsTooLarge',
      many: 'errors.settingsTooManyRules',
      projects: 'errors.settingsTooManyProjects',
      drops: 'errors.settingsDropsShared',
      down: 'errors.settingsUnavailable',
      waits: 'errors.settingsChangeWaits',
    })
    await inEveryLanguage(Object.values(said))
  })

  it('says why a computer changed nothing while a repository or a project file could not be read, in each language', async () => {
    // A reload or an import weighed in repositories Latet could not all read now (EOP-26); a project's latet.yaml
    // there and not read, never saved over (EOP-33).
    const said = {
      unchecked: refusedSave(answered(503, 'repositories_unchecked')),
      unreadable: refusedSave(answered(409, 'repo_file_unreadable')),
      unavailable: refusedSave(answered(503, 'repo_file_unavailable')),
    }
    expect(said).toEqual({
      unchecked: 'errors.repositoriesUnchecked',
      unreadable: 'errors.repoFileUnreadable',
      unavailable: 'errors.repoFileUnavailable',
    })
    await inEveryLanguage([...Object.values(said), 'projects.repo.unreadable'])
  })

  it('leaves every other answer to the usual words', () => {
    expect(refusedSave(answered(409, 'something else'))).toBeNull()
    expect(refusedSave(new ApiError(409, 'this would remove 1 term set for everyone on the server'))).toBeNull()
    expect(refusedSave(new Error('settings_stale'))).toBeNull()
  })
})

describe('refusedWords', () => {
  it('counts what a save would remove of what is set for everyone, never which items', async () => {
    const t = i18n.t.bind(i18n)
    // A server sets destinations for everyone, and no rule: a count for any other list is not worded.
    const dropped = answered(409, 'settings_drops_shared', { dropped: { upstreams: 2, terms: 3, allow_values: 1, unknown: 5 } })
    expect(refusedWords(t, dropped)).toBe(
      'This would remove 2 destinations set for everyone on this server, so nothing was saved. ' +
        "Only the server's administrators can remove them.",
    )
    for (const language of LANGUAGES) {
      await i18n.changeLanguage(language)
      const words = refusedWords(t, dropped) ?? ''
      expect(words).toContain('2')
      expect(words).not.toContain('{{')
    }
    await i18n.changeLanguage('en')
    // The destinations' words, every plural form a language needs.
    await inEveryLanguage(['errors.dropped.upstreams_one', 'errors.dropped.upstreams_other'])
  })

  it('words the other refusals as refusedSave names them, and nothing else', () => {
    const t = i18n.t.bind(i18n)
    expect(refusedWords(t, answered(409, 'settings_stale'))).toBe(i18n.t('errors.settingsStale'))
    expect(refusedWords(t, answered(409, 'something else'))).toBeNull()
    expect(refusedWords(t, new Error('no'))).toBeNull()
  })
})

describe('stale', () => {
  it('knows a save refused because the settings changed since the page read them', () => {
    expect(stale(answered(409, 'settings_stale'))).toBe(true)
    expect(stale(answered(409, 'settings_change_waits'))).toBe(false)
    expect(stale(answered(413, 'settings_stale'))).toBe(false)
    expect(stale(new ApiError(409, 'settings_stale'))).toBe(false)
  })
})

describe('whenStale', () => {
  it('reads the settings again after a save refused because they changed since the page read them', () => {
    const read: string[] = []
    expect(whenStale(answered(409, 'settings_stale'), () => read.push('settings'), () => read.push('project'))).toBe(true)
    expect(read).toEqual(['settings', 'project'])
  })

  it('reads nothing again after any other refusal', () => {
    const read: string[] = []
    for (const error of [answered(409, 'settings_change_waits'), answered(503, 'settings_unavailable'), new Error('no')]) {
      expect(whenStale(error, () => read.push('settings'))).toBe(false)
    }
    expect(read).toEqual([])
  })
})

describe('noticeReason', () => {
  const notice = (reason?: string): ProjectNotice => ({
    source: 'personal',
    key: '',
    level: 'error',
    message: 'the server words it in English',
    ...(reason === undefined ? {} : { reason }),
  })

  it('says why a server builds no rules for a project, in each language', async () => {
    const keys = [noticeReason(notice('space_past_share')), noticeReason(notice('project_unbuilt'))]
    expect(keys).toEqual(['projects.pastShare', 'projects.unbuilt'])
    await inEveryLanguage(keys)
  })

  it('says why a repository file\'s setting is not applied, in each language', async () => {
    // A latet.yaml that sets what only your own settings for the project may set (values never to mask, ...).
    const key = noticeReason({ ...notice('repo_cannot_set'), source: 'repo', key: 'allow_values', level: 'ignored' })
    expect(key).toBe('projects.repoCannotSet')
    await inEveryLanguage([key])
  })

  it('says why a repository file\'s other settings are not applied, in each language', async () => {
    // A setting of the whole of Latet (port), and one Latet does not know.
    const keys = [
      noticeReason({ ...notice('repo_whole_latet'), source: 'repo', key: 'port', level: 'ignored' }),
      noticeReason({ ...notice('repo_unknown'), source: 'repo', key: 'whatever', level: 'ignored' }),
    ]
    expect(keys).toEqual(['projects.repoWholeLatet', 'projects.repoUnknown'])
    await inEveryLanguage(keys)
  })

  it('leaves every other notice to its own words', () => {
    expect(noticeReason(notice())).toBeNull()
    expect(noticeReason(notice('something else'))).toBeNull()
  })
})

describe('detailReason', () => {
  it('says why a field of a save was refused on a server, in each language', async () => {
    const destination = { loc: ['upstreams', 0], msg: 'English words', type: 'value_error', reason: 'destination_refused', url: 'http://10.0.0.5' }
    const key = { loc: ['llm_review', 'api_key_file'], msg: 'English words', type: 'value_error', reason: 'reviewer_key_server' }
    const keys = [detailReason(destination), detailReason(key)]
    expect(keys).toEqual(['errors.destinationRefused', 'errors.reviewerKeyServer'])
    expect(detailValues(destination)).toEqual({ setting: 'upstreams.0', url: 'http://10.0.0.5' })
    expect(detailValues(key)).toEqual({ setting: 'llm_review.api_key_file', url: '' })
    await inEveryLanguage(keys)
  })

  it('leaves every other field to its own words', () => {
    expect(detailReason({ loc: ['terms', 0], msg: 'no' })).toBeNull()
    expect(detailReason({ reason: 'something else' })).toBeNull()
    expect(detailReason('a text')).toBeNull()
    expect(detailValues(null)).toEqual({ setting: '', url: '' })
  })
})
