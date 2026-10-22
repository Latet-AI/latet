import { describe, expect, it } from 'vitest'
import css from './index.css?raw'

type Tokens = Record<string, string>
type Rgb = [number, number, number]
type Matrix = [Rgb, Rgb, Rgb]

/** The custom properties of the rule whose selector is exactly `selector`. */
function block(selector: string): Tokens {
  const start = css.indexOf(`${selector} {`)
  if (start < 0) throw new Error(`no rule ${selector} in index.css`)
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start))
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1] ?? '', (m[2] ?? '').trim()]))
}

const THEMES = { light: block(':root'), dark: block(":root[data-theme='dark']") }
const CLASSES = ['c-secret', 'c-pii', 'c-identity', 'c-custom']
const BACKGROUNDS = ['canvas', 'surface', 'raised', 'well']

function rgb(tokens: Tokens, name: string): Rgb {
  const digits = /^#([0-9a-f]{6})$/i.exec(tokens[name] ?? '')?.[1]
  if (!digits) throw new Error(`--${name} is not a #rrggbb colour: ${tokens[name]}`)
  const channel = (i: number) => parseInt(digits.slice(i, i + 2), 16) / 255
  return [channel(0), channel(2), channel(4)]
}

const linear = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)

function luminance([r, g, b]: Rgb): number {
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

/** WCAG contrast ratio. */
function contrast(a: Rgb, b: Rgb): number {
  const [x, y] = [luminance(a), luminance(b)]
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

/** A translucent token (`rgb(r g b / a)`, the washes) over a background. */
function tinted(tokens: Tokens, name: string, bg: Rgb): Rgb {
  const parts = /^rgb\((\d+) (\d+) (\d+) \/ ([\d.]+)\)$/.exec(tokens[name] ?? '')
  if (!parts) throw new Error(`--${name} is not an rgb(r g b / a) colour: ${tokens[name]}`)
  const n = (i: number) => Number(parts[i])
  return over([n(1) / 255, n(2) / 255, n(3) / 255], n(4), bg)
}

/** A colour at `alpha` over a background, as the browser composites `bg-x/10` and the masks' tints. */
function over(fg: Rgb, alpha: number, bg: Rgb): Rgb {
  const mix = (f: number, b: number) => f * alpha + b * (1 - alpha)
  return [mix(fg[0], bg[0]), mix(fg[1], bg[1]), mix(fg[2], bg[2])]
}

// Machado, Oliveira and Fernandes (2009), severity 1, on linear RGB.
const PROTAN: Matrix = [
  [0.152286, 1.052583, -0.204868],
  [0.114503, 0.786281, 0.099216],
  [-0.003882, -0.048116, 1.051998],
]
const DEUTAN: Matrix = [
  [0.367322, 0.860646, -0.227968],
  [0.280085, 0.672501, 0.047413],
  [-0.01182, 0.04294, 0.968881],
]

function oklab(c: Rgb, vision?: Matrix): Rgb {
  let [r, g, b] = [linear(c[0]), linear(c[1]), linear(c[2])]
  if (vision) {
    const see = (row: Rgb) => Math.min(1, Math.max(0, row[0] * r + row[1] * g + row[2] * b))
    ;[r, g, b] = [see(vision[0]), see(vision[1]), see(vision[2])]
  }
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

/** How far apart two colours look (OKLab distance), to a person with the given vision. */
function apart(a: Rgb, b: Rgb, vision?: Matrix): number {
  const [x, y] = [oklab(a, vision), oklab(b, vision)]
  return Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2])
}

/** Text on its background: [text, background, floor]. */
const TEXT: [string, string, number][] = [
  ...BACKGROUNDS.map((bg): [string, string, number] => ['ink', bg, 7]),
  ['surface', 'ink', 7], // the redaction marker, the primary button
  ...['ink-2', 'ink-3', 'brand-ink', 'good-ink', 'warn-ink', 'crit-ink'].flatMap((fg) =>
    BACKGROUNDS.map((bg): [string, string, number] => [fg, bg, 4.5]),
  ),
  ['nav-ink', 'nav', 4.5],
]

/** Marks and borders that carry meaning, on what they sit on: 3:1. */
const MARKS: [string, string][] = [
  ...['brand', 'focus', 'field', 'good', 'warn', 'crit', 'data'].flatMap((fg) =>
    ['canvas', 'surface'].map((bg): [string, string] => [fg, bg]),
  ),
  ['focus', 'raised'],
  ['focus', 'well'],
  ['field', 'raised'],
  ['brand', 'nav'],
  ...CLASSES.flatMap((c): [string, string][] => [
    [c, 'well'],
    [c, 'surface'],
  ]),
]

describe.each(Object.entries(THEMES))('the %s theme', (_name, tokens) => {
  it.each(TEXT)('%s text reads on %s', (fg, bg, floor) => {
    expect(contrast(rgb(tokens, fg), rgb(tokens, bg))).toBeGreaterThanOrEqual(floor)
  })

  it.each(MARKS)('%s shows on %s', (fg, bg) => {
    expect(contrast(rgb(tokens, fg), rgb(tokens, bg))).toBeGreaterThanOrEqual(3)
  })

  it.each(['ink-2', 'ink-3'])('%s text reads on a selected row', (fg) => {
    const selected = tinted(tokens, 'wash-2', rgb(tokens, 'surface'))
    expect(contrast(rgb(tokens, fg), selected)).toBeGreaterThanOrEqual(4.5)
  })

  it.each(['brand', 'good', 'warn', 'crit'])('ink reads on a callout tinted %s', (tint) => {
    const surface = rgb(tokens, 'surface')
    expect(contrast(rgb(tokens, 'ink'), over(rgb(tokens, tint), 0.1, surface))).toBeGreaterThanOrEqual(7)
  })

  it.each([...CLASSES, 'brand'])('a value masked as %s reads, even highlighted', (tone) => {
    const well = rgb(tokens, 'well')
    expect(contrast(rgb(tokens, 'ink'), over(rgb(tokens, tone), 0.3, well))).toBeGreaterThanOrEqual(4.5)
  })

  it('the four classes stay apart for every vision, protanopia and deuteranopia included', () => {
    for (const [i, a] of CLASSES.entries()) {
      for (const b of CLASSES.slice(i + 1)) {
        for (const vision of [undefined, PROTAN, DEUTAN]) {
          expect(apart(rgb(tokens, a), rgb(tokens, b), vision), `${a} / ${b}`).toBeGreaterThanOrEqual(0.1)
        }
      }
    }
  })

  it.each(['good', 'warn', 'crit', ...CLASSES])('the brand does not look like %s', (other) => {
    expect(apart(rgb(tokens, 'brand'), rgb(tokens, other))).toBeGreaterThanOrEqual(0.1)
  })

  // Under protanopia a dark green and a red come closer (0.063 for crit in the light theme): what the brand draws
  // differs from the classes' marks by its shape (a box, a dotted line, the line beside the current item), and a
  // status colour always comes with an icon and a label. This floor keeps the brand from coming any closer.
  it.each(['good', 'warn', 'crit', ...CLASSES])('the brand stays apart from %s under both simulations', (other) => {
    for (const vision of [PROTAN, DEUTAN]) {
      expect(apart(rgb(tokens, 'brand'), rgb(tokens, other), vision)).toBeGreaterThanOrEqual(0.06)
    }
  })
})

describe('the palette', () => {
  it('is the same dark theme whether the system or the toggle asks for it', () => {
    expect(block(":root:not([data-theme='light'])")).toEqual(THEMES.dark)
  })

  it('Linea keeps the site’s accent', () => {
    expect([THEMES.light.brand, THEMES.dark.brand]).toEqual(['#0b7a4b', '#2ee59d'])
  })

  // The two tones of Latet: a value as it is on your machine (real, warm) and the one that leaves (fake, green).
  it('the real and fake tones read on their washes, in both themes', () => {
    for (const [name, tokens] of Object.entries(THEMES)) {
      expect(contrast(rgb(tokens, 'real-ink'), rgb(tokens, 'real-low')), `${name}: real-ink on real-low`).toBeGreaterThanOrEqual(4.5)
      expect(contrast(rgb(tokens, 'fake-ink'), rgb(tokens, 'fake-low')), `${name}: fake-ink on fake-low`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('the real and fake tones show as marks on the canvas and the surface, in both themes', () => {
    for (const [name, tokens] of Object.entries(THEMES)) {
      for (const tone of ['real', 'fake']) {
        for (const bg of ['canvas', 'surface']) {
          expect(contrast(rgb(tokens, tone), rgb(tokens, bg)), `${name}: ${tone} on ${bg}`).toBeGreaterThanOrEqual(3)
        }
      }
    }
  })
})

describe('the type', () => {
  const theme = block('@theme')

  it('sets the interface in Inter, titles and figures in Montserrat, data in JetBrains Mono', () => {
    expect(theme['font-sans']).toMatch(/^'Inter Variable',/)
    expect(theme['font-display']).toMatch(/^Montserrat,/)
    expect(theme['font-mono']).toMatch(/^'JetBrains Mono',/)
  })
})
