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

  it('shows how new request members are read in your settings, the value in force chosen, and in no project layer', () => {
    for (const mode of ['check', 'refuse'] as const) {
      const html = renderToStaticMarkup(h(DataEditor, { value: { ...draft, unknown_members: mode }, set: () => undefined, layer: 'global' }))
      const group = html.slice(html.indexOf(`aria-label="${t('data.newMembers.title')}"`))
      expect(group).not.toBe(html)
      const chosen = /<input[^>]*value="(check|refuse)"[^>]*checked=""/.exec(group)?.[1] ?? /<input[^>]*checked=""[^>]*value="(check|refuse)"/.exec(group)?.[1]
      expect(chosen).toBe(mode)
      expect(group).toContain(t('data.newMembers.check'))
      expect(group).toContain(t('data.newMembers.refuse'))
    }
    expect(headings('global')).toContain(t('data.newMembers.title'))
    expect(headings('personal')).not.toContain(t('data.newMembers.title'))
    expect(headings('repo')).not.toContain(t('data.newMembers.title'))
  })

  it('offers refuse alone where the company locks it (Latet Enterprise)', () => {
    const html = renderToStaticMarkup(
      h(DataEditor, { value: { ...draft, unknown_members: 'refuse' }, set: () => undefined, layer: 'global', newMembersLocked: true }),
    )
    const group = html.slice(html.indexOf(`aria-label="${t('data.newMembers.title')}"`))
    expect(group).toContain('value="refuse"')
    expect(group).not.toContain('value="check"')
  })

  it('shows refuse chosen while the company locks it, even where your own settings say check (fix round 1, I-4)', () => {
    // GET /config answers your own value (what a save sends back); in use, the lock raises it to `refuse`.
    const html = renderToStaticMarkup(
      h(DataEditor, { value: { ...draft, unknown_members: 'check' }, set: () => undefined, layer: 'global', newMembersLocked: true }),
    )
    const group = html.slice(html.indexOf(`aria-label="${t('data.newMembers.title')}"`))
    expect(/<input[^>]*value="refuse"[^>]*checked=""|<input[^>]*checked=""[^>]*value="refuse"/.test(group)).toBe(true)
    expect(group).not.toContain('value="check"')
  })

  it('lets a repository file add files and web addresses, never choose the tools', () => {
    const shown = headings('repo')
    expect(shown).toContain(t('data.files.title'))
    expect(shown).toContain(t('data.hosts.title'))
    expect(shown).not.toContain(t('data.tools.title'))
  })
})
