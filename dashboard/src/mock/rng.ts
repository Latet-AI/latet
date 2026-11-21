// Deterministic fake values for the mock mode. Secrets are generated at run time from a seed, so no
// credential-shaped literal appears in the source (secret scanners stay quiet) and reloads look the same.

export type Rng = () => number

export function seeded(seed: number): Rng {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function hashSeed(text: string): number {
  let h = 2166136261
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export const ALNUM = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
export const UPPER_NUM = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
export const LOWER_HEX = '0123456789abcdef'
export const URLSAFE = ALNUM + '-_'
export const B64 = ALNUM + '+/'

export function chars(rng: Rng, alphabet: string, length: number): string {
  let out = ''
  for (let i = 0; i < length; i++) out += alphabet[Math.floor(rng() * alphabet.length)]
  return out
}

/** A typed token: public prefix plus random body ("ghp_" + 36 characters). */
export function token(rng: Rng, prefix: string, length: number, alphabet = ALNUM, suffix = ''): string {
  return prefix + chars(rng, alphabet, length) + suffix
}

/** Same length and character classes, separators kept: what the engine's surrogates look like (a secret's: `secretShape`). */
export function shape(rng: Rng, value: string, keep = 0): string {
  let out = value.slice(0, keep)
  for (const c of value.slice(keep)) {
    if (c >= 'A' && c <= 'Z') out += chars(rng, UPPER, 1)
    else if (c >= 'a' && c <= 'z') out += chars(rng, LOWER, 1)
    else if (c >= '0' && c <= '9') out += chars(rng, DIGITS, 1)
    else out += c
  }
  return out
}

const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
const LOWER = 'abcdefghijklmnopqrstuvwxyz'
const DIGITS = '0123456789'
const UPPER_HEX = '0123456789ABCDEF'

/**
 * What a run of a secret's fake is drawn from at every place, as the engine draws it: one case's hexadecimal digits
 * where the run holds nothing else, else every kind of character it holds ('' for a run of signs).
 */
function runAlphabet(run: string): string {
  if (/[a-fA-F]/.test(run) && /^[0-9a-f]+$/.test(run)) return LOWER_HEX
  if (/[a-fA-F]/.test(run) && /^[0-9A-F]+$/.test(run)) return UPPER_HEX
  return (/[0-9]/.test(run) ? DIGITS : '') + (/[a-z]/.test(run) ? LOWER : '') + (/[A-Z]/.test(run) ? UPPER : '')
}

/**
 * A secret's fake, as the engine draws it (`secret_shape`): the first `keep` characters (a vendor's public prefix) and
 * the signs kept, each run of letters and digits between signs drawn at every place from the kinds of characters it
 * holds, never a digit for a digit and a capital for a capital. With `first`, the first `keep` characters are drawn
 * from it instead of kept (a fake that keeps none of them, the rest drawn from `rng` as before).
 */
export function secretShape(rng: Rng, value: string, keep = 0, first?: Rng): string {
  let out = first ? '' : value.slice(0, keep)
  let at = out.length
  for (const run of value.slice(at).match(/[A-Za-z0-9]+|[^A-Za-z0-9]+/g) ?? []) {
    const alphabet = runAlphabet(run)
    for (const c of run) {
      out += alphabet ? chars(first && at < keep ? first : rng, alphabet, 1) : c
      at++
    }
  }
  return out
}

export function pick<T>(rng: Rng, items: readonly T[]): T {
  return items[Math.floor(rng() * items.length)] as T
}

export function between(rng: Rng, min: number, max: number): number {
  return Math.round(min + rng() * (max - min))
}

/** Base64url of a JSON value (for JWT-shaped fakes). */
export function b64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
}

export function hexId(rng: Rng, length = 20): string {
  return chars(rng, LOWER_HEX, length)
}

const PEM_DASHES = '-----'

/** A PEM block with a random body (64-character lines). */
export function pem(rng: Rng, label: string, lines = 6): { header: string; body: string; footer: string } {
  const body = Array.from({ length: lines }, (_, i) => chars(rng, B64, i === lines - 1 ? 24 : 64)).join('\n')
  return {
    header: `${PEM_DASHES}BEGIN ${label}${PEM_DASHES}`,
    body: `\n${body}\n`,
    footer: `${PEM_DASHES}END ${label}${PEM_DASHES}`,
  }
}
