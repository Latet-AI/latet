// What a value needs to be masked as a term of yours, checked as the daemon checks it (has_letter_or_digit, TERM_LONGEST).

/** A letter or a digit, of any script. A fake keeps the signs of a value, so signs alone (`--`, `###`) would be their own fake. */
export function hasLetterOrDigit(value: string): boolean {
  return /[\p{L}\p{Nd}]/u.test(value)
}

/** The most characters a term holds (the daemon's TERM_LONGEST): a longer one would be matched on its own, and one of
 * about a million letters could not be matched at all, so the daemon refuses it. */
export const TERM_LONGEST = 8192

/** Whether a term holds more characters than the daemon keeps, counted as it counts them: a character outside the first
 * plane (an emoji) is one, where JavaScript counts its two halves. */
export function termTooLong(value: string): boolean {
  if (value.length <= TERM_LONGEST) return false // as many halves at most: as many characters at most
  const characters = value[Symbol.iterator]()
  let count = 0
  while (!characters.next().done) {
    count += 1
    if (count > TERM_LONGEST) return true
  }
  return false
}

/** Why a term cannot be saved: none typed, signs alone, longer than the daemon keeps, or a value the list holds already
 * with the same type (`others`: the other terms of the same list), as the daemon tells terms apart. */
export type TermProblem = 'empty' | 'noLetterOrDigit' | 'tooLong' | 'taken'

export function termProblem(term: { value: string; type: string }, others: readonly { value: string; type: string }[]): TermProblem | null {
  const text = term.value.trim()
  if (text === '') return 'empty'
  if (!hasLetterOrDigit(text)) return 'noLetterOrDigit'
  if (termTooLong(text)) return 'tooLong'
  const type = term.type.toUpperCase()
  if (others.some((other) => other.value === text && other.type.toUpperCase() === type)) return 'taken'
  return null
}
