// Your rules in a file, for a colleague: what the dashboard needs around `latet rules export/import`.

import type { RulesReport } from '../api/types'

/** Whether importing would change anything: what adds protection, or with `everything` what is held back too. */
export function importable(report: RulesReport, everything: boolean): boolean {
  return report.added.length > 0 || (everything && report.held.length > 0)
}

/** A file saved to the browser's downloads: of rules (YAML) unless `type` says otherwise (a list's CSV). */
export function download(name: string, text: string, type = 'application/yaml'): void {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
