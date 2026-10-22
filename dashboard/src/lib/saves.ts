// A form's save of your settings, and what the form is made from after it was kept.

import type { ConfigView } from '../api/types'
import { DEFAULT_LIVE, DEFAULT_REVIEW } from './reviewer'

/** What a GET /config holds that a save does not send: read-only, or (identity) only in part. */
type ReadOnly =
  | 'etag'
  | 'pii_available'
  | 'pii_catalog'
  | 'pii_base'
  | 'pii_effective'
  | 'id_regions_available'
  | 'identity'
  | 'ner_status'
  | 'ocr_status'
  | 'recognizer_status'
  | 'checksums'
  | 'data_effective'

/** Your global rules as the Rules page edits them and a save sends them. */
export type Editable = Omit<ConfigView, ReadOnly> & { identity: { enabled: boolean } }

/** The settings a save of your global rules sends, from those read: the save bar's, and a panel's. */
export function editable(config: ConfigView): Editable {
  return {
    profile: config.profile,
    pii: config.pii,
    id_regions: config.id_regions ?? [],
    phone_regions: config.phone_regions,
    include_private_ips: config.include_private_ips,
    terms: config.terms,
    recognizers: config.recognizers ?? [],
    allow_values: config.allow_values,
    allow_patterns: config.allow_patterns,
    policies: config.policies,
    identity: { enabled: config.identity.enabled },
    history: config.history ?? 'full',
    retention_days: config.retention_days ?? 14,
    ner: config.ner ?? 'auto',
    auto_mode_review: config.auto_mode_review ?? { user_name: 'block', paths: 'block', host_name: 'block', git_author: 'block' },
    data: config.data ?? null, // null: the profile decides, and a save keeps it so
    data_tools: config.data_tools ?? [],
    data_files: config.data_files ?? [],
    data_hosts: config.data_hosts ?? [],
    fields: config.fields ?? [],
    dump_guard: config.dump_guard ?? null,
    images: config.images ?? 'allow',
    ocr: config.ocr ?? 'auto',
    llm_review: config.llm_review ?? DEFAULT_REVIEW,
    live_review: config.live_review ?? DEFAULT_LIVE,
  }
}

/**
 * What a form is made again from once its save was kept: the settings read after it (`fresh`) while the form is still
 * what it sent (`latest` is `sent`); null otherwise, when the form changed while the save ran or nothing could be read.
 * Then it keeps what it was made from, its etag among it, which the save made old: its next save is refused as stale and
 * the form made again. So a draft never meets an etag newer than what it was made from, and a value the administrators
 * stopped letting through meanwhile is never saved back as the person's own.
 */
export function remadeFrom<T>(fresh: ConfigView | null, latest: T, sent: T): ConfigView | null {
  return fresh !== null && latest === sent ? fresh : null
}
