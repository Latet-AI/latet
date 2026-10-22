// The sections of the page on screen, for the side menu: each <Section anchor> registers itself, and
// the menu lists them under the page's entry, in the order they appear.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export interface PageSection {
  id: string
  title: string
}

interface SectionsApi {
  sections: PageSection[]
  add: (section: PageSection) => () => void
}

const SectionsContext = createContext<SectionsApi | null>(null)

export function SectionsProvider({ children }: { children: ReactNode }) {
  const [sections, setSections] = useState<PageSection[]>([])
  const add = useCallback((section: PageSection) => {
    setSections((list) => [...list.filter((s) => s.id !== section.id), section])
    return () => setSections((list) => list.filter((s) => s.id !== section.id))
  }, [])
  const value = useMemo(() => ({ sections, add }), [sections, add])
  return <SectionsContext.Provider value={value}>{children}</SectionsContext.Provider>
}

/** Lists a section of the page in the side menu while it is on screen. */
export function usePageSection(id: string | undefined, title: string) {
  const add = useContext(SectionsContext)?.add
  useEffect(() => (add && id ? add({ id, title }) : undefined), [add, id, title])
}

/** The page's sections, in the order they appear on it. */
export function usePageSections(): PageSection[] {
  const sections = useContext(SectionsContext)?.sections
  return useMemo(() => inPageOrder(sections ?? []), [sections])
}

export function inPageOrder(sections: readonly PageSection[]): PageSection[] {
  return [...sections].sort((a, b) => {
    const first = document.getElementById(a.id)
    const second = document.getElementById(b.id)
    if (!first || !second) return 0
    return first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1
  })
}

/** The section being read: the last one whose top has scrolled past the top of the window. */
export function useActiveSection(sections: readonly PageSection[]): string | null {
  const [active, setActive] = useState<string | null>(null)
  useEffect(() => {
    if (sections.length === 0) return
    const update = () => {
      let current: string | null = sections[0]?.id ?? null
      for (const section of sections) {
        const el = document.getElementById(section.id)
        if (el && el.getBoundingClientRect().top <= 96) current = section.id
      }
      // At the bottom of the page the last sections never reach the top: the last one is being read.
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
        current = sections[sections.length - 1]?.id ?? current
      }
      setActive(current)
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [sections])
  return active
}
