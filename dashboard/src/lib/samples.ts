// The Playground's example: invented people and identifiers, and a key generated on the spot.

function randomChars(length: number): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  const bytes = new Uint8Array(length)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('')
}

export function playgroundSample(): string {
  return [
    "Hi, I'm Emily Carter (emily.carter@northwind-legal.com, +44 20 7946 0192).",
    'National Insurance number AB 58 39 17 C, IBAN GB82WEST12345698765432.',
    '',
    '# .env of the staging server',
    `DB_PASSWORD=${randomChars(16)}`,
    `STRIPE_SECRET_KEY=${['sk', 'test', ''].join('_')}${randomChars(24)}`,
    'Logs are in C:\\Users\\dhayes\\AppData\\Local\\billing\\logs',
  ].join('\n')
}
