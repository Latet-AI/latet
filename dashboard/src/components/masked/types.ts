import type { Module, Via } from '../../api/types'
import type { ViewClass } from '../../lib/labels'

export type Side = 'original' | 'sent' | 'model' | 'agent'

/** Everything the inspector shows about one masked value. */
export interface SpanInfo {
  /** Shared by the marks of one value across panes (original ↔ sent, model ↔ agent). */
  pair: string
  /** Footnote number inside its block, when the view numbers values. */
  footnote: number | null
  type: string
  cls: ViewClass
  rule: string | null
  module: Module | null
  via: Via | null
  entity: string | null
  /** The real value, or null when Latet never saw it (masked by the Claude Code plugin). */
  original: string | null
  /** What was sent upstream instead (a surrogate, or a [REDACTED:TYPE] marker). */
  sent: string
  /** One-way redaction: the value is never restored. */
  redacted: boolean
  /**
   * False when no vault entry is linked to the value: forgotten, or redacted one way, which links none. The vault
   * holds none for a value the detectors redacted, and keeps one the local reviewer or the Live Reviewer found under
   * `redact` sealed, with a fake that never leaves. Either way its original is not shown.
   */
  known: boolean
  /** Found in its JSON-escaped form inside a JSON document. */
  json: boolean
  side: Side
  /**
   * A value that stopped the request: in clear in the text, so nothing was masked and nothing left.
   * `original` and `sent` are then the value as the check read it, which is what Not sensitive must name.
   */
  stopped?: boolean
  /**
   * A stopped value read more than one way (a PEM key whole, and its body inside it) is one mark over the
   * union: `original` and `sent` are all of it, and these are the distinct texts the check read, which Not
   * sensitive must all name, or the request stops again on the one left.
   */
  texts?: string[]
}

/** A highlighted range of a text, in UTF-16 offsets. */
export interface TextMark {
  start: number
  end: number
  info: SpanInfo
  /** Found by matching a known surrogate (responses), not reported by the daemon. */
  soft?: boolean
}
