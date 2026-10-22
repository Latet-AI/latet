import { describe, expect, it } from 'vitest'
import { BLOCK_DOCS, FLAG_DOCS, LIVE_DOCS, PROJECT_DOCS, RULES_DOCS } from './docsPlaces'
import links from './docsLinks.json'
import { PROJECT_TABS, RULES_TABS } from './rulesTabs'

// Every section of the rules opens its documentation, and every notice that says why something happened says it
// with a link to where it is explained: the maps hold one for each, and only targets of the link table.

describe('the documentation of each place', () => {
  it('has a section for every section of the global rules and of a project’s', () => {
    expect(RULES_TABS.flatMap((tab) => tab.anchors).filter((anchor) => !(anchor in RULES_DOCS))).toEqual([])
    expect(PROJECT_TABS.flatMap((tab) => tab.anchors).filter((anchor) => !(anchor in PROJECT_DOCS))).toEqual([])
  })

  it('explains every way a request is stopped, the Live Reviewer’s three included', () => {
    expect(Object.keys(BLOCK_DOCS).sort()).toEqual(
      ['history', 'imagesMask', 'imagesSetting', 'leak', 'localReviewer', 'other', 'outbound', 'plugin', 'review', 'uninspected'],
    )
    expect(Object.keys(LIVE_DOCS).sort()).toEqual(['abandoned', 'expired', 'rejected'])
  })

  it('explains the flags that say what happened, not those with a notice of their own or nothing to explain', () => {
    // Blocked and Leak have the notice under them; Error is the provider's; Unknown route and another flag say all.
    expect(Object.keys(FLAG_DOCS).sort()).toEqual(
      ['auto-mode-review', 'degraded', 'history', 'image', 'outbound', 'project-off', 'route-bypass', 'uninspected', 'withheld'],
    )
  })

  it('names only targets of the link table', () => {
    const all = [RULES_DOCS, PROJECT_DOCS, BLOCK_DOCS, LIVE_DOCS, FLAG_DOCS].flatMap((map) => Object.values(map))
    expect(all.filter((target) => !(target in links))).toEqual([])
  })
})
