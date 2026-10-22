import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import { DataEditor, type DataDraft, type DataLayer } from './rules'

const draft: DataDraft = {
  data: 'strict',
  data_tools: [],
  data_files: ['*.json'],
  data_hosts: [],
  fields: [],
  dump_guard: null,
  images: null,
}

/** The headings of the production data editor in the layer `layer`, in order. */
function headings(layer: DataLayer): string[] {
  const html = renderToStaticMarkup(h(DataEditor, { value: draft, set: () => undefined, layer }))
  return [...html.matchAll(/<h3[^>]*>(.*?)<\/h3>/g)].map((match) => match[1] ?? '')
}

describe('DataEditor', () => {
  const t = i18n.t.bind(i18n)

  it('names the files, the web addresses, and the tools and MCP servers that count as data, in that order', () => {
    const shown = headings('global')
    const lists = [t('data.files.title'), t('data.hosts.title'), t('data.tools.title')]
    expect(shown.filter((heading) => lists.includes(heading))).toEqual(lists)
    expect(headings('personal').filter((heading) => lists.includes(heading))).toEqual(lists)
  })

  it('lets a repository file add files and web addresses, never choose the tools', () => {
    const shown = headings('repo')
    expect(shown).toContain(t('data.files.title'))
    expect(shown).toContain(t('data.hosts.title'))
    expect(shown).not.toContain(t('data.tools.title'))
  })
})
