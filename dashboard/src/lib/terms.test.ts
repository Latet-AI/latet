import { describe, expect, it } from 'vitest'
import { hasLetterOrDigit, TERM_LONGEST, termProblem, termTooLong } from './terms'

describe('hasLetterOrDigit', () => {
  it('takes a letter or a digit of any script', () => {
    for (const value of ['ab', 'a--b', '42', '#ops', 'Zoë', '١٢٣']) expect(hasLetterOrDigit(value)).toBe(true)
  })

  it('refuses signs alone, as the daemon does: their fake would be themselves', () => {
    for (const value of ['--', '###', '~~~~', '-- --', '+', '__', '²', ' ']) expect(hasLetterOrDigit(value)).toBe(false)
  })
})

describe('termTooLong', () => {
  it('refuses a term longer than the daemon keeps, as it does', () => {
    expect(TERM_LONGEST).toBe(8192)
    expect(termTooLong('Acme Corp')).toBe(false)
    expect(termTooLong('x'.repeat(TERM_LONGEST))).toBe(false)
    expect(termTooLong('x'.repeat(TERM_LONGEST + 1))).toBe(true)
  })

  it('counts characters as the daemon does, a character outside the first plane as one', () => {
    const emoji = '\u{1F600}' // two halves in JavaScript, one character for the daemon
    expect(termTooLong(emoji.repeat(TERM_LONGEST))).toBe(false)
    expect(termTooLong(emoji.repeat(TERM_LONGEST + 1))).toBe(true)
  })
})

describe('termProblem', () => {
  const of = (value: string, type = 'PROJECT') => ({ value, type })

  it('says why a term cannot be saved: empty, signs alone, too long, or a value the list holds already of its type', () => {
    expect(termProblem(of(''), [])).toBe('empty')
    expect(termProblem(of('   '), [])).toBe('empty')
    expect(termProblem(of('--'), [])).toBe('noLetterOrDigit')
    expect(termProblem(of('x'.repeat(TERM_LONGEST + 1)), [])).toBe('tooLong')
    expect(termProblem(of('Project Aurora'), [of('Whitfield & Co', 'CLIENT'), of('Project Aurora')])).toBe('taken')
    expect(termProblem(of(' Project Aurora '), [of('Project Aurora')])).toBe('taken')
    expect(termProblem(of('Project Aurora North'), [of('Project Aurora')])).toBeNull()
  })

  it('takes a value the list holds with another type, as the daemon does (a client list imported over a term of yours)', () => {
    expect(termProblem(of('Acme', 'CLIENT'), [of('Acme', 'CUSTOM')])).toBeNull()
    expect(termProblem(of('Acme', 'client'), [of('Acme', 'CLIENT')])).toBe('taken')
  })
})
