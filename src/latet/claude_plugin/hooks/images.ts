// Pure helpers for the images Claude Code sends: where they are in a tool's result, in a prompt you
// pasted, and in the conversation the final check reads. No mods API ($) here, so they can be
// unit-tested on their own.

export type Picture = { data: string; media_type: string }
// What the daemon answers for one image: the image as it may be sent, or none when it was withheld.
export type MaskedPicture = { image: Picture | null; count: number; withheld?: string; blocked?: string; unread?: boolean }

export const NOT_SENT = (why: string) => `[image withheld by Latet: ${why}]`

type Found = { pictures: Picture[]; rebuild: (masked: MaskedPicture[]) => { result: unknown } | { deny: string } }

type ApiBlock = { type?: string; source?: { type?: string; data?: unknown; media_type?: unknown }; content?: unknown; text?: unknown }

function picture(source: ApiBlock['source']): Picture | null {
  if (source?.type !== 'base64' || typeof source.data !== 'string' || !source.data) return null
  return { data: source.data, media_type: typeof source.media_type === 'string' ? source.media_type : 'image/png' }
}

function why(m: MaskedPicture): string {
  return m.withheld ?? m.blocked ?? 'it could not be masked'
}

/** The images of a tool's result: a Read of an image, of PDF pages read as images, or the image
 * blocks of an MCP tool's result. Null when it has none. */
export function toolImages(result: unknown): Found | null {
  if (Array.isArray(result)) {
    const at = result.flatMap((block: ApiBlock, i) => (block?.type === 'image' && picture(block.source) ? [i] : []))
    if (at.length === 0) return null
    return {
      pictures: at.map((i) => picture(result[i].source) as Picture),
      rebuild: (masked) => {
        const out = [...result]
        at.forEach((i, n) => {
          const m = masked[n]
          out[i] = m.image ? { ...out[i], source: { ...out[i].source, data: m.image.data, media_type: m.image.media_type } } : { type: 'text', text: NOT_SENT(why(m)) }
        })
        return { result: out }
      },
    }
  }
  const read = result as { type?: string; file?: Record<string, unknown>; pages?: { base64?: unknown; mediaType?: unknown }[] }
  if (read?.type === 'image' && typeof read.file?.base64 === 'string') {
    const media = typeof read.file.type === 'string' ? read.file.type : 'image/png'
    return {
      pictures: [{ data: read.file.base64, media_type: media }],
      rebuild: ([m]) =>
        m.image
          ? { result: { ...read, file: { ...read.file, base64: m.image.data, type: m.image.media_type } } }
          : { deny: `Latet did not let this image through: ${why(m)}.` },
    }
  }
  if (read?.type === 'parts' && Array.isArray(read.pages)) {
    const at = read.pages.flatMap((page, i) => (typeof page?.base64 === 'string' && page.base64 ? [i] : []))
    if (at.length === 0) return null
    const pages = read.pages
    return {
      pictures: at.map((i) => ({ data: pages[i].base64 as string, media_type: typeof pages[i].mediaType === 'string' ? (pages[i].mediaType as string) : 'image/png' })),
      rebuild: (masked) => {
        const out = [...pages]
        at.forEach((i, n) => {
          const m = masked[n]
          out[i] = m.image ? { ...out[i], base64: m.image.data, mediaType: m.image.media_type } : { ...out[i], base64: '', error: NOT_SENT(why(m)) }
        })
        return { result: { ...read, pages: out } }
      },
    }
  }
  return null
}

/** The images you pasted in a prompt's row, and the row without them: each masked one is offered as a
 * copy to read (``copies``: where each is), and a note says what happened. Null when there are none. */
export function pastedImages(content: unknown): { pictures: Picture[]; rebuild: (masked: MaskedPicture[], copies: (string | null)[]) => unknown[] } | null {
  if (!Array.isArray(content)) return null
  const at = content.flatMap((block: ApiBlock, i) => (block?.type === 'image' && picture(block.source) ? [i] : []))
  if (at.length === 0) return null
  return {
    pictures: at.map((i) => picture(content[i].source) as Picture),
    rebuild: (masked, copies) => {
      const notes: string[] = []
      const drop = new Set<number>()
      at.forEach((i, n) => {
        const m = masked[n]
        if (m.image && m.count === 0 && !m.unread) return // nothing to mask in it: it goes as it is
        if (m.image && m.unread) return // not read here: the final check decides
        drop.add(i)
        if (m.image && copies[n]) {
          notes.push(`[Latet: the image you pasted showed values Latet masks, so it was not sent as it was. A copy with them masked is at ${copies[n]}: open it with the Read tool to see it.]`)
        } else {
          notes.push(`[Latet: the image you pasted was not sent: ${why(m)}.]`)
        }
      })
      const kept = content.filter((_, i) => !drop.has(i))
      if (notes.length === 0) return kept
      const note = notes.join('\n')
      const first = kept.findIndex((b: ApiBlock) => b?.type === 'text' && typeof b.text === 'string')
      if (first < 0) return [...kept, { type: 'text', text: note }]
      return kept.map((b: ApiBlock, i) => (i === first ? { ...b, text: `${b.text}\n\n${note}` } : b))
    },
  }
}

/** The images a conversation sends, for the final check: in messages, and in tool results. */
export function apiImages(messages: readonly { content?: unknown }[]): Picture[] {
  const out: Picture[] = []
  const walk = (blocks: unknown): void => {
    if (!Array.isArray(blocks)) return
    for (const block of blocks as ApiBlock[]) {
      if (block?.type === 'image') {
        const found = picture(block.source)
        if (found) out.push(found)
      } else if (block?.type === 'tool_result') {
        walk(block.content)
      }
    }
  }
  for (const message of messages) walk(message.content)
  return out
}

const EXTENSIONS: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' }

/** Where a masked copy of a pasted image is offered: a file name Claude Code never finds on disk, which
 * the plugin answers itself when the Read tool asks for it. */
export function copyPath(folder: string, id: string, media: string): string {
  const separator = folder.includes('\\') && !folder.includes('/') ? '\\' : '/'
  return `${folder.replace(/[\\/]+$/, '')}${separator}.latet${separator}masked-${id}.${EXTENSIONS[media] ?? 'png'}`
}

/** The id of the masked copy a Read asks for, or null for any other file. */
export function copyId(path: unknown): string | null {
  if (typeof path !== 'string') return null
  const found = /[\\/]\.latet[\\/]masked-([0-9a-f]{12})\.(?:png|jpg|webp|gif)$/.exec(path)
  return found ? found[1] : null
}
