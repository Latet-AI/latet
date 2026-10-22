// A list's own Import and Export, in its table's toolbar: the terms and the values never masked as CSV, the recognizers
// as a rules file. Export writes the rows the table shows, as filtered; Import goes through the daemon's import, which
// shows what it would add before anything changes, adds and never replaces, and holds back what lowers protection until
// you opt in (RulesTransfer's preview).

import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { RecognizerEntry, Term } from '../../api/types'
import { tk } from '../../i18n'
import { allowedCsv, allowedFromCsv, leftOutByExport, otherLists, rulesFile, termsCsv, termsFromCsv, type CsvProblem } from '../../lib/listTransfer'
import { useMe } from '../../lib/me'
import type { AllowItem, RuleRow } from '../../lib/ruleTable'
import { download } from '../../lib/transfer'
import { Icon } from '../Icon'
import { ImportDialog, type Preview } from '../RulesTransfer'
import { useToast } from '../Toast'
import { errorText } from '../ui'

/** Which list a file holds. */
export type TransferKind = 'terms' | 'values' | 'recognizers'

const REASON: Record<CsvProblem['reason'], string> = {
  empty: tk('rules.import.reason.empty'),
  yesNo: tk('rules.import.reason.yesNo'),
  noLetterOrDigit: tk('rules.import.reason.noLetterOrDigit'),
  tooLong: tk('rules.import.reason.tooLong'),
  quote: tk('rules.import.reason.quote'),
  kind: tk('rules.import.reason.kind'),
  pattern: tk('rules.import.reason.pattern'),
  type: tk('rules.import.reason.type'),
  columns: tk('rules.import.reason.columns'),
}

/** What a file of the list is saved as, and holds. */
function exported(kind: TransferKind, items: unknown[]): { name: string; text: string; type: string } {
  if (kind === 'terms') return { name: 'latet-terms.csv', text: termsCsv(items as Term[]), type: 'text/csv' }
  if (kind === 'values') return { name: 'latet-allowed-values.csv', text: allowedCsv(items as AllowItem[]), type: 'text/csv' }
  return { name: 'latet-recognizers.yaml', text: rulesFile({ recognizers: items as RecognizerEntry[] }), type: 'application/yaml' }
}

/**
 * The recognizers' rows an export may write, as the daemon's export of the rules decides (`/rules/export`): it leaves
 * out each one whose examples hold values Latet has seen for real, the global rules' and your own for the project
 * (a project's latet.yaml is in its repository already). What it says of them, in its words.
 */
async function exportable<T>(rows: RuleRow<T>[], project: string | undefined): Promise<{ rows: RuleRow<T>[]; warnings: string[] }> {
  const [global, own] = await Promise.all([api.exportRules(), project ? api.exportRules(project) : Promise.resolve(null)])
  const out = { global: leftOutByExport(global.warnings), personal: leftOutByExport(own?.warnings ?? []) }
  const name = (row: RuleRow<T>) => String((row.item as RecognizerEntry).name ?? '')
  const kept = rows.filter((row) => !(row.source === 'global' && out.global.has(name(row))) && !(row.source === 'personal' && out.personal.has(name(row))))
  const said = [...global.warnings, ...(own?.warnings ?? [])].filter((warning) => warning.startsWith('recognizer') && kept.length < rows.length)
  return { rows: kept, warnings: [...new Set(said)] }
}

export function ListTransfer<T>({ kind, rows, project, onImported }: { kind: TransferKind; rows: RuleRow<T>[]; project?: string; onImported: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { me } = useMe()
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  // A Latet server refuses an import into your settings for a project: those are imported on your computer.
  const canImport = !(project !== undefined && me?.server)

  // Export keeps the guards of the whole export: the recognizers checked by the daemon against the vault, the terms
  // and the values said to be in the file in clear.
  const exportRows = async () => {
    setBusy(true)
    try {
      const guarded = kind === 'recognizers' ? await exportable(rows, project) : { rows, warnings: [] }
      const file = exported(
        kind,
        guarded.rows.map((row) => row.item),
      )
      download(file.name, file.text, file.type)
      toast.show(
        kind === 'terms'
          ? t('transfer.exported', { name: file.name })
          : kind === 'values'
            ? t('rules.export.valuesInClear', { name: file.name })
            : t('rules.export.saved', { name: file.name }),
      )
      for (const warning of guarded.warnings) toast.show(warning, 'info')
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }
  /** The file read in the browser: a CSV made a rules file of that list alone, a recognizers file sent as it is when
   * it holds recognizers alone (a whole rules file is imported from the top of the page, which shows every list). */
  const pick = async (file: File) => {
    setBusy(true)
    try {
      const text = await file.text()
      let rules = text
      let problems: CsvProblem[] = []
      const others = kind === 'recognizers' ? otherLists(text) : []
      if (others.length > 0) {
        toast.show(t('rules.import.otherLists', { name: file.name, lists: others.join(', ') }), 'fail')
        return
      }
      if (kind === 'terms') {
        const read = termsFromCsv(text)
        rules = rulesFile({ terms: read.terms })
        problems = read.problems
      } else if (kind === 'values') {
        const read = allowedFromCsv(text)
        rules = rulesFile({
          allow_values: read.items.filter((item) => item.kind === 'value').map((item) => item.value),
          allow_patterns: read.items.filter((item) => item.kind === 'pattern').map((item) => item.value),
        })
        problems = read.problems
      }
      const result = await api.importRules({ file: rules, project })
      const lines = problems.map((problem) => t('rules.import.line', { line: problem.line, reason: t(REASON[problem.reason]) }))
      setPreview({ name: file.name, file: rules, report: result.report, lines })
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className="btn btn-sm" disabled={rows.length === 0 || busy} title={t('rules.export.title')} onClick={() => void exportRows()}>
        <Icon name="download" size={14} />
        {t('rules.export.button')}
      </button>
      {canImport && (
        <button type="button" className="btn btn-sm" disabled={busy} title={t('rules.import.title')} onClick={() => input.current?.click()}>
          <Icon name="upload" size={14} />
          {t('rules.import.button')}
        </button>
      )}
      <input
        ref={input}
        type="file"
        accept={kind === 'recognizers' ? '.yaml,.yml,.json,text/yaml,application/yaml,application/json' : '.csv,.txt,text/csv,text/plain'}
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
