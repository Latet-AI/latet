// The rules a translation file follows, against English: the same keys, the same {{variables}}, and every
// plural form its language needs (Intl.PluralRules says which). check-i18n.mjs reports them; tests pin them.

const PLURAL = /_(zero|one|two|few|many|other)$/

export function flatten(value, prefix = '', out = new Map()) {
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (child && typeof child === 'object' && !Array.isArray(child)) flatten(child, path, out)
    else out.set(path, child)
  }
  return out
}

export function variables(text) {
  return [...new Set([...String(text).matchAll(/\{\{\s*([\w.]+)\s*\}\}/g)].map((m) => m[1]))].sort()
}

const base = (key) => key.replace(PLURAL, '')

export function compareLanguage(en, other, lang) {
  const enPlural = new Set([...en.keys()].filter((k) => PLURAL.test(k)).map(base))
  const enPlain = [...en.keys()].filter((k) => !PLURAL.test(k))
  const otherBases = new Set([...other.keys()].map(base))

  const missing = [...enPlain.filter((k) => !other.has(k)), ...[...enPlural].filter((b) => !otherBases.has(b))].sort()
  const extra = [...other.keys()].filter((k) => !(PLURAL.test(k) ? enPlural.has(base(k)) : en.has(k))).sort()

  const variableIssues = []
  for (const [key, text] of other) {
    if (PLURAL.test(key)) {
      // A plural form keeps the variables of English's form of its category (of _other, for one English lacks,
      // like _many), but {{count}}: any form may leave it out (English's _one does) or add it (French's _one
      // covers 0 too). It has none that English's forms of the key lack.
      const allowed = new Set([...en].filter(([k]) => base(k) === base(key)).flatMap(([, t]) => variables(t)))
      const want = variables(en.get(key) ?? en.get(`${base(key)}_other`) ?? '')
      const found = variables(text)
      const lost = want.some((v) => v !== 'count' && !found.includes(v))
      if (lost || found.some((v) => !allowed.has(v))) variableIssues.push({ key, en: want, other: found })
    } else if (en.has(key)) {
      const want = variables(en.get(key))
      const found = variables(text)
      if (want.join() !== found.join()) variableIssues.push({ key, en: want, other: found })
    }
  }

  const categories = new Intl.PluralRules(lang).resolvedOptions().pluralCategories
  const plurals = []
  for (const b of [...enPlural].sort()) {
    if (!otherBases.has(b)) continue
    const lacks = categories.filter((c) => !other.has(`${b}_${c}`))
    if (lacks.length) plurals.push({ key: b, lacks: [...lacks].sort() })
  }

  const untranslated = [...other].filter(([k, t]) => en.get(k) === t).length
  return { missing, extra, variables: variableIssues, plurals, untranslated }
}
