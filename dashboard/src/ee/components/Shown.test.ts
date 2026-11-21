import { createElement as h, useState } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { connectShown, NO_PANELS, panelsReducer, panelsView, ShownOnce, usePanels, type PanelAction, type Panels, type Shown } from './Shown'

const code: Shown = { title: 'Code for Tom Becker', lines: [{ label: 'Code', text: 'K7QX-M2PV-9WDT' }] }
const key: Shown = { title: 'Key made: Billing CI', lines: [{ label: 'Key', text: 'lk_live_3f9a1c7e2b4d' }] }
const note = 'One SCIM token was made by someone who no longer administers, and it still works.'

const after = (...actions: PanelAction[]): Panels => actions.reduce(panelsReducer, NO_PANELS)
const titles = (panels: Panels) => panels.secrets.map((held) => held.shown.title)

describe('what a page keeps on screen from its changes', () => {
  it('keeps a secret shown until its own Close, whatever else the page says meanwhile', () => {
    const shown = after({ kind: 'secret', shown: code }, { kind: 'note', text: note })
    expect(titles(shown)).toEqual([code.title]) // a note is not put in its place
    expect(shown.note).toBe(note)
    const again = panelsReducer(shown, { kind: 'note', text: `${note} (again)` })
    expect(titles(again)).toEqual([code.title]) // nor a second note
    expect(titles(panelsReducer(again, { kind: 'close-note' }))).toEqual([code.title]) // nor the note's Close
  })

  it('adds a second secret beside the first, never in its place, and each Close takes away its own', () => {
    const both = after({ kind: 'secret', shown: code }, { kind: 'secret', shown: key })
    expect(titles(both)).toEqual([code.title, key.title])
    const [first, second] = both.secrets
    expect(titles(panelsReducer(both, { kind: 'close', id: first?.id ?? -1 }))).toEqual([key.title])
    expect(titles(panelsReducer(both, { kind: 'close', id: second?.id ?? -1 }))).toEqual([code.title])
    expect(first?.id).not.toBe(second?.id)
    const closed = after({ kind: 'secret', shown: code }, { kind: 'close', id: 0 }, { kind: 'secret', shown: key })
    expect(closed.secrets.map((held) => held.id)).toEqual([1]) // an id is never used twice
  })

  it("puts a page's secrets and its note on screen as panels of their own", () => {
    // The page's hook as it renders after: a code, a key, then a note (made in the first pass, as a page's changes are).
    const Page = () => {
      const { show, note: say, panels } = usePanels()
      const [made, setMade] = useState(false)
      if (!made) {
        setMade(true)
        show(code)
        show(key)
        say(note)
      }
      return panels
    }
    const html = renderToStaticMarkup(h(Page))
    expect(html.split('role="status"').length - 1).toBe(3)
    for (const words of ['K7QX-M2PV-9WDT', 'lk_live_3f9a1c7e2b4d', 'who no longer administers']) expect(html).toContain(words)
    expect(html.indexOf('K7QX-M2PV-9WDT')).toBeLessThan(html.indexOf('lk_live_3f9a1c7e2b4d')) // in the order they were made
    expect(html.indexOf('lk_live_3f9a1c7e2b4d')).toBeLessThan(html.indexOf('who no longer administers')) // the note after them
    expect(renderToStaticMarkup(h(ShownOnce, { shown: { title: note, lines: [] }, onClose: () => undefined }))).not.toContain('<code')
  })
})

describe('what the page puts on screen from the panels it holds', () => {
  const screen = (panels: Panels) => renderToStaticMarkup(h('div', null, panelsView(panels, { close: () => undefined, closeNote: () => undefined })))

  it('shows each secret and the note, and nothing once the page holds none', () => {
    expect(screen(NO_PANELS)).toBe('<div></div>')
    const html = screen(after({ kind: 'secret', shown: code }, { kind: 'secret', shown: key }, { kind: 'note', text: note }))
    for (const words of ['K7QX-M2PV-9WDT', 'lk_live_3f9a1c7e2b4d', 'who no longer administers']) expect(html).toContain(words)
  })

  it("takes a secret off the screen with its own Close, and leaves the others and the note", () => {
    const held = after({ kind: 'secret', shown: code }, { kind: 'secret', shown: key }, { kind: 'note', text: note })
    const first = held.secrets[0]?.id ?? -1
    const html = screen(panelsReducer(held, { kind: 'close', id: first }))
    expect(html).not.toContain('K7QX-M2PV-9WDT') // gone from the page, not only hidden
    expect(html).not.toContain(code.title)
    expect(html).toContain('lk_live_3f9a1c7e2b4d')
    expect(html).toContain('who no longer administers')
  })

  it('counts the secrets on screen, so that a page offers to make another thing once the last is closed', () => {
    // The hook as the page renders it: nothing shown yet, then (in the first pass, as the changes of a page are) a code.
    const Page = ({ make }: { make: boolean }) => {
      const { show, held, panels } = usePanels()
      const [made, setMade] = useState(false)
      if (make && !made) {
        setMade(true)
        show(code)
      }
      return h('div', null, h('span', { 'data-held': held }), panels)
    }
    expect(renderToStaticMarkup(h(Page, { make: false }))).toContain('data-held="0"')
    expect(renderToStaticMarkup(h(Page, { make: true }))).toContain('data-held="1"')
  })

  it('takes the last secret off the screen too, and a note closed leaves no secret and no panel', () => {
    const closed = after({ kind: 'secret', shown: code }, { kind: 'close', id: 0 })
    expect(screen(closed)).toBe('<div></div>')
    expect(screen(after({ kind: 'note', text: note }, { kind: 'close-note' }))).toBe('<div></div>')
  })
})

describe('the panel that shows a code to connect a computer', () => {
  it('gives the command first, to run, and the code it asks for, to copy', () => {
    const shown = connectShown('Run this command on the computer', { command: 'latet connect https://latet.acme.lan', code: 'K7QX-M2PV-9WDT' }, { command: 'Command', code: 'Code' })
    expect(shown).toEqual({
      title: 'Run this command on the computer',
      lines: [
        { label: 'Command', text: 'latet connect https://latet.acme.lan', command: true },
        { label: 'Code', text: 'K7QX-M2PV-9WDT' },
      ],
    })
    // The code is a secret line (no command prompt before it), and is not part of the command: it is not in a shell's history.
    expect(shown.lines[0]?.text).not.toContain('K7QX')
    expect(shown.lines[1]?.command).toBeUndefined()
  })
})
