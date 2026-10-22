// Strict data mode, guided: which columns of a tool's output to keep in clear, from what Latet
// proposes for each (and, when you ask, the local reviewer's opinion). Nothing applies by itself.

import { ApiError } from '../api/client'
import type { Agent, DataMode, FieldRule, OutputColumn, OutputColumns, OutputShape, ProjectDetail, Slot } from '../api/types'
import { canKeep, keepInClear } from './fields'

/** A tool's output with values strict data mode masked: the only blocks the columns are shown for. */
export function hasStrictValues(slot: Slot): boolean {
  return slot.kind === 'tool_result' && slot.replacements.some((r) => r.module === 'data')
}

/** The columns checked at first: the ones proposed in clear that a rule can keep and none keeps yet. */
export function initialChecks(columns: readonly OutputColumn[]): Set<string> {
  return new Set(columns.filter((c) => c.proposal === 'keep' && canKeep(c.name) && !c.kept_now).map((c) => c.name))
}

/** The rules with the checked columns kept in clear: first, each once, in the order the output has them. */
export function rulesWith(fields: readonly FieldRule[], columns: readonly OutputColumn[], checked: ReadonlySet<string>): FieldRule[] {
  return keepInClear(
    fields,
    columns.filter((c) => checked.has(c.name)).map((c) => c.name),
  )
}

/**
 * Where the choice can go: the project, when its settings are loaded, or everywhere. `notLoaded`: the
 * exchange has a project, but Latet restarted since and loads its settings at its next session.
 */
export function whereChoices(project: OutputColumns['project']): { project: boolean; notLoaded: boolean } {
  return { project: project?.loaded === true, notLoaded: project !== null && !project.loaded }
}

/** Agents that send the whole conversation each time: the outputs already in it go again, with the columns in clear. */
export function resends(agent: Agent): boolean {
  return agent === 'codex' || agent === 'opencode' || agent === 'gemini-cli' || agent === 'tutorial-agent'
}

/** The checked columns a rule for everywhere would not change there: a rule of the project's own comes first. */
export function heldByProject(columns: readonly OutputColumn[], checked: ReadonlySet<string>): string[] {
  return columns.filter((c) => checked.has(c.name) && !c.everywhere).map((c) => c.name)
}

/**
 * Whether strict data mode is on for an exchange: as it applies in its project, or as the global rules say
 * (`global`) for an exchange of no project or of a project Latet has not loaded since it started (`null`: it
 * answers 404 until then). Undefined while what decides is still loading.
 */
export function strictOn(global: DataMode | undefined, project: ProjectDetail | null | undefined): boolean | undefined {
  if (project === undefined) return undefined
  if (project) return project.effective.data === 'strict'
  return global === undefined ? undefined : global === 'strict'
}

/**
 * Where strict data mode is set for an exchange: the project's rules when the project's own settings say `data` (yours
 * for it, either way; its latet.yaml, which can only turn it on), else the global rules.
 */
export function strictLayer(project: ProjectDetail | null): 'project' | 'global' {
  if (!project) return 'global'
  return project.personal?.data != null || project.repo.settings.data === 'strict' ? 'project' : 'global'
}

/** The tool outputs of an exchange asked about at most, the most recent: an agent resends its whole history each time. */
export const STRUCTURE_ASKED = 10

/**
 * Whether a tool output of the exchange holds structure strict data mode would mask, as the daemon reads it: the names
 * of its columns (the light answer, `GET /exchanges/{id}/columns?slot=&shape=1`: no proposal, no detector), asked of
 * the most recent tool outputs first (STRUCTURE_ASKED at most) up to the first that has some and is of a tool strict
 * data mode covers (`applies`: not a file read nor a web page no rule names). The daemon reads them only from the texts the history
 * keeps (409 otherwise: none, then); a block it no longer finds (404) is passed over; an answer that says strict data
 * mode is on there leaves nothing to say.
 */
export async function holdsStructure(slots: readonly Slot[], columns: (slot: number) => Promise<OutputShape>): Promise<boolean> {
  const outputs = slots.flatMap((slot, index) => (slot.kind === 'tool_result' ? [index] : []))
  for (const index of outputs.reverse().slice(0, STRUCTURE_ASKED)) {
    try {
      const found = await columns(index)
      if (found.columns.length > 0 && found.applies) return !found.strict
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) continue
      if (error instanceof ApiError && error.status === 409) return false
      throw error
    }
  }
  return false
}
