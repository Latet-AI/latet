// Commands shown to the user to copy: they are not translated.

export const CMD_DASHBOARD = 'latet dashboard'
export const CMD_DASHBOARD_PRINT = 'latet dashboard --print-url'
export const CMD_STATUS = 'latet status'
export const CMD_START = 'latet start'
export const CMD_DOCTOR = 'latet doctor'
export const CMD_CLAUDE = 'latet claude'
export const CMD_CODEX = 'latet codex'
export const CMD_GLOBAL_INSTALL = 'latet global-install'
export const CMD_TUTORIAL = 'latet tutorial'
/** Your company's endpoint as a destination, by hand (the documentation's example). */
export const CMD_COMPANY_UPSTREAM = 'latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"'
/** The latest release, with the extras that run the name model and the text recognition. */
export const CMD_UPGRADE = 'latet upgrade'
export const CMD_NER_EXTRA = CMD_UPGRADE
export const CMD_NER_INSTALL = 'latet ner install'
export const CMD_OCR_EXTRA = CMD_UPGRADE
export const CMD_OCR_INSTALL = 'latet ocr install'

/** The command that routes an installed agent through Latet. */
export function applyCommand(agentId: string): string {
  return `latet agents apply ${agentId}`
}

export const CMD_APPLY_CLAUDE = applyCommand('claude')

/** What the Protected agents page offers an installed agent to route it through Latet: none when it goes through
 * Latet already, or when it does once started with Latet's own command (`latet gemini`), which Latet never edits. */
export function fixCommand(agent: { id: string; installed: boolean; status: string }): string | null {
  return agent.installed && agent.status !== 'applied' && agent.status !== 'wrapper' ? applyCommand(agent.id) : null
}
