// Your rules in a file, for a colleague: Export downloads them, Import shows what a file would add
// (and what it would lower, applied only if you tick it) before anything changes.

import { useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { RulesReport } from '../api/types'
import { savedChangePending } from '../lib/refused'
import { download, importable } from '../lib/transfer'
import { DocsWhyLink } from './DocsLinks'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { errorText } from './ui'

export interface Preview {
  name: string
  file: string
  report: RulesReport
  /** What of the file the page could not read (a list's CSV), said before what the daemon did not read. */
  lines?: string[]
}

/**
 * Export and Import, for your rules or (with `project`) your own settings for one project. Import is
 * off while `dirty`: the form has changes not saved yet. `onImported` gets nothing: reload what you show.
 */
export function RulesTransfer({ project, dirty = false, onImported }: { project?: string; dirty?: boolean; onImported: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    try {
      await action()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }
  const exportRules = () =>
    run(async () => {
      const out = await api.exportRules(project)
      download(out.name, out.file)
      toast.show(t('transfer.exported', { name: out.name }))
      for (const warning of out.warnings) toast.show(warning, 'info')
    })
  const pick = (file: File) =>
    run(async () => {
      const text = await file.text()
      const result = await api.importRules({ file: text, project })
      setPreview({ name: file.name, file: text, report: result.report })
    })

  return (
    <>
      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void exportRules()}>
        <Icon name="download" size={14} />
        {project ? t('transfer.exportProject') : t('transfer.export')}
      </button>
      <button
        type="button"
        className="btn btn-sm"
        disabled={busy || dirty}
        title={dirty ? t('transfer.saveFirst') : undefined}
        onClick={() => input.current?.click()}
      >
        <Icon name="upload" size={14} />
        {project ? t('transfer.importProject') : t('transfer.import')}
      </button>
      <input
        ref={input}
        type="file"
        accept=".yaml,.yml,text/yaml,application/yaml"
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void pick(file)
        }}
      />
      {preview && (
        <ImportDialog
          preview={preview}
          project={project}
          onClose={() => setPreview(null)}
          onImported={() => {
            setPreview(null)
            onImported()
          }}
        />
      )}
    </>
  )
}

export function ImportDialog({ preview, project, onClose, onImported }: { preview: Preview; project?: string; onClose: () => void; onImported: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [everything, setEverything] = useState(false)
  const [busy, setBusy] = useState(false)
  const { report } = preview
  const nothing = report.added.length === 0 && report.held.length === 0

  useEffect(() => {
    const dialog = ref.current
    if (dialog && !dialog.open) dialog.showModal()
  }, [])

  const apply = async () => {
    setBusy(true)
    try {
      const result = await api.importRules({ file: preview.file, project, apply: true, all: everything })
      toast.show(result.applied ? t('transfer.imported') : t('transfer.nothing'))
      onImported()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
      if (savedChangePending(error)) onImported()
    } finally {
      setBusy(false)
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      className="floating m-auto w-[600px] max-w-[calc(100vw-32px)] p-0 backdrop:bg-[rgb(10_11_13/0.45)]"
    >
      <div className="max-h-[70vh] space-y-4 overflow-auto p-5">
        <div>
          <h2 id={titleId} className="text-title font-semibold text-ink">
            {t('transfer.title')}
          </h2>
          <p className="mono mt-1 break-all text-meta text-ink-3">{preview.name}</p>
        </div>
        {nothing && <p className="text-body text-ink-2">{t('transfer.nothingNew')}</p>}
        <Part title={t('transfer.added')} items={report.added} tone="good" />
        {report.held.length > 0 && (
          <div className="space-y-2 rounded-lg border border-warn/60 bg-warn/10 p-3">
            <Part title={t('transfer.held')} items={report.held} tone="warn" />
            <p className="text-meta">
              <DocsWhyLink target="why.importHeld" />
            </p>
            <label className="flex cursor-pointer items-start gap-2.5 text-ui text-ink">
              <input type="checkbox" className="mt-0.5 accent-[var(--ink)]" checked={everything} onChange={(e) => setEverything(e.target.checked)} />
              <span>
                <span className="block font-medium">{t('transfer.everything')}</span>
                <span className="block text-meta text-ink-2">{t('transfer.everythingHelp')}</span>
              </span>
            </label>
          </div>
        )}
        <Part title={t('transfer.skipped')} items={nothing ? report.skipped.slice(1) : report.skipped} tone="plain" />
        <Part title={t('transfer.notices')} items={[...(preview.lines ?? []), ...report.notices]} tone="warn" />
      </div>
      <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
        <button type="button" className="btn" onClick={onClose} autoFocus>
          {nothing ? t('common.close') : t('common.cancel')}
        </button>
        {!nothing && (
          <button type="button" className="btn btn-primary" disabled={busy || !importable(report, everything)} onClick={() => void apply()}>
            <Icon name="check" size={14} />
            {busy ? t('transfer.importing') : t('transfer.apply')}
          </button>
        )}
      </div>
    </dialog>
  )
}

const TONE = { good: 'text-good-ink', warn: 'text-warn-ink', plain: 'text-ink-3' } as const

function Part({ title, items, tone }: { title: string; items: string[]; tone: keyof typeof TONE }) {
  if (items.length === 0) return null
  return (
    <div>
      <p className={`text-ui font-medium ${TONE[tone]}`}>{title}</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ui text-ink">
        {items.map((item, i) => (
          <li key={i} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </div>
  )
}
