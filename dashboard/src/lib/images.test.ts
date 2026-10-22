import { describe, expect, it } from 'vitest'
import type { LiveImage } from '../api/types'
import { approveBody } from './approvals'
import { choiceFor, imageChanges, imagesBody, rectBetween, textRects, toImage, usable } from './images'

const IMAGE: LiveImage = {
  width: 400,
  height: 100,
  media_type: 'image/png',
  // "Email: mario@acme.it" then "VIP"; 10 pixels a character from x = 20.
  lines: [
    { box: [20, 10, 220, 30], start: 0, end: 20 },
    { box: [20, 50, 50, 70], start: 21, end: 24 },
  ],
  covered: [],
  rects: [],
  withheld: false,
}

describe('rectangles drawn on an image', () => {
  it('go from either corner, stay inside the image, in whole pixels', () => {
    expect(rectBetween([120.4, 80.6], [30.2, 10.1], 400, 100)).toEqual([30, 10, 120, 81])
    expect(rectBetween([-10, -5], [500, 300], 400, 100)).toEqual([0, 0, 400, 100])
  })

  it('cover something only when 4 pixels or more each way', () => {
    expect(usable([0, 0, 4, 4])).toBe(true)
    expect(usable([0, 0, 3, 40])).toBe(false)
  })

  it('take a point of the screen to the image pixels it shows', () => {
    const box = { left: 100, top: 50, width: 200, height: 50 } as DOMRect
    expect(toImage(200, 75, box, 400, 100)).toEqual([200, 50])
  })
})

describe('text read in an image', () => {
  it('is located on its line, one rectangle per line it touches', () => {
    expect(textRects(IMAGE, 7, 20)).toEqual([[90, 10, 220, 30]])
    expect(textRects(IMAGE, 18, 24)).toEqual([
      [200, 10, 220, 30],
      [20, 50, 50, 70],
    ])
    expect(textRects(IMAGE, 20, 21)).toEqual([]) // the line break
  })
})

describe('your decisions on images', () => {
  it('start from what you decided on the image before', () => {
    const before = { ...IMAGE, rects: [[0, 0, 10, 10]] as [number, number, number, number][] }
    expect(choiceFor(before, undefined)).toEqual({ rects: [[0, 0, 10, 10]], withhold: false })
    expect(choiceFor(before, { rects: [], withhold: true })).toEqual({ rects: [], withhold: true })
  })

  it('send each image you touched as you left it: no rectangles clears those drawn before', () => {
    const choices = { 0: { rects: [[1, 2, 30, 40]] as [number, number, number, number][], withhold: false }, 2: { rects: [], withhold: false }, 3: { rects: [[1, 2, 30, 40]] as [number, number, number, number][], withhold: true } }
    expect(imagesBody(choices)).toEqual([
      { item: 0, rects: [[1, 2, 30, 40]] },
      { item: 2, rects: [] },
      { item: 3, withhold: true },
    ])
    expect(imageChanges(choices)).toEqual({ rects: 1, withheld: 1 })
  })

  it('go with the approval, next to the values you masked', () => {
    expect(approveBody([], {})).toEqual({ action: 'approve' })
    expect(approveBody([], { 1: { rects: [], withhold: true } })).toEqual({ action: 'approve', images: [{ item: 1, withhold: true }] })
  })
})
