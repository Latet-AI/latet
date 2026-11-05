import { describe, expect, it } from 'vitest'
import de from '../locales/de.json'
import en from '../locales/en.json'
import es from '../locales/es.json'
import fr from '../locales/fr.json'
import it_ from '../locales/it.json'
import i18n from '../i18n'
import { revealedText } from './vault'

describe('revealedText', () => {
  it('shows a value the vault reveals as it is', () => {
    expect(revealedText({ original: 'giulia.conti@pec-studio.it' }, i18n.t)).toBe('giulia.conti@pec-studio.it')
  })

  it("shows a plain label in place of Latet's own launch key, which is never revealed", () => {
    expect(revealedText({ original: null, launch_key: true }, i18n.t)).toBe(en.vault.reveal.launchKey)
    expect(en.vault.reveal.launchKey).toBe('Latet launch key')
  })

  it('has the label in every language', () => {
    for (const locale of [de, es, fr, it_]) {
      expect(locale.vault.reveal.launchKey).toBeTruthy()
      expect(locale.vault.reveal.launchKey).not.toBe(en.vault.reveal.launchKey)
    }
  })
})
