// Where each place of the dashboard is explained in the documentation (docsLinks.ts): the sections of the rules,
// global and a project's, and what explains each way a request is stopped and each flag that says what happened.

import { ANCHORS, PROJECT_ANCHORS } from '../components/rules'
import type { BlockKind, LiveEnd } from './blocks'
import type { DocsTarget } from './docsLinks'
import type { FlagKey } from './flags'
import type { ProjectAnchor, RulesAnchor } from './rulesTabs'

/** Each section of the global rules: its "?" beside its title. */
export const RULES_DOCS: Record<RulesAnchor, DocsTarget> = {
  [ANCHORS.profile]: 'section.profile',
  [ANCHORS.pii]: 'section.pii',
  [ANCHORS.ner]: 'section.names',
  [ANCHORS.phones]: 'section.phones',
  [ANCHORS.privateIps]: 'section.privateIps',
  [ANCHORS.terms]: 'section.terms',
  [ANCHORS.recognizers]: 'section.recognizers',
  [ANCHORS.allow]: 'section.allow',
  [ANCHORS.policies]: 'section.policies',
  [ANCHORS.reviewer]: 'section.reviewer',
  [ANCHORS.live]: 'section.liveReview',
  [ANCHORS.data]: 'section.data',
  [ANCHORS.identity]: 'section.identity',
  [ANCHORS.history]: 'section.history',
}

/** Each section of a project's rules: its lists are explained where the global ones are, its recognizers for one project. */
export const PROJECT_DOCS: Record<ProjectAnchor, DocsTarget> = {
  [PROJECT_ANCHORS.effective]: 'section.projectEffective',
  [PROJECT_ANCHORS.personal]: 'section.projectPersonal',
  [PROJECT_ANCHORS.protection]: 'section.projectProtection',
  [PROJECT_ANCHORS.projectData]: 'section.projectData',
  [PROJECT_ANCHORS.terms]: 'section.terms',
  [PROJECT_ANCHORS.recognizers]: 'section.projectRecognizers',
  [PROJECT_ANCHORS.allow]: 'section.allow',
  [PROJECT_ANCHORS.policies]: 'section.policies',
  [PROJECT_ANCHORS.reviewer]: 'section.projectReviewer',
  [PROJECT_ANCHORS.live]: 'section.projectLive',
  [PROJECT_ANCHORS.repo]: 'section.projectRepo',
}

/** Why a request was stopped, by how (the Live Reviewer's, by how it ended: LIVE_DOCS). */
export const BLOCK_DOCS: Record<Exclude<BlockKind, 'live'>, DocsTarget> = {
  review: 'why.autoModeReview',
  history: 'why.history',
  plugin: 'why.blocked',
  uninspected: 'why.uninspected',
  imagesSetting: 'why.imagesSetting',
  imagesMask: 'why.imagesMask',
  localReviewer: 'why.localReviewer',
  leak: 'why.blocked',
  outbound: 'why.outbound',
  other: 'why.blocked',
}

/** A text the Live Reviewer did not send: you stopped it, it waited past its time, or its agent gave up. */
export const LIVE_DOCS: Record<LiveEnd, DocsTarget> = {
  rejected: 'why.liveRejected',
  expired: 'why.liveTimeout',
  abandoned: 'why.liveTimeout',
}

/**
 * The flags of a request that say what happened to it, each with where that is explained. Not Blocked and Leak, whose
 * notices under them say why; not Error (the provider's answer), nor Unknown route and the daemon's other flags, whose
 * words say all there is.
 */
export const FLAG_DOCS: Partial<Record<FlagKey, DocsTarget>> = {
  uninspected: 'why.uninspected',
  degraded: 'why.degraded',
  'auto-mode-review': 'why.autoModeReviewer',
  'project-off': 'why.projectOff',
  withheld: 'why.withheld',
  history: 'why.history',
  outbound: 'why.outbound',
  'route-bypass': 'why.routeBypass',
  image: 'why.image',
}
