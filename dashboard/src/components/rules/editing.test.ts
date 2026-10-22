import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import i18n from '../../i18n'
import { MeContext, type MeState } from '../../lib/me'
import { RepoNotWritable } from '../../lib/panelSave'
import { computer } from '../../test/eop35-fixtures'
import { SaveRefusal } from './editing'

// Why a panel's save was refused, in the reader's language.

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/\s+/g, ' ').trim()

describe('SaveRefusal', () => {
  it("says in the reader's language that a project's latet.yaml cannot be written here", () => {
    const me: MeState = { me: computer, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
    for (const lng of ['en', 'it']) {
      void i18n.changeLanguage(lng)
      const html = renderToStaticMarkup(h(MeContext.Provider, { value: me }, h(SaveRefusal, { error: new RepoNotWritable() })))
      expect(text(html)).toBe(i18n.t('projects.repo.readOnly'))
    }
    void i18n.changeLanguage('en')
  })
})
