import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import css from '../index.css?raw'
import { Segmented } from './ui'

/** Each choice as rendered: the classes of its label, its radio, and the words it shows or says. */
function choicesOf(html: string): { classes: string[]; radio: string; text: string; hiddenText: boolean }[] {
  return [...html.matchAll(/<label\b([^>]*)>(.*?)<\/label>/g)].map((match) => {
    const body = match[2] ?? ''
    return {
      classes: (/class="([^"]*)"/.exec(match[1] ?? '')?.[1] ?? '').split(/\s+/),
      radio: /<input\b[^>]*>/.exec(body)?.[0] ?? '',
      text: body.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
      hiddenText: /<span class="sr-only">/.test(body),
    }
  })
}

describe('Segmented', () => {
  const render = (size?: 'sm' | 'md') =>
    renderToStaticMarkup(
      h(Segmented<'chart' | 'table'>, {
        label: 'Show as',
        value: 'table',
        onChange: () => undefined,
        size,
        options: [
          { value: 'chart', label: 'Chart' },
          { value: 'table', label: 'Table', icon: 'stats', iconOnly: true },
        ],
      }),
    )

  it('is a radio group named by its label, drawn as the well of the look (.seg)', () => {
    const html = render()
    expect(html).toMatch(/^<div[^>]*role="radiogroup"[^>]*>/)
    const outer = html.slice(0, html.indexOf('>'))
    expect(outer).toMatch(/aria-label="Show as"/)
    expect(/class="([^"]*)"/.exec(outer)?.[1]?.split(/\s+/)).toContain('seg')
  })

  it('has one choice per option (.seg-item), each a radio of the group, the value chosen checked', () => {
    const choices = choicesOf(render())
    expect(choices).toHaveLength(2)
    for (const choice of choices) {
      expect(choice.classes).toContain('seg-item')
      expect(choice.radio).toMatch(/type="radio"/)
    }
    const names = choices.map((choice) => /name="([^"]*)"/.exec(choice.radio)?.[1])
    expect(names[0]).toBeTruthy()
    expect(names[1]).toBe(names[0])
    expect(choices.map((choice) => /\bchecked=""/.test(choice.radio))).toEqual([false, true])
  })

  it('keeps the words of a choice shown by its icon alone, for a screen reader', () => {
    const [chart, table] = choicesOf(render())
    expect(chart?.text).toBe('Chart')
    expect(chart?.hiddenText).toBe(false)
    expect(table?.text).toBe('Table')
    expect(table?.hiddenText).toBe(true)
  })

  it('is as tall as a button, or as a small one', () => {
    expect(choicesOf(render())[0]?.classes).toContain('h-[22px]') // 22 + 2 × 3 of the well + 2 × 1 of its line: 30, a .btn
    expect(choicesOf(render('sm'))[0]?.classes).toContain('h-[18px]') // 26, a .btn-sm
  })

  it('raises the chosen one by its radio, checked, and draws its focus ring', () => {
    expect(css).toMatch(/\.seg-item:has\(> input:checked\)/)
    expect(render()).toMatch(/peer-focus-visible:outline-2/)
  })
})
