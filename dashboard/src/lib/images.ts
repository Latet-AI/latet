// Images in the Live Reviewer: rectangles you draw on them, in the image's own pixels, and what you
// decided for each image of a batch.

import type { ApprovalImage, LiveImage } from '../api/types'

export type Rect = [number, number, number, number] // x0, y0, x1, y1
export const MIN_RECT = 4 // pixels: the daemon refuses a thinner rectangle
export const MAX_RECTS = 100

export interface ImageChoice {
  rects: Rect[]
  withhold: boolean
}

/** The rectangle between two points, inside the image, in whole pixels. */
export function rectBetween(a: [number, number], b: [number, number], width: number, height: number): Rect {
  const clamp = (v: number, max: number) => Math.min(Math.max(Math.round(v), 0), max)
  const x0 = clamp(Math.min(a[0], b[0]), width)
  const y0 = clamp(Math.min(a[1], b[1]), height)
  const x1 = clamp(Math.max(a[0], b[0]), width)
  const y1 = clamp(Math.max(a[1], b[1]), height)
  return [x0, y0, x1, y1]
}

/** Big enough to cover something, as the daemon wants it. */
export function usable(rect: Rect): boolean {
  return rect[2] - rect[0] >= MIN_RECT && rect[3] - rect[1] >= MIN_RECT
}

/** A point of the screen, in the image's pixels (the image is shown scaled to `box`). */
export function toImage(clientX: number, clientY: number, box: DOMRect, width: number, height: number): [number, number] {
  return [((clientX - box.left) / box.width) * width, ((clientY - box.top) / box.height) * height]
}

/**
 * Where a stretch of the text read in an image is (code-point offsets, as the Live Reviewer's texts
 * have them), one rectangle for each line it touches. The characters of a line are taken as equally
 * wide: good enough to show where a word you mask will be covered.
 */
export function textRects(image: LiveImage, start: number, end: number): Rect[] {
  const out: Rect[] = []
  for (const line of image.lines) {
    const a = Math.max(start, line.start)
    const b = Math.min(end, line.end)
    if (a >= b) continue
    const [x0, y0, x1, y1] = line.box
    const per = (x1 - x0) / Math.max(1, line.end - line.start)
    out.push([Math.floor(x0 + (a - line.start) * per), y0, Math.ceil(x0 + (b - line.start) * per), y1])
  }
  return out
}

/** What you see for an image: your choice, or what you decided on it before (it is applied again). */
export function choiceFor(image: LiveImage, choice: ImageChoice | undefined): ImageChoice {
  return choice ?? { rects: image.rects, withhold: image.withheld }
}

/** Your decisions on a batch's images, as the daemon takes them: each image you touched, as you left it
 * (no rectangles any more clears those you drew on it before). */
export function imagesBody(choices: Readonly<Record<number, ImageChoice>>): ApprovalImage[] {
  return Object.entries(choices).map(([item, choice]) =>
    choice.withhold ? { item: Number(item), withhold: true } : { item: Number(item), rects: choice.rects },
  )
}

/** How many rectangles you drew and images you withheld: what approving changes in the images. */
export function imageChanges(choices: Readonly<Record<number, ImageChoice>>): { rects: number; withheld: number } {
  const all = Object.values(choices)
  return { rects: all.reduce((n, c) => n + (c.withhold ? 0 : c.rects.length), 0), withheld: all.filter((c) => c.withhold).length }
}
