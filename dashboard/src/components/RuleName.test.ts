import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { RuleName, RuleScope } from './RuleName'

// Where the name of a rule of your own leads: the section of Rules it is edited in, in the tab that holds it.

function hrefOf(rule: string, layer: 'repo' | 'personal' | null, scoped = true): string | undefined {
  const name = h(RuleName, { rule })
  const html = renderToStaticMarkup(
    h(MemoryRouter, null, scoped ? h(RuleScope.Provider, { value: { projectId: 'a1b2c3', layer: () => layer } }, name) : name),
  )
  return /<a\b[^>]*href="([^"]*)"/.exec(html)?.[1]?.replace(/&amp;/g, '&')
}

describe('RuleName', () => {
  it('leads to the section of the global rules for a rule of theirs', () => {
    expect(hrefOf('recognizer:badge', null)).toBe('/rules#recognizers')
    expect(hrefOf('terms:client', null, false)).toBe('/rules#terms')
    expect(hrefOf('field:iban', null)).toBe('/rules#data')
  })

  it("leads to the section of your settings for the project, in its tab, for a rule of theirs", () => {
    expect(hrefOf('recognizer:badge', 'personal')).toBe('/rules?project=a1b2c3#project-recognizers')
    expect(hrefOf('terms:client', 'personal')).toBe('/rules?project=a1b2c3#project-terms')
    expect(hrefOf('field:iban', 'personal')).toBe('/rules?project=a1b2c3#project-data')
    expect(hrefOf('strict:iban', 'personal')).toBe('/rules?project=a1b2c3#project-data')
  })

  it("leads to its list's tab for a rule of the project's latet.yaml, where the file's rows show", () => {
    expect(hrefOf('recognizer:invoice-ref', 'repo')).toBe('/rules?project=a1b2c3#project-recognizers')
    expect(hrefOf('terms:project', 'repo')).toBe('/rules?project=a1b2c3#project-terms')
  })

  it("leads to the project's repository file for a field rule of its latet.yaml, which is no list of a table", () => {
    expect(hrefOf('field:note_*', 'repo')).toBe('/rules?project=a1b2c3#repo')
  })
})
