import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NARROW_MAX, narrowQuery, narrowStore, useNarrow } from './useNarrow'

/** A stand-in for `window.matchMedia` over a window `width` px wide, which `resize` changes as a browser would. */
function fakeMatchMedia(width: number) {
  const lists: { max: number; listeners: Set<() => void> }[] = []
  const matchMedia = (query: string) => {
    const max = Number(/^\(max-width: (\d+)px\)$/.exec(query)?.[1] ?? NaN)
    const list = { max, listeners: new Set<() => void>() }
    lists.push(list)
    return {
      media: query,
      get matches() {
        return width <= list.max
      },
      addEventListener: (type: string, listener: () => void) => {
        if (type === 'change') list.listeners.add(listener)
      },
      removeEventListener: (type: string, listener: () => void) => {
        if (type === 'change') list.listeners.delete(listener)
      },
    } as unknown as MediaQueryList
  }
  const resize = (to: number) => {
    const before = lists.map((list) => width <= list.max)
    width = to
    lists.forEach((list, i) => {
      if (before[i] !== width <= list.max) for (const listener of list.listeners) listener()
    })
  }
  const listening = () => lists.reduce((count, list) => count + list.listeners.size, 0)
  return { matchMedia, resize, listening }
}

describe('a narrow window', () => {
  it('is 1099 px wide or less, as a media query', () => {
    expect(NARROW_MAX).toBe(1099)
    expect(narrowQuery(NARROW_MAX)).toBe('(max-width: 1099px)')
    expect(narrowQuery(767)).toBe('(max-width: 767px)')
  })

  it('reads the window as it is: narrow at 960 and 390 px, not at 1100 or 1440', () => {
    for (const [width, narrow] of [
      [390, true],
      [960, true],
      [1099, true],
      [1100, false],
      [1440, false],
    ] as const) {
      expect(narrowStore(fakeMatchMedia(width).matchMedia, NARROW_MAX).get(), `${width} px`).toBe(narrow)
    }
  })

  it('follows the window as it changes, until it is let go', () => {
    const media = fakeMatchMedia(1440)
    const store = narrowStore(media.matchMedia, NARROW_MAX)
    const changed = vi.fn()
    const stop = store.subscribe(changed)
    media.resize(960)
    expect(changed).toHaveBeenCalledTimes(1)
    expect(store.get()).toBe(true)
    media.resize(1000) // still narrow: nothing to say
    expect(changed).toHaveBeenCalledTimes(1)
    media.resize(1280)
    expect(changed).toHaveBeenCalledTimes(2)
    expect(store.get()).toBe(false)
    stop()
    expect(media.listening()).toBe(0)
    media.resize(390)
    expect(changed).toHaveBeenCalledTimes(2)
  })

  it('is wide where there is no window to ask (no matchMedia)', () => {
    const store = narrowStore(undefined, NARROW_MAX)
    expect(store.get()).toBe(false)
    expect(() => store.subscribe(() => undefined)()).not.toThrow()
  })
})

describe('useNarrow', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const Shows = ({ maxWidth }: { maxWidth?: number }) => h('p', null, String(useNarrow(maxWidth)))

  it('asks the window for 1099 px unless told another width', () => {
    vi.stubGlobal('window', { matchMedia: fakeMatchMedia(960).matchMedia })
    expect(renderToStaticMarkup(h(Shows))).toBe('<p>true</p>')
    expect(renderToStaticMarkup(h(Shows, { maxWidth: 767 }))).toBe('<p>false</p>')
    vi.stubGlobal('window', { matchMedia: fakeMatchMedia(1440).matchMedia })
    expect(renderToStaticMarkup(h(Shows))).toBe('<p>false</p>')
  })
})
