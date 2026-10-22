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

/** Same length and character classes, separators kept: what the engine's surrogates look like. */
export function shape(rng: Rng, value: string, keep = 0): string {
  let out = value.slice(0, keep)
  for (const c of value.slice(keep)) {
    if (c >= 'A' && c <= 'Z') out += chars(rng, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 1)
    else if (c >= 'a' && c <= 'z') out += chars(rng, 'abcdefghijklmnopqrstuvwxyz', 1)
    else if (c >= '0' && c <= '9') out += chars(rng, '0123456789', 1)
    else out += c
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
