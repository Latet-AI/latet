// The daemon's doctor writes commands between backticks: "not installed: `latet service install`".

export interface InlinePart {
  text: string
  code: boolean
}

/** Split a message into plain text and `code` parts. An unmatched backtick stays plain text. */
export function splitInlineCode(message: string): InlinePart[] {
  const parts: InlinePart[] = []
  const pattern = /`([^`\n]+)`/g
  let last = 0
  for (const match of message.matchAll(pattern)) {
    const index = match.index
    if (index > last) parts.push({ text: message.slice(last, index), code: false })
    parts.push({ text: match[1] ?? '', code: true })
    last = index + match[0].length
  }
  if (last < message.length) parts.push({ text: message.slice(last), code: false })
  return parts
}

/** Commands to run, in the order they appear. */
export function extractCommands(message: string): string[] {
  return splitInlineCode(message)
    .filter((part) => part.code)
    .map((part) => part.text.trim())
    .filter(Boolean)
}
