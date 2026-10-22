/** The page links of a long list, as GitHub draws them: the first, the last and two on each side of the current page,
 *  with a gap where pages are left out (a gap of one page shows that page instead). */
export function pageNumbers(current: number, last: number): (number | 'gap')[] {
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1)
  const shown = new Set([1, last])
  for (let page = current - 2; page <= current + 2; page++) if (page >= 1 && page <= last) shown.add(page)
  const sorted = [...shown].sort((a, b) => a - b)
  const result: (number | 'gap')[] = []
  for (const page of sorted) {
    const before = result.at(-1)
    if (typeof before === 'number' && page - before === 2) result.push(before + 1)
    else if (typeof before === 'number' && page - before > 2) result.push('gap')
    result.push(page)
  }
  return result
}

/** The first and the last value a page shows, counted from 1 (0 and 0 when nothing matches). */
export function pageRange(page: number, perPage: number, total: number): { from: number; to: number } {
  if (total === 0) return { from: 0, to: 0 }
  return { from: (page - 1) * perPage + 1, to: Math.min(total, page * perPage) }
}
