import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import de from './locales/de.json'
import en from './locales/en.json'
import es from './locales/es.json'
import fr from './locales/fr.json'
import it from './locales/it.json'
import { readPref, writePref } from './lib/prefs'

export const resources = {
  en: { translation: en },
  it: { translation: it },
  de: { translation: de },
  fr: { translation: fr },
  es: { translation: es },
} as const

export type Language = keyof typeof resources

export const LANGUAGES = Object.keys(resources) as Language[]

/** Each language in its own words: the menu shows them so, whatever the dashboard's language. */
export const LANGUAGE_NAMES: Record<Language, string> = {
  en: 'English',
  it: 'Italiano',
  de: 'Deutsch',
  fr: 'Français',
  es: 'Español',
}

const PREF = 'language'

const isLanguage = (value: unknown): value is Language => LANGUAGES.includes(value as Language)

/** The reader's choice when it is a language of the dashboard, else the browser's first one it has, else English. */
export function pickLanguage(stored: unknown, preferred: readonly string[]): Language {
  if (isLanguage(stored)) return stored
  for (const tag of preferred) {
    const exact = LANGUAGES.find((lang) => lang.toLowerCase() === tag.toLowerCase())
    if (exact) return exact
    const base = LANGUAGES.find((lang) => lang === tag.split('-')[0]?.toLowerCase())
    if (base) return base
  }
  return 'en'
}

function browserLanguages(): readonly string[] {
  return typeof navigator === 'undefined' ? [] : (navigator.languages ?? [navigator.language])
}

void i18n.use(initReactI18next).init({
  resources,
  lng: pickLanguage(readPref<unknown>(PREF, null), browserLanguages()),
  fallbackLng: 'en',
  interpolation: { escapeValue: false }, // React escapes already
  returnNull: false,
})

/** The reader picks a language: kept on this browser, and the dashboard changes at once. */
export function setLanguage(lng: Language): void {
  writePref(PREF, lng)
  void i18n.changeLanguage(lng)
}

const syncDocument = (lng: string) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng
}
syncDocument(i18n.language)
i18n.on('languageChanged', syncDocument)

/** Marks a translation key that is used through a variable, so `npm run check:i18n` sees it. */
export function tk<K extends string>(key: K): K {
  return key
}

export default i18n
