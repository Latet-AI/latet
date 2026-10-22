import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { Me } from '../../api/types'
import i18n from '../../i18n'
import { MeContext, type MeState } from '../../lib/me'
import { computer, server } from '../../test/eop35-fixtures'
import { ListTransfer } from './transfer'

// A list's own Import and Export, as they render: a project's Import is not offered on a Latet server, which refuses it
// (your settings for a project are imported on your computer).

const buttons = (me: Me, project?: string) => {
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const html = renderToStaticMarkup(h(MeContext.Provider, { value }, h(ListTransfer, { kind: 'terms', rows: [], project, onImported: () => undefined })))
  return [...html.matchAll(/<button\b[^>]*>(.*?)<\/button>/g)].map((match) => (match[1] ?? '').replace(/<[^>]+>/g, '').trim())
}

describe("a list's Import", () => {
  it('is offered in the global rules and in a project on your computer', () => {
    expect(buttons(computer)).toContain(i18n.t('rules.import.button'))
    expect(buttons(computer, 'billing-app')).toContain(i18n.t('rules.import.button'))
    expect(buttons(server('person'))).toContain(i18n.t('rules.import.button'))
  })

  it('is not offered in a project on a Latet server, Export still is', () => {
    expect(buttons(server('person'), 'billing-app')).not.toContain(i18n.t('rules.import.button'))
    expect(buttons(server('person'), 'billing-app')).toContain(i18n.t('rules.export.button'))
  })
})
