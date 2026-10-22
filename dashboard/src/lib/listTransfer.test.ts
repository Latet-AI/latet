import { describe, expect, it } from 'vitest'
import type { RecognizerEntry, Term } from '../api/types'
import { allowedCsv, allowedFromCsv, leftOutByExport, otherLists, rulesFile, termsCsv, termsFromCsv } from './listTransfer'

const term = (value: string, type = 'CLIENT', case_sensitive = false, whole_word = true): Term => ({ value, type, case_sensitive, whole_word })

describe('the terms as CSV', () => {
  it('write a header row, then a row each, quoting what needs it', () => {
    const csv = termsCsv([term('Whitfield & Co'), term('Smith, Jones', 'CLIENT', true, false), term('The "Orion" deal', 'PROJECT')])
    expect(csv).toBe(
      ['value,type,case_sensitive,whole_word', 'Whitfield & Co,CLIENT,false,true', '"Smith, Jones",CLIENT,true,false', '"The ""Orion"" deal",PROJECT,false,true', ''].join('\r\n'),
    )
  })

  it('read back what they wrote', () => {
    const terms = [term('Whitfield & Co'), term('Smith, Jones', 'CLIENT', true, false), term('The "Orion" deal', 'PROJECT'), term('Line\nbreak')]
    expect(termsFromCsv(termsCsv(terms))).toEqual({ terms, problems: [] })
  })

  it('read a list pasted without a header, the type and how it matches left to their defaults', () => {
    expect(termsFromCsv('Harbor Bank\nFernsby Studio,CLIENT\n')).toEqual({
      terms: [term('Harbor Bank', 'CUSTOM'), term('Fernsby Studio', 'CLIENT')],
      problems: [],
    })
  })

  it('read a CSV a spreadsheet saved with a byte order mark first', () => {
    expect(termsFromCsv(`${String.fromCharCode(0xfeff)}value,type
Harbor Bank,CLIENT
`)).toEqual({ terms: [term('Harbor Bank')], problems: [] })
  })

  it('report a line with a problem, by its number, and read the others', () => {
    const csv = 'value,type,case_sensitive,whole_word\nHarbor Bank,CLIENT,maybe,true\n,CLIENT\nFernsby Studio\n--\n"unclosed,CLIENT'
    expect(termsFromCsv(csv)).toEqual({
      terms: [term('Fernsby Studio', 'CUSTOM')],
      problems: [
        { line: 2, reason: 'yesNo' },
        { line: 3, reason: 'empty' },
        { line: 5, reason: 'noLetterOrDigit' },
        { line: 6, reason: 'quote' },
      ],
    })
  })

  it('report a line a comma not in quotes split: a type that is not one, or more than four fields', () => {
    expect(termsFromCsv('Acme, Inc.\nAcme, Inc.,CLIENT\nHarbor Bank, client \nOrion,CLIENT,no,yes,more\n')).toEqual({
      terms: [term('Harbor Bank')],
      problems: [
        { line: 1, reason: 'type' },
        { line: 2, reason: 'type' },
        { line: 4, reason: 'columns' },
      ],
    })
  })
})

describe('the values never masked as CSV', () => {
  it('write a kind and a value each, and read them back', () => {
    const items = [
      { kind: 'value' as const, value: 'noreply@hayes-partners.com' },
      { kind: 'pattern' as const, value: '^test-[a-z0-9]{8}@example\\.com$' },
      { kind: 'value' as const, value: 'a,b' },
    ]
    const csv = allowedCsv(items)
    expect(csv.split('\r\n')[0]).toBe('kind,value')
    expect(allowedFromCsv(csv)).toEqual({ items, problems: [] })
  })

  it('take a line without a kind as a value, and report a kind it does not know', () => {
    expect(allowedFromCsv('kind,value\nbilling-dev\nregex,^x$\npattern,(')).toEqual({
      items: [{ kind: 'value', value: 'billing-dev' }],
      problems: [
        { line: 3, reason: 'kind' },
        { line: 4, reason: 'pattern' },
      ],
    })
  })
})

describe('rulesFile', () => {
  it('is a rules file the daemon reads (JSON is YAML), holding only the lists given', () => {
    const recognizers: RecognizerEntry[] = [{ name: 'badge', pattern: '\\bBD-\\d{6}\\b', future_key: 1 }]
    const file = rulesFile({ recognizers })
    expect(JSON.parse(file)).toEqual({ latet_rules: 1, recognizers })
    expect(JSON.parse(rulesFile({ terms: [term('Orion')], allow_values: [], allow_patterns: ['^x$'] }))).toEqual({
      latet_rules: 1,
      terms: [term('Orion')],
      allow_patterns: ['^x$'],
    })
  })
})

describe('a cell a spreadsheet would run', () => {
  it('is written with a quote before it, so it opens as text, and read back without it', () => {
    const items = [
      { kind: 'value' as const, value: '=HYPERLINK("http://x","y")' },
      { kind: 'value' as const, value: '+39 055 123' },
      { kind: 'value' as const, value: '-1' },
      { kind: 'value' as const, value: '@me' },
      { kind: 'value' as const, value: '\tindented' },
    ]
    const csv = allowedCsv(items)
    expect(csv.split('\r\n').slice(1, 5)).toEqual([`value,"'=HYPERLINK(""http://x"",""y"")"`, "value,'+39 055 123", "value,'-1", "value,'@me"])
    expect(allowedFromCsv(csv)).toEqual({ items, problems: [] })
    expect(termsFromCsv(termsCsv([term('=1+1')]))).toEqual({ terms: [term('=1+1')], problems: [] })
  })

  it("keeps a quote that is a value's own", () => {
    expect(allowedFromCsv("kind,value\nvalue,'quoted'\n").items).toEqual([{ kind: 'value', value: "'quoted'" }])
  })
})

describe('leftOutByExport', () => {
  it("names the recognizers the daemon's export left out, its examples holding values seen for real", () => {
    const warnings = [
      'recognizer badge: left out, its examples hold values Latet has seen for real',
      '2 terms are in the file in clear: send it the way you would send them',
      'recognizer client-code: left out, its examples hold values Latet has seen for real',
    ]
    expect(leftOutByExport(warnings)).toEqual(new Set(['badge', 'client-code']))
    expect(leftOutByExport([])).toEqual(new Set())
  })
})

describe('otherLists', () => {
  it('names the lists of a rules file besides its recognizers, in YAML or in JSON', () => {
    const yaml = ['# a colleague', 'latet_rules: 1', 'terms:', '  - value: Acme', '    type: CLIENT', 'recognizers:', '  - name: badge', '    pattern: B-[0-9]{6}', 'policies:', '  EMAIL: allow', ''].join('\n')
    expect(otherLists(yaml)).toEqual(['terms', 'policies'])
    expect(otherLists(rulesFile({ recognizers: [{ name: 'badge', pattern: 'x' }] }))).toEqual([])
    expect(otherLists(JSON.stringify({ latet_rules: 1, recognizers: [], allow_values: ['dev'] }))).toEqual(['allow_values'])
    expect(otherLists('recognizers:\n  - name: badge\n')).toEqual([])
  })
})
