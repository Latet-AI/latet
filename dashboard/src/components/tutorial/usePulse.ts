// What a tutorial step asks you to look at, pulsing: the marks of its values (both sides of each) and the
// blocks or cards it names, inside one pane. The attribute is set on the elements the components drew, as
// the inspector's `setPairActive` sets `data-active`, and set again on what they draw later (a block that
// loads, a text that unfolds); index.css draws it, still under reduced motion.

import { useEffect, type RefObject } from 'react'
import type { Target } from '../../lib/tutorial'

export const PULSE = 'data-tutorial-pulse'

/** Scrolls an element into view when it is out of it, without motion when the system asks for none. */
export function bringIntoView(element: Element): void {
  const rect = element.getBoundingClientRect()
  if (rect.top >= 0 && rect.bottom <= window.innerHeight) return
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  element.scrollIntoView({ block: 'nearest', behavior: still ? 'auto' : 'smooth' })
}

/** Makes `target` pulse inside `root` (nothing when null), and brings the first of it into view once. */
export function usePulse(root: RefObject<HTMLElement | null>, target: Target | null): void {
  // By value: a target computed again with the same marks is the same target.
  const key = target === null ? null : JSON.stringify([target.pairs, target.blocks])
  useEffect(() => {
    const box = root.current
    if (!box || key === null) return
    const [pairs, ids] = JSON.parse(key) as [string[], string[]]
    if (pairs.length === 0 && ids.length === 0) return
    const marked = new Set<Element>()
    let seen = false
    const apply = () => {
      const found = [
        ...pairs.flatMap((pair) => [...box.querySelectorAll(`[data-pair="${CSS.escape(pair)}"]`)]),
        ...ids.flatMap((id) => {
          const element = box.querySelector(`#${CSS.escape(id)}`)
          return element ? [element] : []
        }),
      ]
      for (const element of found) {
        if (!element.hasAttribute(PULSE)) element.setAttribute(PULSE, '')
        marked.add(element)
      }
      if (!seen && found[0]) {
        seen = true
        bringIntoView(found[0])
      }
    }
    apply()
    // Setting the attribute changes no child: the observer does not wake itself.
    const observer = new MutationObserver(apply)
    observer.observe(box, { childList: true, subtree: true })
    return () => {
      observer.disconnect()
      for (const element of marked) element.removeAttribute(PULSE)
    }
  }, [root, key])
}
