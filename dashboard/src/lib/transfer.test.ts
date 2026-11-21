import { describe, expect, it, vi } from 'vitest'
import { download, importable } from './transfer'

describe('importing rules', () => {
  const report = { added: [], skipped: ['nothing new: these rules are yours already'], held: ['values never to mask: ACME'], notices: [] }

  it('changes something only when it adds protection, or when the rest is chosen too', () => {
    expect(importable(report, false)).toBe(false)
    expect(importable(report, true)).toBe(true)
    expect(importable({ ...report, added: ['terms: Banca Ligure'] }, false)).toBe(true)
    expect(importable({ ...report, held: [] }, true)).toBe(false)
  })
})

it('downloads server report bytes unchanged, including BOM, quoting and JSON truncation markers', async () => {
  const blobs: Blob[] = []
  const link = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
  vi.useFakeTimers()
  vi.stubGlobal('document', { createElement: () => link, body: { append: () => undefined } })
  vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => { blobs.push(blob as Blob); return 'blob:report-fixture' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  try {
    const csv = '\uFEFFday,project\r\n2026-10-01,"\'=SUM(1,2)"\r\n'
    download('report.csv', csv, 'text/csv')
    expect(new Uint8Array(await blobs[0]!.arrayBuffer())).toEqual(new TextEncoder().encode(csv))
    expect(blobs[0]!.type).toBe('text/csv'); expect(link.download).toBe('report.csv'); expect(link.click).toHaveBeenCalledOnce()
    const json = '{"administration_truncated":true,"rows":[]}'
    download('report.json', json, 'application/json')
    expect(await blobs[1]!.text()).toBe(json); expect(blobs[1]!.type).toBe('application/json')
    vi.runAllTimers()
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  } finally { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() }
})
