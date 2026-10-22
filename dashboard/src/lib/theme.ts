import { useEffect } from 'react'
import { readPref, usePref } from './prefs'

export type ThemePref = 'system' | 'light' | 'dark'

const KEY = 'theme'
const query = () => window.matchMedia('(prefers-color-scheme: dark)')

export function applyTheme(pref: ThemePref): void {
  const dark = pref === 'dark' || (pref === 'system' && query().matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

/** Apply the stored theme before React renders (avoids a flash when it differs from the OS). */
export function applyStoredTheme(): void {
  applyTheme(readPref<ThemePref>(KEY, 'system'))
}

export function useTheme(): [ThemePref, (pref: ThemePref) => void] {
  const [pref, setPref] = usePref<ThemePref>(KEY, 'system')
  useEffect(() => {
    applyTheme(pref)
    if (pref !== 'system') return
    const media = query()
    const onChange = () => applyTheme('system')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [pref])
  return [pref, setPref]
}
