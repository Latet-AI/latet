// Rebuild a JSON document from the slots' JSON paths ("$.messages[3].content[0].text").
// The contract stores the inspected text fields, not the whole body: this gives a faithful view of
// what was sent in those fields, in their place in the payload.

export type PathPart = string | number

/** Parse a JSONPath-like location. Returns null when the path is malformed. */
export function parsePath(path: string): PathPart[] | null {
  if (!path.startsWith('$')) return null
  const parts: PathPart[] = []
  let i = 1
  while (i < path.length) {
    const char = path[i]
    if (char === '.') {
      let j = i + 1
      while (j < path.length && path[j] !== '.' && path[j] !== '[') j++
      const name = path.slice(i + 1, j)
      if (!name) return null
      parts.push(name)
      i = j
    } else if (char === '[') {
      const quote = path[i + 1]
      if (quote === '"' || quote === "'") {
        let j = i + 2
        let name = ''
        while (j < path.length && path[j] !== quote) {
          if (path[j] === '\\' && j + 1 < path.length) {
            name += path[j + 1]
            j += 2
          } else {
            name += path[j]
            j++
          }
        }
        if (path[j] !== quote || path[j + 1] !== ']') return null
        parts.push(name)
        i = j + 2
      } else {
        const close = path.indexOf(']', i)
        if (close === -1) return null
        const raw = path.slice(i + 1, close).trim()
        if (raw === '*' || raw === '') return null
        const index = Number(raw)
        if (!Number.isInteger(index) || index < 0) return null
        parts.push(index)
        i = close + 1
      }
    } else {
      return null
    }
  }
  return parts
}

type Container = Record<string, unknown> | unknown[]

const isContainer = (value: unknown): value is Container => typeof value === 'object' && value !== null

/**
 * Build one document from (path, value) entries. An entry that conflicts with an earlier one (a
 * value where a container is needed, or the reverse) or has a malformed path is returned in
 * `skipped`. Array positions no entry fills come out as null in JSON.
 */
export function buildFromPaths(entries: readonly { path: string; value: unknown }[]): {
  root: unknown
  skipped: string[]
} {
  let root: unknown = undefined
  const skipped: string[] = []
  for (const { path, value } of entries) {
    const parts = parsePath(path)
    if (parts === null) {
      skipped.push(path)
      continue
    }
    if (parts.length === 0) {
      if (root === undefined) root = value
      else skipped.push(path)
      continue
    }
    const first = parts[0]!
    if (root === undefined) root = typeof first === 'number' ? [] : {}
    if (!isContainer(root) || Array.isArray(root) !== (typeof first === 'number')) {
      skipped.push(path)
      continue
    }
    let node: Container = root
    let ok = true
    for (let p = 0; p < parts.length; p++) {
      const part = parts[p]!
      const last = p === parts.length - 1
      const nextPart = parts[p + 1]
      if (Array.isArray(node) !== (typeof part === 'number')) {
        ok = false
        break
      }
      const current: unknown = (node as Record<string | number, unknown>)[part]
      if (last) {
        if (current !== undefined) {
          ok = false
          break
        }
        ;(node as Record<string | number, unknown>)[part] = value
        break
      }
      if (current === undefined) {
        const child: Container = typeof nextPart === 'number' ? [] : {}
        ;(node as Record<string | number, unknown>)[part] = child
        node = child
      } else if (isContainer(current) && Array.isArray(current) === (typeof nextPart === 'number')) {
        node = current
      } else {
        ok = false
        break
      }
    }
    if (!ok) skipped.push(path)
  }
  return { root: root ?? {}, skipped }
}

/** JSON text of a document with sparse array holes written as null. */
export function toJson(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) => (Array.isArray(v) ? Array.from(v, (x) => x ?? null) : v), 2)
}
