import { useEffect, useState } from 'react'

/** True once `element` has come near the viewport (stays true). */
export function useInView(element: Element | null, margin = '200px'): boolean {
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    if (!element || seen) return
    if (typeof IntersectionObserver === 'undefined') {
      const timer = setTimeout(() => setSeen(true), 0)
      return () => clearTimeout(timer)
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setSeen(true)
          observer.disconnect()
        }
      },
      { rootMargin: margin },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [element, margin, seen])
  return seen
}
