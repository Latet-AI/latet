// Fails when a translation key used in the source is missing from src/locales/en.json, and checks that every
// other language has the same keys, the same {{variables}} and the plural forms it needs (i18n-rules.mjs);
// strings still the same as English are counted. Lists keys en.json defines but the source never uses (warning).
//
// A key counts as used when it appears as a string literal in:
//   t('key'), i18n.t('key'), tk('key')        (tk marks keys used through a variable)
//   i18nKey="key" / i18nKey={'key'}           (the <Trans> component)
// Plural forms (key_one, key_other...) are satisfied by a use of `key`.

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compareLanguage, flatten } from './i18n-rules.mjs'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = join(root, 'src')
const localesDir = join(srcDir, 'locales')
const PLURAL = /_(zero|one|two|few|many|other)$/

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== 'locales' && name !== 'node_modules') walk(path, files)
    } else if (/\.(tsx?|jsx?)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      files.push(path)
    }
  }
  return files
}

const lineOf = (text, index) => text.slice(0, index).split('\n').length

const patterns = [
  /(?<![\w$.])(?:i18n\.)?t\(\s*(['"`])([^'"`$\\\n]+)\1/g,
  /(?<![\w$.])tk\(\s*(['"`])([^'"`$\\\n]+)\1/g,
  /i18nKey=\{?\s*(['"`])([^'"`$\\\n]+)\1/g,
]

const en = flatten(JSON.parse(readFileSync(join(localesDir, 'en.json'), 'utf8')))
const enBase = new Set([...en.keys()].map((k) => k.replace(PLURAL, '')))
const has = (key) => en.has(key) || [...en.keys()].some((k) => k.replace(PLURAL, '') === key && PLURAL.test(k))

const used = new Map() // key -> [file:line]
for (const file of walk(srcDir)) {
  const text = readFileSync(file, 'utf8')
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const key = match[2]
      const where = `${relative(root, file)}:${lineOf(text, match.index)}`
      if (!used.has(key)) used.set(key, [])
      used.get(key).push(where)
    }
  }
}

let failed = false
const missing = [...used.keys()].filter((key) => !has(key)).sort()
if (missing.length) {
  failed = true
  console.error(`✖ ${missing.length} key(s) used in the source are missing from src/locales/en.json:`)
  for (const key of missing) console.error(`  ${key}  (${used.get(key).join(', ')})`)
}

const notLeaf = [...used.keys()].filter((key) => !has(key) && [...en.keys()].some((k) => k.startsWith(`${key}.`)))
for (const key of notLeaf) console.error(`  note: "${key}" is a group of keys, not a string`)

const usedBase = new Set([...used.keys()])
const unused = [...enBase].filter((key) => !usedBase.has(key)).sort()
if (unused.length) {
  console.warn(`⚠ ${unused.length} key(s) in en.json are not used in the source:`)
  for (const key of unused) console.warn(`  ${key}`)
}

for (const name of readdirSync(localesDir).sort()) {
  if (!name.endsWith('.json') || name === 'en.json') continue
  const lang = name.slice(0, -'.json'.length)
  const other = flatten(JSON.parse(readFileSync(join(localesDir, name), 'utf8')))
  const result = compareLanguage(en, other, lang)
  const problems = [
    ...result.missing.map((key) => `missing: ${key}`),
    ...result.extra.map((key) => `not in en.json: ${key}`),
    ...result.variables.map((v) => `variables of ${v.key}: ${v.other.join(', ') || 'none'} (English: ${v.en.join(', ') || 'none'})`),
    ...result.plurals.map((p) => `plural ${p.key} lacks: ${p.lacks.join(', ')}`),
  ]
  if (problems.length) {
    failed = true
    console.error(`✖ ${name}: ${problems.length} problem(s):`)
    for (const problem of problems) console.error(`  ${problem}`)
  }
  if (result.untranslated) console.warn(`⚠ ${name}: ${result.untranslated} string(s) still the same as English`)
}

if (failed) process.exit(1)
console.log(`✔ ${used.size} key(s) used, all defined in en.json (${en.size} strings).`)
