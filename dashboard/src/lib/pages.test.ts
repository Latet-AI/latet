import { describe, expect, it } from 'vitest'
import { pageNumbers, pageRange } from './pages'

describe('pageNumbers', () => {
  it('shows every page when there are seven or fewer', () => {
    expect(pageNumbers(1, 1)).toEqual([1])
    expect(pageNumbers(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7])
  })

  it('shows the first, the last and two on each side of the current page, with a gap between', () => {
    expect(pageNumbers(6, 20)).toEqual([1, 'gap', 4, 5, 6, 7, 8, 'gap', 20])
  })

  it('shows a lone page in place of a gap of one', () => {
    expect(pageNumbers(4, 20)).toEqual([1, 2, 3, 4, 5, 6, 'gap', 20])
    expect(pageNumbers(5, 10)).toEqual([1, 2, 3, 4, 5, 6, 7, 'gap', 10])
  })

  it('stays within the first and the last page', () => {
    expect(pageNumbers(1, 20)).toEqual([1, 2, 3, 'gap', 20])
    expect(pageNumbers(20, 20)).toEqual([1, 'gap', 18, 19, 20])
  })
})

describe('pageRange', () => {
  it('gives the first and the last value a page shows', () => {
    expect(pageRange(2, 50, 1234)).toEqual({ from: 51, to: 100 })
    expect(pageRange(25, 50, 1234)).toEqual({ from: 1201, to: 1234 })
    expect(pageRange(1, 50, 0)).toEqual({ from: 0, to: 0 })
  })
})
