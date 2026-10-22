// Find surrogates in the model's answer and the matching originals in what the agent received.
//
// The contract gives the response as two texts (`text_masked`, `text_restored`) without offsets.
// The request's replacements tell us which surrogates exist; restoring only swaps each surrogate
// for its original, so the text between surrogates is identical on both sides and anchors the
// alignment. All offsets here are UTF-16 (computed in JavaScript).

import type { Range } from './spans'

export interface KnownValue<T> {
  surrogate: string
  /** The original when known: used first to delimit the restored value. */
  original: string | null
  info: T
}

export interface Found<T> extends Range {
  info: T
}

export interface Alignment<T> {
  masked: Found<T>[]
  /** When aligned, one range per entry of `masked`, in the same order (possibly empty). */
  restored: Found<T>[]
  /** False when the restored text could not be aligned (its ranges are then empty). */
  aligned: boolean
}

const MIN_SURROGATE = 3

/** Every non-overlapping occurrence of a known surrogate, longest surrogates first. */
export function findSurrogates<T>(text: string, known: readonly KnownValue<T>[]): Found<T>[] {
  const unique = new Map<string, KnownValue<T>>()
  for (const value of known) {
    if (value.surrogate.length >= MIN_SURROGATE && !unique.has(value.surrogate)) unique.set(value.surrogate, value)
  }
  const ordered = [...unique.values()].sort((a, b) => b.surrogate.length - a.surrogate.length)
  const taken: Found<T>[] = []
  const overlaps = (start: number, end: number) => taken.some((f) => start < f.end && end > f.start)
  for (const value of ordered) {
    let from = 0
    for (;;) {
      const start = text.indexOf(value.surrogate, from)
      if (start === -1) break
      const end = start + value.surrogate.length
      if (!overlaps(start, end)) taken.push({ start, end, info: value.info })
      from = start + 1
    }
  }
  return taken.sort((a, b) => a.start - b.start)
}

/**
 * Align `restored` against `masked`: returns where each surrogate sits in `masked` and where the
 * value that replaced it sits in `restored`.
 */
export function alignRestored<T>(
  masked: string,
  restored: string,
  known: readonly KnownValue<T>[],
): Alignment<T> {
  const found = findSurrogates(masked, known)
  if (found.length === 0) return { masked: [], restored: [], aligned: masked === restored }
  const byInfo = new Map<T, KnownValue<T>>()
  for (const value of known) if (!byInfo.has(value.info)) byInfo.set(value.info, value)

  const restoredRanges: Found<T>[] = []
  let maskedPos = 0
  let pos = 0
  for (let i = 0; i < found.length; i++) {
    const current = found[i]!
    const plain = masked.slice(maskedPos, current.start)
    if (!restored.startsWith(plain, pos)) return { masked: found, restored: [], aligned: false }
    pos += plain.length
    const nextStart = i + 1 < found.length ? found[i + 1]!.start : masked.length
    const nextPlain = masked.slice(current.end, nextStart)
    const isLast = i + 1 === found.length

    // Candidates for the restored value: the known original, then the surrogate left as is.
    const value = byInfo.get(current.info)
    let end = -1
    for (const candidate of [value?.original, masked.slice(current.start, current.end)]) {
      if (candidate == null || candidate === '') continue
      if (!restored.startsWith(candidate, pos)) continue
      const after = pos + candidate.length
      if (isLast ? restored.slice(after) === nextPlain : restored.startsWith(nextPlain, after)) {
        end = after
        break
      }
    }
    if (end === -1) {
      // Surrogates keep the shape of the original: try a value of the same length first.
      const sameLength = pos + (current.end - current.start)
      const fits = isLast ? restored.slice(sameLength) === nextPlain : restored.startsWith(nextPlain, sameLength)
      if (sameLength <= restored.length && fits) end = sameLength
    }
    if (end === -1) {
      // Unknown original: the restored value runs up to the next unchanged text.
      if (isLast) {
        end = nextPlain === '' ? restored.length : restored.length - nextPlain.length
        if (end < pos || !restored.endsWith(nextPlain)) return { masked: found, restored: [], aligned: false }
      } else if (nextPlain === '') {
        return { masked: found, restored: [], aligned: false }
      } else {
        end = restored.indexOf(nextPlain, pos + 1)
        if (end === -1) return { masked: found, restored: [], aligned: false }
      }
    }
    restoredRanges.push({ start: pos, end, info: current.info })
    pos = end
    maskedPos = current.end
  }
  if (restored.slice(pos) !== masked.slice(maskedPos)) return { masked: found, restored: [], aligned: false }
  return { masked: found, restored: restoredRanges, aligned: true }
}
