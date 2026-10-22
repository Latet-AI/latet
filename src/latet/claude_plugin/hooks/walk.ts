// Pure helpers for the Latet hooks module: walking JSON values and Messages API content.
// No mods API ($) here, so they can be imported by register.ts and unit-tested on their own.

export type PathKey = string | number
export type StringLeaf = { path: PathKey[]; value: string }
export type Slot = { text: string; kind: string; from_model: boolean }

/** Every string inside a JSON value, with its path, skipping the given top-level keys. */
export function collectStrings(value: unknown, skipTopLevel: ReadonlySet<string> = new Set()): StringLeaf[] {
  const out: StringLeaf[] = []
  const walk = (v: unknown, path: PathKey[]): void => {
    if (typeof v === 'string') {
      out.push({ path, value: v })
    } else if (Array.isArray(v)) {
      v.forEach((x, i) => walk(x, [...path, i]))
    } else if (v !== null && typeof v === 'object') {
      for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
        if (path.length === 0 && skipTopLevel.has(k)) continue
        walk(x, [...path, k])
      }
    }
  }
  walk(value, [])
  return out
}

/** A copy of ``value`` with the strings at ``leaves`` replaced by ``replacements`` (same order). */
export function replaceStrings<T>(value: T, leaves: StringLeaf[], replacements: string[]): T {
  const copy = structuredClone(value) as unknown as Record<PathKey, unknown>
  leaves.forEach((leaf, i) => {
    if (leaf.path.length === 0) return
    let node: Record<PathKey, unknown> = copy
    for (const key of leaf.path.slice(0, -1)) node = node[key] as Record<PathKey, unknown>
    node[leaf.path[leaf.path.length - 1]] = replacements[i]
  })
  return copy as unknown as T
}

export type KeyLeaf = { path: PathKey[]; key: string }

/**
 * Every key of the objects inside a JSON value, with the path of its object: a key can hold a value (an address, a
 * name) that the daemon masks and restores as it does a string. The top level's own keys are not taken: a tool call's
 * arguments are named by its tool.
 */
export function collectKeys(value: unknown): KeyLeaf[] {
  const out: KeyLeaf[] = []
  const walk = (v: unknown, path: PathKey[]): void => {
    if (Array.isArray(v)) {
      v.forEach((x, i) => walk(x, [...path, i]))
    } else if (v !== null && typeof v === 'object') {
      for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
        if (path.length > 0) out.push({ path, key: k })
        walk(x, [...path, k])
      }
    }
  }
  walk(value, [])
  return out
}

/**
 * A copy of ``value`` with the keys at ``keys`` named ``names`` (same order), each object's keys in their order. The
 * deepest objects go first, so that the path of each object still names its keys as they were. Two keys of an object
 * that would be named alike (a stand-in beside the value it stands for) each keep their own name: an object holds a
 * key once, and a value would be lost.
 */
export function renameKeys<T>(value: T, keys: KeyLeaf[], names: string[]): T {
  const copy = structuredClone(value) as unknown as Record<PathKey, unknown>
  const objects = new Map<string, { path: PathKey[]; names: Map<string, string> }>()
  keys.forEach((leaf, i) => {
    if (names[i] === leaf.key) return
    const id = JSON.stringify(leaf.path)
    const found = objects.get(id) ?? { path: leaf.path, names: new Map<string, string>() }
    found.names.set(leaf.key, names[i])
    objects.set(id, found)
  })
  const deepestFirst = [...objects.values()].sort((a, b) => b.path.length - a.path.length)
  for (const { path, names: given } of deepestFirst) {
    let parent: Record<PathKey, unknown> = copy
    for (const key of path.slice(0, -1)) parent = parent[key] as Record<PathKey, unknown>
    const last = path[path.length - 1]
    const object = parent[last] as Record<string, unknown>
    const own = Object.keys(object)
    let named = own.map((k) => given.get(k) ?? k)
    while (new Set(named).size !== named.length) {
      // What one key would be named, another is: those keep their own name.
      const counts = new Map<string, number>()
      for (const name of named) counts.set(name, (counts.get(name) ?? 0) + 1)
      named = named.map((name, i) => ((counts.get(name) ?? 0) > 1 ? own[i] : name))
    }
    parent[last] = Object.fromEntries(own.map((k, i) => [named[i], object[k]]))
  }
  return copy as unknown as T
}

type Block = { type?: string; text?: unknown; content?: unknown; input?: unknown; tool_use_id?: unknown }
type ApiMessage = { role?: string; content?: unknown }

/** The text a Messages API conversation sends: text blocks, tool results and tool inputs. */
export function apiSlots(messages: readonly ApiMessage[]): Slot[] {
  const slots: Slot[] = []
  for (const message of messages) {
    const fromModel = message.role === 'assistant'
    const kind = fromModel ? 'assistant_text' : 'user_text'
    const content = message.content
    if (typeof content === 'string') {
      slots.push({ text: content, kind, from_model: fromModel })
      continue
    }
    if (!Array.isArray(content)) continue
    for (const block of content as Block[]) {
      if (block.type === 'text' && typeof block.text === 'string') {
        slots.push({ text: block.text, kind, from_model: fromModel })
      } else if (block.type === 'tool_result') {
        if (typeof block.content === 'string') {
          slots.push({ text: block.content, kind: 'tool_result', from_model: false })
        } else if (Array.isArray(block.content)) {
          for (const inner of block.content as Block[]) {
            if (inner.type === 'text' && typeof inner.text === 'string') {
              slots.push({ text: inner.text, kind: 'tool_result', from_model: false })
            }
          }
        }
      } else if (block.type === 'tool_use') {
        for (const leaf of collectStrings(block.input)) {
          slots.push({ text: leaf.value, kind: 'tool_input', from_model: true })
        }
      }
    }
  }
  return slots
}

/**
 * The texts of the maskable blocks of one row's content, the tool_use id each tool result answers
 * (undefined for other text), and a function putting the texts back.
 */
export function rowTexts(content: readonly Block[]): {
  texts: string[]
  ids: Array<string | undefined>
  rebuild: (masked: string[]) => Block[]
} {
  const texts: string[] = []
  const ids: Array<string | undefined> = []
  const places: Array<{ block: number; inner?: number; whole?: boolean }> = []
  content.forEach((block, i) => {
    if (block.type === 'text' && typeof block.text === 'string') {
      texts.push(block.text)
      ids.push(undefined)
      places.push({ block: i })
    } else if (block.type === 'tool_result') {
      const id = typeof block.tool_use_id === 'string' ? block.tool_use_id : undefined
      if (typeof block.content === 'string') {
        texts.push(block.content)
        ids.push(id)
        places.push({ block: i, whole: true })
      } else if (Array.isArray(block.content)) {
        ;(block.content as Block[]).forEach((inner, j) => {
          if (inner.type === 'text' && typeof inner.text === 'string') {
            texts.push(inner.text)
            ids.push(id)
            places.push({ block: i, inner: j })
          }
        })
      }
    }
  })
  const rebuild = (masked: string[]): Block[] => {
    const copy = structuredClone(content) as Block[]
    places.forEach((place, k) => {
      const block = copy[place.block]
      if (place.whole) block.content = masked[k]
      else if (place.inner !== undefined) (block.content as Block[])[place.inner].text = masked[k]
      else block.text = masked[k]
    })
    return copy
  }
  return { texts, ids, rebuild }
}
