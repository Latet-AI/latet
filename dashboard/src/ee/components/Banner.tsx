import { useEffect, useRef, type ReactNode } from 'react'
import { reveal } from '../lib/changes'

/**
 * A notice that says what a change came to, above the content it concerns. An alert, unless `quiet` (a status, for a
 * page that says it from the start): the alert is the only feedback of the change that raised it (no toast repeats it),
 * so when it appears it is brought to the person, who may be looking at the button far below it: scrolled into view
 * and focused (`reveal`). It appears when the change is refused, so the effect runs then, once.
 */
export function Banner({ quiet = false, className, children }: { quiet?: boolean; className: string; children: ReactNode }) {
  const element = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!quiet) reveal(element.current)
  }, [quiet])
  return (
    // scroll-mt: scrolled into view with a little room above it, not flush with the top of the window.
    <div ref={element} role={quiet ? 'status' : 'alert'} tabIndex={quiet ? undefined : -1} className={`scroll-mt-4 scroll-mb-4 ${className}`}>
      {children}
    </div>
  )
}
