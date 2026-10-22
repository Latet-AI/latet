import { describe, expect, it } from 'vitest'
import i18n, { LANGUAGES } from '../i18n'
import { servedFromServer, serverHost } from './serverPage'

describe('a Latet server’s page', () => {
  it('knows a server’s page by its host', () => {
    expect(serverHost('https://latet.acme.lan')).toBe('latet.acme.lan')
    expect(serverHost('not an address')).toBe('not an address')
    expect(servedFromServer('latet.acme.lan')).toBe(true)
    expect(servedFromServer('127.0.0.1')).toBe(false)
    expect(servedFromServer('localhost')).toBe(false)
  })
})

describe('the words a server’s pages say in place of a computer’s', () => {
  it('are in every language', async () => {
    const keys = ['rules.identity.toggleServer', 'rules.identity.detectedServer', 'projects.personal.descriptionServer']
    for (const language of LANGUAGES) {
      await i18n.changeLanguage(language)
      for (const key of keys) expect(i18n.exists(key, { lng: language, fallbackLng: [] }), `${language}: ${key}`).toBe(true)
    }
    await i18n.changeLanguage('en')
  })
})
