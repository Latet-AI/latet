// Checks of the dashboard in a real browser, on the mock's data: what a test in node cannot see, such as a page that
// loads its own data, a click or a key. Each check is a named function in CHECKS; the run prints PASS or FAIL for
// each and ends with 1 when one fails.
//
//   npm run ui-checks                    every check
//   npm run ui-checks -- strict rules    only the checks whose names hold one of the words given (any case)
//
// Vite serves the dashboard in mock mode on a free port of 127.0.0.1, and Playwright's Chromium (headless, the
// version it pins: `npx playwright install chromium` fetches it once) opens it in English and the light theme. The
// page may reach that origin alone: any other request is refused, and fails the check that made it, as an error
// on the page does.
//
// A check is `async function name({ page, open, app, go })`; it throws (node:assert) when the dashboard is not as
// it should be. `open(path)` loads a page of the dashboard; `app(fn, ...args)` runs `fn(base, ...args)` in the page,
// where `import(base + '/src/...')` gives the dashboard's own modules (the same instances the page uses; the
// arguments go as JSON); `go(path)` moves to another page without loading it again, so what a check changed in the
// mock (it lives in the page's memory) stays.

// The functions given to page.evaluate run in the page, with its globals.
/* global document, window, PopStateEvent, getComputedStyle */

import assert from 'node:assert/strict'
import { readFile, realpath } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const BASE = '/_latet/ui'
// The mock answers each call after 120 to 380 ms: a check that looks for something absent first waits this long
// after the page's last load, so that what would have come had the time to.
const MOCK_SETTLE_MS = 1200

/** The removed switch's labels ("Strict data mode in this project", "... everywhere"). */
const STRICT_SWITCH = /strict data mode/i

// ---------------------------------------------------------------------------------------------------------------
// Strict data mode leaves the exchanges (the plan's Task 3).

/**
 * In the ExchangePage, no switch with role `switch` named like the old `exchange.strict.label*`; and with strict data
 * mode on (the mock's billing-app turns it on in its latet.yaml), no notice and no tool output asked about.
 */
/** An exchange on a phone: its top row (back, the id, Documentation) wraps or shortens the id, never widens the page. */
async function exchangePageFitsAPhone({ page, app, go }) {
  await page.setViewportSize({ width: 375, height: 812 })
  const { exchange } = await app(structuredExchange)
  await go(`/exchanges/${exchange}`)
  await page.locator('#request').waitFor()
  const [scroll, inner] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth])
  assert.ok(scroll <= inner, `the exchange page is ${scroll} px wide in a ${inner} px phone`)
  const link = page.getByRole('link', { name: new RegExp(await word(app, 'docs.page')) }).first()
  const box = await link.boundingBox()
  assert.ok(box && box.x + box.width <= inner, 'the Documentation link is cut off')
}

async function exchangePageHasNoStrictSwitch({ page, app, go }) {
  const { exchange } = await app(structuredExchange)
  await app(watchColumns)
  await go(`/exchanges/${exchange}`)
  await page.locator('#request').waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await page.getByRole('switch', { name: STRICT_SWITCH }).count(), 0, 'a strict data mode switch is on the exchange page')
  assert.equal(await page.locator('[data-strict-notice]').count(), 0, 'the notice shows while strict data mode is on')
  assert.deepEqual(await app(columnsAsked), [], 'tool outputs were asked about while strict data mode is on')
}

/**
 * Strict data mode off in your settings for the project: the exchange asks the daemon for its tool outputs' columns
 * one by one, the most recent first (Ruling 10), stops at the first that has some strict data mode covers, and says
 * so in a line that leads to the project's rules, where the setting that decides is (Ruling 8).
 */
async function exchangeWithStructureLeadsToProjectRules({ page, app, go }) {
  const { exchange, project, outputs, structured, masked } = await app(structuredExchange)
  await app(async (base, id) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changePersonal(id, (personal) => ({ ...personal, data: 'normal' }))
  }, project)
  await app(watchColumns)
  await go(`/exchanges/${exchange}`)
  const notice = await sawNotice(page, app)
  const newest = outputs.toReversed()
  assert.deepEqual(await app(columnsAsked), newest.slice(0, newest.indexOf(structured) + 1), 'not asked newest first, up to the first with columns')
  // The full answer is the columns panel's, under a block with values strict data mode masked: never the notice's.
  const full = await app(fullColumnsAsked)
  assert.ok(full.every((slot) => masked.includes(slot)), `the notice asked for the columns with their proposals: ${JSON.stringify(full)}`)
  await notice.getByRole('link', { name: await word(app, 'exchange.strict.open'), exact: true }).click()
  await landedOn(page, '#project-data', `${BASE}/rules?project=${project}#project-data`)
}

/** Strict data mode off in the global rules, which decide it for the exchange: the line leads to Production data in Rules. */
async function exchangeWithStructureLeadsToGlobalRules({ page, app, go }) {
  const { exchange, project } = await app(structuredExchange)
  await app(async (base, id) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    // The project's latet.yaml turns strict data mode on, and its max profile would too: neither, and Normal globally.
    const { repo } = await api.project(id)
    const settings = { ...repo.settings }
    delete settings.data
    delete settings.profile
    await api.saveRepo(id, settings, repo.tag)
    await api.changeConfig(() => ({ data: 'normal' }))
  }, project)
  await go(`/exchanges/${exchange}`)
  const notice = await sawNotice(page, app)
  await notice.getByRole('link', { name: await word(app, 'exchange.strict.open'), exact: true }).click()
  await landedOn(page, '#data', `${BASE}/rules#data`)
}

/** No tool output of the exchange holds structure (a Codex exchange of the mock, no project, strict data mode off). */
async function exchangeWithoutStructureShowsNothing({ page, app, go }) {
  const { exchange, outputs } = await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    for (const summary of (await api.exchanges()).items.filter((item) => item.agent === 'codex' && !item.project)) {
      const outputs = (await api.exchange(summary.id)).request.slots.flatMap((slot, i) => (slot.kind === 'tool_result' ? [i] : []))
      if (outputs.length > 1) return { exchange: summary.id, outputs }
    }
    throw new Error('no Codex exchange of the mock has two tool outputs')
  })
  await app(watchColumns)
  await go(`/exchanges/${exchange}`)
  await page.locator('#request').waitFor()
  await page.waitForFunction((count) => window.latetColumnsAsked.length >= count, outputs.length)
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await app(columnsAsked), outputs.toReversed(), 'every tool output, once each, the most recent first')
  assert.deepEqual(await app(fullColumnsAsked), [], 'the notice asked for the columns with their proposals')
  assert.equal(await page.locator('[data-strict-notice]').count(), 0, 'the notice shows with no structure in the request')
}

/** With `history: metadata` the daemon keeps no texts to read columns in: nothing is asked, nothing is said. */
async function exchangeAsksNothingWithoutTheTexts({ page, app, go }) {
  const { exchange, project } = await app(structuredExchange)
  await app(async (base, id) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changePersonal(id, (personal) => ({ ...personal, data: 'normal' }))
    await api.changeConfig(() => ({ history: 'metadata' }))
  }, project)
  await app(watchColumns)
  await go(`/exchanges/${exchange}`)
  await page.locator('#request').waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await page.locator('[data-strict-notice]').count(), 0, 'the notice shows with no texts kept')
  assert.deepEqual(await app(columnsAsked), [], 'tool outputs were asked about with no texts kept')
}

/** The mock's exchange of a project whose tool output holds a table strict data mode masked (billing-app's query). */
async function structuredExchange(base) {
  const { api } = await import(`${base}/src/api/client.ts`)
  for (const summary of (await api.exchanges()).items.filter((item) => item.project)) {
    const slots = (await api.exchange(summary.id)).request.slots
    const outputs = slots.flatMap((slot, i) => (slot.kind === 'tool_result' ? [i] : []))
    const masked = outputs.filter((i) => slots[i].replacements.some((replacement) => replacement.module === 'data'))
    const structured = masked[0]
    if (structured !== undefined) return { exchange: summary.id, project: summary.project.id, outputs, structured, masked }
  }
  throw new Error('no exchange of a project holds a value strict data mode masked in the mock')
}

/**
 * From now on, the blocks the dashboard asks the columns of, in order (`columnsAsked` reads them): by the light answer
 * (`?shape=1`, no detector runs for it), the one the notice asks; a block asked the full answer, with its proposals,
 * is kept apart (`fullColumnsAsked`).
 */
async function watchColumns(base) {
  const { api } = await import(`${base}/src/api/client.ts`)
  const asked = (window.latetColumnsAsked = [])
  const full = (window.latetFullColumnsAsked = [])
  const [shape, columns] = [api.columnShape, api.columns]
  api.columnShape = (id, slot, signal) => {
    asked.push(slot)
    return shape(id, slot, signal)
  }
  api.columns = (id, slot, signal) => {
    full.push(slot)
    return columns(id, slot, signal)
  }
}

async function columnsAsked() {
  return window.latetColumnsAsked
}

async function fullColumnsAsked() {
  return window.latetFullColumnsAsked
}

/** The notice in the request, in its words, as a note; never a switch beside it. */
async function sawNotice(page, app) {
  const notice = page.locator('#request [data-strict-notice]')
  await notice.waitFor()
  const words = await app(async (base) => (await import(`${base}/src/i18n.ts`)).default.t('exchange.strict.notice'))
  assert.ok((await notice.innerText()).includes(words), 'the notice does not say the request holds structured values')
  assert.equal(await notice.getAttribute('role'), 'note')
  assert.equal(await page.getByRole('switch', { name: STRICT_SWITCH }).count(), 0, 'a strict data mode switch is on the exchange page')
  return notice
}

/**
 * The page the link led to, scrolled to the part named: its top in the upper half of the window, or, when the page
 * ends too soon for that (the last section of a short tab), the page scrolled to its end with the part in view above
 * the save bar.
 */
async function landedOn(page, selector, address) {
  await page.locator(selector).waitFor()
  assert.equal(await page.evaluate(() => window.location.pathname + window.location.search + window.location.hash), address)
  await page.waitForFunction((target) => {
    const top = document.querySelector(target)?.getBoundingClientRect().top ?? -1
    const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2
    return top >= 0 && (top < window.innerHeight / 2 || (atEnd && top < window.innerHeight - 120))
  }, selector)
}

// ---------------------------------------------------------------------------------------------------------------
// Rules in horizontal tabs (the plan's Task 4).

/** Opening `/rules#reviewer` selects the Reviewers tab and shows the reviewer section, scrolled to; Protection's are hidden. */
async function rulesLinkOpensItsTab({ page, open, app }) {
  await open('/rules#reviewer')
  const tab = await rulesTab(page, app, 'reviewers')
  await tab.waitFor()
  assert.equal(await tab.getAttribute('aria-selected'), 'true', 'the Reviewers tab is not selected')
  await landedOn(page, '#reviewer', `${BASE}/rules#reviewer`)
  assert.equal(await page.locator('#live-review').isVisible(), true, 'the Live Reviewer is not beside the local reviewer')
  assert.equal(await page.locator('#profile').isVisible(), false, 'a section of Protection shows in Reviewers')
  assert.equal(await page.getByRole('tab', { selected: true }).count(), 1)
}

/** Every section a link can name (`/rules#<section>`, as WhyBlocked and RuleName make them) opens its tab, scrolled to. */
async function everyRulesLinkOpensItsTab({ page, open, app, go }) {
  await open('/rules')
  await page.locator('#profile').waitFor()
  for (const { id, anchors } of await app(rulesTabs)) {
    for (const anchor of anchors) {
      await go(`/rules#${anchor}`)
      try {
        await selectedTab(page, id)
        await landedOn(page, `#${anchor}`, `${BASE}/rules#${anchor}`)
      } catch (error) {
        error.message = `#${anchor}: ${error.message}`
        throw error
      }
    }
  }
}

/** The tutorial's links into Rules (its chapters' ends, "Find it later"), followed in its sandbox: each opens its tab. */
async function tutorialLinksIntoRulesOpenTheirTab({ page, open, app, go }) {
  const sources = await Promise.all(['src/mock/tutorial.ts', 'src/lib/tutorial.ts'].map((file) => readFile(join(root, file), 'utf8')))
  const anchors = [...new Set(sources.flatMap((text) => [...text.matchAll(/['"`]\/rules#([\w-]+)['"`]/g)].map((match) => match[1])))]
  assert.deepEqual([...anchors].sort(), ['allow', 'data', 'live-review', 'reviewer', 'terms'], 'the tutorial links to other parts of Rules')
  await open('/live?mock-tutorial')
  const tabs = await app(rulesTabs)
  for (const anchor of anchors) {
    await go(`/rules#${anchor}`)
    await selectedTab(page, tabs.find((tab) => tab.anchors.includes(anchor)).id)
    await landedOn(page, `#${anchor}`, `${BASE}/rules#${anchor}`)
  }
}

/** A setting changed in its tab survives a visit to Protection; the tab shows a dot meanwhile, in words too. */
async function rulesEditSurvivesTabSwitch({ page, open, app }) {
  await open('/rules')
  await page.locator('#profile').waitFor()
  const advanced = await rulesTab(page, app, 'advanced')
  const protection = await rulesTab(page, app, 'protection')
  await advanced.click()
  await page.locator('#history input[value=metadata]').check()
  await protection.click()
  await page.locator('#profile').waitFor()
  assert.equal(await page.locator('#history').isVisible(), false, 'a section of Advanced shows in Protection')
  assert.equal(await advanced.getAttribute('data-dirty'), '', 'the Advanced tab has no dot for its unsaved change')
  assert.ok((await advanced.innerText()).includes(await word(app, 'tabs.unsaved')), 'the Advanced tab says it has unsaved changes only by colour')
  assert.equal(await protection.getAttribute('data-dirty'), null, 'Protection, unchanged, has a dot')
  await advanced.click()
  assert.ok(await page.locator('#history input[value=metadata]').isChecked(), 'the change was lost by switching tabs')
}

/**
 * What a panel holds of its own (a new term typed and not saved yet) and what an editor holds outside the form (a value
 * typed in the allow list's field) survive a visit to another tab; meanwhile the hidden tab is out of reach: no field of
 * it to type in, no heading of it for a screen reader.
 */
async function rulesLocalFieldsSurviveTabSwitch({ page, open, app }) {
  await open('/rules#terms')
  await page.locator('#terms').waitFor()
  const terms = await rulesTab(page, app, 'terms')
  const values = await rulesTab(page, app, 'values')
  const protection = await rulesTab(page, app, 'protection')
  const [termName, allowName, termsTitle, allowTitle, add, newTerm, addAllowed, newAllowed] = await Promise.all(
    [
      'rules.terms.value',
      'rules.allow.column',
      'rules.terms.title',
      'rules.allow.title',
      'rules.terms.add',
      'rules.terms.newValue',
      'rules.allow.add',
      'rules.allow.new',
    ].map((key) => word(app, key)),
  )
  const termField = () => page.getByRole('dialog', { name: newTerm }).getByRole('textbox', { name: termName, exact: true })
  const allowValue = () => page.getByRole('dialog', { name: newAllowed }).getByRole('textbox', { name: allowName, exact: true })
  const termsHeading = () => page.getByRole('heading', { name: termsTitle, exact: true })
  await page.locator('#terms').getByRole('button', { name: add, exact: true }).click()
  await termField().fill('Project Kestrel draft')
  assert.equal(await termsHeading().count(), 1, 'the Terms heading is not there in its own tab')
  await values.click()
  await page.locator('#allow').getByRole('button', { name: addAllowed, exact: true }).click()
  await allowValue().fill('ops@example.com')
  await protection.click()
  await page.locator('#profile').waitFor()
  assert.equal(await termField().count(), 0, "the hidden Terms tab's panel can be reached")
  assert.equal(await allowValue().count(), 0, "the hidden Values tab's field can be reached")
  assert.equal(await termsHeading().count(), 0, 'the Terms heading is in the accessibility tree from Protection')
  assert.equal(await page.getByRole('heading', { name: allowTitle, exact: true }).count(), 0, 'the allow list heading is there from Protection')
  // Nothing of the form changed: typed, not saved.
  assert.equal(await page.locator('[role=tab][data-dirty]').count(), 0, 'a field typed and not saved counts as a change')
  await terms.click()
  assert.equal(await termField().inputValue(), 'Project Kestrel draft', 'the new term typed was lost by switching tabs')
  await values.click()
  assert.equal(await allowValue().inputValue(), 'ops@example.com', 'the value typed in the allow list was lost by switching tabs')
}

/** One Save sends a profile picked in Protection and the history picked in Advanced, and the dots go. */
async function rulesOneSaveSendsEveryTab({ page, open, app }) {
  await open('/rules')
  await page.locator('#profile').waitFor()
  await recordSaves(app)
  const protection = await rulesTab(page, app, 'protection')
  const advanced = await rulesTab(page, app, 'advanced')
  await page.locator('#profile label').filter({ has: page.locator('input[value=max]') }).click()
  await advanced.click()
  await page.locator('#history input[value=metadata]').check()
  assert.equal(await protection.getAttribute('data-dirty'), '')
  assert.equal(await advanced.getAttribute('data-dirty'), '')
  await page.getByRole('button', { name: await word(app, 'rules.save'), exact: true }).click()
  await page.waitForFunction(() => window.latetSaves.length > 0)
  await page.waitForFunction(() => document.querySelectorAll('[role=tab][data-dirty]').length === 0)
  const saves = await page.evaluate(() => window.latetSaves)
  assert.equal(saves.length, 1, 'more than one save')
  assert.equal(saves[0].profile, 'max', 'the profile picked in Protection was not saved')
  assert.equal(saves[0].history, 'metadata', 'the history picked in Advanced was not saved')
}

/** Every save of the global rules the page sends, in `window.latetSaves`. */
async function recordSaves(app) {
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const saves = (window.latetSaves = [])
    const save = api.saveConfig
    api.saveConfig = (config, etag) => {
      saves.push(structuredClone(config))
      return save(config, etag)
    }
  })
}

/** The global rules' terms, as the mock keeps them now. */
async function savedTerms(app) {
  return app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.config()).terms.map((term) => term.value))
}

/** The number a tab shows beside its name. */
async function tabCount(tab) {
  return (await tab.innerText()).match(/\d+/)?.[0] ?? null
}

/**
 * The terms are a table: a term added from the panel, opened again and edited, then two selected and deleted at once
 * after a confirmation; the rows, the tab's count and the saved rules follow each step.
 */
async function termsTableAddsEditsAndDeletes({ page, open, app }) {
  await open('/rules#terms')
  const section = page.locator('#terms')
  await section.getByRole('table').waitFor()
  const tab = await rulesTab(page, app, 'terms')
  assert.equal(await tabCount(tab), '2', 'the Terms tab does not count the mock’s two terms')
  const [value, save, caseSensitive, del] = await Promise.all(['rules.terms.value', 'rules.panel.save', 'rules.terms.caseSensitive', 'rules.table.delete'].map((key) => word(app, key)))

  await section.getByRole('button', { name: await word(app, 'rules.terms.add'), exact: true }).click()
  const adding = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  await adding.getByRole('textbox', { name: value, exact: true }).fill('Orion Labs')
  await adding.getByRole('button', { name: save, exact: true }).click()
  await adding.waitFor({ state: 'detached' })
  await section.getByRole('row').filter({ hasText: 'Orion Labs' }).waitFor()
  await page.waitForFunction(() => /3/.test(document.querySelector('#rules-tab-terms')?.textContent ?? ''))
  assert.deepEqual(await savedTerms(app), ['Project Aurora', 'Whitfield & Co', 'Orion Labs'])

  await section.getByRole('row').filter({ hasText: 'Orion Labs' }).click()
  const editing = page.getByRole('dialog', { name: 'Orion Labs' })
  await editing.getByRole('textbox', { name: value, exact: true }).fill('Orion Labs Ltd')
  await editing.locator('label').filter({ hasText: caseSensitive }).click()
  await editing.getByRole('button', { name: save, exact: true }).click()
  await editing.waitFor({ state: 'detached' })
  const row = section.getByRole('row').filter({ hasText: 'Orion Labs Ltd' })
  await row.waitFor()
  assert.ok((await row.innerText()).includes(await word(app, 'common.yes')), 'the edited term does not match case')
  assert.deepEqual(await savedTerms(app), ['Project Aurora', 'Whitfield & Co', 'Orion Labs Ltd'])

  for (const name of ['Whitfield & Co', 'Orion Labs Ltd']) await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: name }) }).check()
  await section.getByText(await word(app, 'rules.table.selected', { count: 2 })).waitFor()
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  const confirm = page.getByRole('dialog', { name: del })
  await confirm.getByRole('button', { name: del, exact: true }).click()
  await section.getByRole('row').filter({ hasText: 'Orion Labs Ltd' }).waitFor({ state: 'detached' })
  await page.waitForFunction(() => /\b1\b/.test(document.querySelector('#rules-tab-terms')?.textContent ?? ''))
  assert.deepEqual(await savedTerms(app), ['Project Aurora'], 'the delete did not remove exactly the two selected')
}

/**
 * A change of Protection not saved yet stays, unsaved, while a term is saved from its panel: the page reads the
 * settings again after the panel's save, and carries the edit over, since nothing it touches changed meanwhile.
 */
async function rulesPanelSaveKeepsTheSaveBarEdits({ page, open, app }) {
  await open('/rules')
  await page.locator('#profile').waitFor()
  const protection = await rulesTab(page, app, 'protection')
  await page.locator('#profile label').filter({ has: page.locator('input[value=max]') }).click()
  await (await rulesTab(page, app, 'terms')).click()
  await page.locator('#terms').getByRole('button', { name: await word(app, 'rules.terms.add'), exact: true }).click()
  const adding = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  await adding.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).fill('Fernsby Studio')
  await adding.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).click()
  await adding.waitFor({ state: 'detached' })
  await page.locator('#terms').getByRole('row').filter({ hasText: 'Fernsby Studio' }).waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await protection.getAttribute('data-dirty'), '', 'the unsaved profile lost its dot')
  await protection.click()
  assert.ok(await page.locator('#profile input[value=max]').isChecked(), 'the profile picked and not saved was lost')
  await page.getByText(await word(app, 'rules.unsaved'), { exact: true }).waitFor()
  const saved = await app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.config()).profile)
  assert.equal(saved, 'balanced', 'the profile not saved yet was saved with the term')
  assert.ok((await savedTerms(app)).includes('Fernsby Studio'))
}

/**
 * A term of 8,192 characters: its row is one line, cut with an ellipsis and whole in its title, and the page does not
 * grow wider than the window; the panel shows it whole.
 */
async function ruleTableLongValueTruncates({ page, app, go }) {
  const long = `Project ${'x'.repeat(8184)}`
  await app(async (base, value) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changeConfig((config) => ({ terms: [...config.terms, { value, type: 'PROJECT', case_sensitive: false, whole_word: true }] }))
  }, long)
  await go('/rules#terms')
  const cell = page.locator('#terms tbody span[title]').filter({ hasText: 'Project xxxx' })
  await cell.waitFor()
  const fit = await cell.evaluate((span) => ({
    cut: span.scrollWidth > span.clientWidth,
    whole: span.getAttribute('title').length,
    page: document.documentElement.scrollWidth <= window.innerWidth,
    tall: span.getBoundingClientRect().height < 30,
  }))
  assert.deepEqual(fit, { cut: true, whole: 8192, page: true, tall: true }, 'the long term is not one line cut with its title, or widens the page')
  await cell.click()
  const panel = page.getByRole('dialog')
  assert.equal(await panel.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).inputValue(), long, 'the panel does not show it whole')
}

/**
 * A panel left open, with an edit not saved, in a tab no longer shown stays out of the way: Esc in another tab's panel
 * closes that one alone, and asks nothing of a panel nobody sees (its question would be a dialog out of sight, and the
 * page behind it out of reach). Back in its tab, the edit is still there.
 */
async function rulePanelLeftInAnotherTabStaysQuiet({ page, open, app }) {
  await open('/rules#terms')
  await page.locator('#terms').getByRole('row').filter({ hasText: 'Whitfield & Co' }).click()
  const terms = page.getByRole('dialog', { name: 'Whitfield & Co' })
  await terms.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).fill('Whitfield & Co Ltd')
  await (await rulesTab(page, app, 'recognizers')).click()
  await page.locator('#recognizers').getByRole('row').filter({ hasText: 'employee-id' }).click()
  const recognizer = page.getByRole('dialog', { name: 'employee-id' })
  await recognizer.waitFor()
  await page.keyboard.press('Escape')
  await recognizer.waitFor({ state: 'detached' })
  await page.waitForTimeout(300)
  assert.equal(await page.locator('dialog[open]').count(), 0, 'a question opened for a panel out of sight')
  await (await rulesTab(page, app, 'terms')).click()
  assert.equal(await terms.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).inputValue(), 'Whitfield & Co Ltd', 'the edit left in the other tab was lost')
}

/**
 * Leaving Rules with a panel's edit not saved asks first: Stay keeps the page and the edit, Leave goes; closing the
 * browser's tab asks too (beforeunload). Another tab of Rules is no leaving, and asks nothing.
 */
async function leavingWithAnUnsavedPanelAsksFirst({ page, open, app }) {
  await open('/rules#terms')
  await page.locator('#terms').getByRole('row').filter({ hasText: 'Whitfield & Co' }).click()
  const panel = page.getByRole('dialog', { name: 'Whitfield & Co' })
  const value = panel.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true })
  const unloadAsks = () =>
    page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    })
  assert.equal(await unloadAsks(), false, 'closing the tab asks with nothing changed')
  await value.fill('Whitfield & Co Ltd')
  assert.equal(await unloadAsks(), true, 'closing the tab does not ask with a panel changed')
  await (await rulesTab(page, app, 'recognizers')).click()
  await page.waitForTimeout(300)
  assert.equal(await page.locator('dialog[open]').count(), 0, 'another tab of Rules asked')
  await (await rulesTab(page, app, 'terms')).click()

  const live = page.getByRole('link', { name: await word(app, 'nav.live'), exact: true })
  const question = page.getByRole('dialog', { name: await word(app, 'rules.leave.title') })
  await live.click()
  await question.waitFor()
  await question.getByRole('button', { name: await word(app, 'rules.leave.stay'), exact: true }).click()
  await question.waitFor({ state: 'hidden' })
  assert.equal(new URL(page.url()).pathname, `${BASE}/rules`, 'Stay left the page')
  assert.equal(await value.inputValue(), 'Whitfield & Co Ltd', 'Stay lost the edit')
  await live.click()
  await question.getByRole('button', { name: await word(app, 'rules.leave.confirm'), exact: true }).click()
  await page.waitForURL((url) => url.pathname === `${BASE}/live`)
  await page.locator('[data-rule-panel]').waitFor({ state: 'detached' })
  assert.equal(await unloadAsks(), false, 'the page left still asks before closing the tab')
}

/** The search at the top of Rules hides no panel: a term open in its panel stays there while the search holds text
 * its section does not match. */
async function rulePanelStaysThroughTheTopSearch({ page, open, app }) {
  await open('/rules#terms')
  await page.locator('#terms').getByRole('row').filter({ hasText: 'Whitfield & Co' }).click()
  const panel = page.getByRole('dialog', { name: 'Whitfield & Co' })
  const value = panel.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true })
  await value.fill('Whitfield & Co Ltd')
  await page.getByRole('searchbox', { name: await word(app, 'rules.search') }).fill('phone')
  await page.locator('#phones').waitFor()
  assert.equal(await page.locator('#terms').isVisible(), false)
  assert.equal(await panel.isVisible(), true, 'the search hid the panel')
  assert.equal(await value.inputValue(), 'Whitfield & Co Ltd')
}

/** Copy to a project keeps the rows selected until the copy is done: cancelled, the selection stays to pick again. */
async function copyToAProjectKeepsTheSelectionUntilDone({ page, open, app }) {
  await open('/rules#terms')
  const section = page.locator('#terms')
  await section.getByRole('table').waitFor()
  for (const name of ['Project Aurora', 'Whitfield & Co']) await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: name }) }).check()
  const selected = section.getByText(await word(app, 'rules.table.selected', { count: 2 }))
  await selected.waitFor()
  const copy = section.getByRole('toolbar').getByRole('button', { name: await word(app, 'rules.table.copy'), exact: true })
  const dialog = page.getByRole('dialog', { name: await word(app, 'rules.copy.title') })
  await copy.click()
  await dialog.getByRole('button', { name: await word(app, 'common.cancel'), exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  assert.equal(await selected.isVisible(), true, 'cancelling the copy let the selection go')
  await copy.click()
  await dialog.getByRole('button', { name: await word(app, 'rules.copy.confirm'), exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  await selected.waitFor({ state: 'detached' })
}

/** A filter whose value left its menu (its rows deleted) goes back to Any, and the table shows the rows again. */
async function ruleFilterLetsGoAValueThatLeft({ page, open, app }) {
  await open('/rules#terms')
  const section = page.locator('#terms')
  await section.getByRole('table').waitFor()
  const type = section.getByRole('combobox', { name: await word(app, 'rules.terms.type'), exact: true })
  await type.selectOption('CLIENT')
  await section.getByRole('row').filter({ hasText: 'Project Aurora' }).waitFor({ state: 'detached' })
  const del = await word(app, 'rules.table.delete')
  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'Whitfield & Co' }) }).check()
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  await page.getByRole('dialog', { name: del }).getByRole('button', { name: del, exact: true }).click()
  await section.getByRole('row').filter({ hasText: 'Project Aurora' }).waitFor()
  assert.equal(await type.inputValue(), '', 'the filter kept a value its menu no longer has')
  assert.ok((await section.getByRole('status').first().innerText()).includes(await word(app, 'rules.table.count', { count: 1 })))
}

/** On a phone, a row opens over the whole page, as a modal: Close at the top, Save in sight at the bottom; Esc closes
 * it and the focus goes back to the row. */
async function rulePanelIsAModalOnAPhone({ page, open, app }) {
  await page.setViewportSize({ width: 390, height: 844 })
  await open('/rules#terms')
  const row = page.locator('#terms').getByRole('row').filter({ hasText: 'Project Aurora' })
  await row.click()
  const panel = page.getByRole('dialog', { name: 'Project Aurora' })
  await panel.waitFor()
  assert.equal(await panel.getAttribute('aria-modal'), 'true', 'the panel is not a modal on a phone')
  assert.deepEqual(await panel.boundingBox(), { x: 0, y: 0, width: 390, height: 844 }, 'the panel does not cover the page')
  const close = await panel.getByRole('button', { name: await word(app, 'rules.panel.close') }).first().boundingBox()
  assert.ok(close.y < 80, `Close is not at the top (${close.y})`)
  const save = await panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).boundingBox()
  assert.ok(save.y + save.height <= 844 && save.y > 844 - 100, `Save is not in sight at the bottom (${JSON.stringify(save)})`)
  assert.ok(await panel.evaluate((box) => box.contains(document.activeElement)), 'the focus did not move into the panel')
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).overflow), 'hidden', 'the page behind the panel scrolls')
  await page.keyboard.press('Escape')
  await panel.waitFor({ state: 'detached' })
  assert.notEqual(await page.evaluate(() => getComputedStyle(document.body).overflow), 'hidden', 'the page does not scroll again')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('data-key')), 'global:term:PROJECT:Project Aurora', 'the focus did not go back to the row')
}

/**
 * "phone" in the search shows the phone section from any tab, with the tabs hidden meanwhile; cleared, the tab is back.
 * A link to a section while the search holds text lets the search go and opens the section's tab.
 */
async function rulesSearchFindsAcrossTabs({ page, open, app, go }) {
  await open('/rules#terms')
  await page.locator('#terms').waitFor()
  const search = page.getByRole('searchbox', { name: await word(app, 'rules.search') })
  await search.fill('phone')
  await page.locator('#phones').waitFor()
  assert.equal(await page.getByRole('tablist').count(), 0, 'the tabs show while searching')
  assert.equal(await page.locator('#terms').isVisible(), false, 'a section that does not match shows')
  await search.fill('no section says this')
  await page.getByText(await word(app, 'rules.searchNone', { query: 'no section says this' })).waitFor()
  await search.fill('')
  await selectedTab(page, 'terms')
  assert.equal(await page.locator('#terms').isVisible(), true)
  assert.equal(await page.locator('#phones').isVisible(), false)
  // From another tab, a search typed, then a link to Custom terms.
  await (await rulesTab(page, app, 'protection')).click()
  await selectedTab(page, 'protection')
  await search.fill('phone')
  await page.locator('#phones').waitFor()
  await go('/rules#terms')
  await selectedTab(page, 'terms')
  assert.equal(await search.inputValue(), '', 'the search kept its text after a link to a section')
  assert.equal(await page.getByRole('tablist').count(), 1, 'the tabs did not come back after a link to a section')
  await landedOn(page, '#terms', `${BASE}/rules#terms`)
}

/**
 * The search at the top finds rows too, not only sections: "Aurora", a term of the mock, shows the Terms section with
 * that row from any tab, and says in which tab rows were found; let go, the tab shown is back.
 */
async function rulesSearchFindsARowInItsTab({ page, open, app }) {
  await open('/rules#profile')
  await page.locator('#profile').waitFor()
  const search = page.getByRole('searchbox', { name: await word(app, 'rules.search') })
  await search.fill('Aurora')
  const terms = page.locator('#terms')
  await terms.getByRole('row').filter({ hasText: 'Project Aurora' }).waitFor()
  assert.equal(await terms.getByRole('row').filter({ hasText: 'Whitfield' }).count(), 0, 'a row the search does not hold shows')
  assert.equal(await page.locator('#profile').isVisible(), false, 'a section that does not hold the search shows')
  const tab = await word(app, 'rules.tabs.terms')
  await page.getByText(await word(app, 'rules.searchFound', { tabs: `${tab} (1)` })).waitFor()
  await search.fill('')
  await selectedTab(page, 'protection')
  assert.equal(await page.locator('#terms').isVisible(), false)
}

/** The arrow keys, Home and End move between the tabs: the focus, the selection and the address, without scrolling. */
async function rulesTabsMoveWithArrowKeys({ page, open, app }) {
  await open('/rules')
  await page.locator('#profile').waitFor()
  await (await rulesTab(page, app, 'protection')).focus()
  // At the top: a tab that scrolled to its first section would move the page down to it.
  await page.evaluate(() => window.scrollTo(0, 0))
  const top = await page.evaluate(() => window.scrollY)
  for (const [key, id, anchor] of [
    ['ArrowRight', 'terms', 'terms'],
    ['ArrowRight', 'recognizers', 'recognizers'],
    ['ArrowLeft', 'terms', 'terms'],
    ['End', 'advanced', 'identity'],
    ['ArrowRight', 'protection', 'profile'],
    ['ArrowLeft', 'advanced', 'identity'],
    ['Home', 'protection', 'profile'],
  ]) {
    await page.keyboard.press(key)
    await selectedTab(page, id)
    assert.equal(await page.evaluate(() => document.activeElement?.id), `rules-tab-${id}`, `${key}: the focus did not move with the selection`)
    assert.equal(await page.evaluate(() => window.location.hash), `#${anchor}`, `${key}: the address does not name the tab's first section`)
    assert.equal(await page.evaluate(() => window.scrollY), top, `${key}: switching tabs scrolled the page`)
  }
}

/**
 * In a phone's width the row of tabs scrolls sideways: a link to a section in a later tab brings that tab into sight.
 * (The local reviewer, first in its tab: a section below it can move after the scroll, as the reviewer's editor loads.)
 */
async function rulesSelectedTabInSightWhenNarrow({ page, open, app }) {
  await page.setViewportSize({ width: 375, height: 812 })
  await open('/rules#reviewer')
  await selectedTab(page, 'reviewers')
  const tab = await rulesTab(page, app, 'reviewers')
  const [row, box] = await Promise.all([page.getByRole('tablist').boundingBox(), tab.boundingBox()])
  assert.ok(box.x >= row.x - 1 && box.x + box.width <= row.x + row.width + 1, `the selected tab is out of sight in its row (${JSON.stringify({ row, box })})`)
  await landedOn(page, '#reviewer', `${BASE}/rules#reviewer`)
}

/** The menu lists no sections under Rules, the global ones or a project's: the tabs do. */
async function rulesHasNoSectionList({ page, open, app }) {
  const sections = await word(app, 'nav.sections')
  await open('/rules')
  await page.locator('#profile').waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await page.getByRole('list', { name: sections }).count(), 0, 'the menu lists the sections of Rules')
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}`)
  await page.locator('#effective').waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await page.getByRole('list', { name: sections }).count(), 0, "the menu lists the sections of a project's rules")
}

// ---------------------------------------------------------------------------------------------------------------
// A project's rules in the tabs of the global ones.

/**
 * A project's rules have the tabs of the global ones that a project sets, by the same names and in the same order,
 * after its Overview and before its Repository file; the page opens on the Overview.
 */
async function projectRulesHaveTheGlobalTabs({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}`)
  await page.locator('#effective').waitFor()
  const names = await page.getByRole('tablist', { name: await word(app, 'rules.tabs.label') }).getByRole('tab').allInnerTexts()
  const expected = await Promise.all(
    [
      'projects.tabs.overview',
      'rules.tabs.protection',
      'rules.tabs.terms',
      'rules.tabs.recognizers',
      'rules.tabs.values',
      'rules.tabs.policies',
      'rules.tabs.reviewers',
      'projects.tabs.repo',
    ].map(
      (key) => word(app, key),
    ),
  )
  // A list's tab says its count beside its name: "Terms 1 yours · 4 in all".
  assert.deepEqual(
    names.map((name) => name.trim().replace(/\s+\d.*$/, '')),
    expected,
  )
  await selectedTab(page, 'overview', PROJECT_TABS_ID)
  assert.equal(await page.locator('#personal').isVisible(), true, 'your settings as a whole are not in the Overview')
  assert.equal(await page.locator('#project-terms').isVisible(), false, 'a section of Terms shows in the Overview')
}

/** Every section of a project's rules a link can name (`#project-data`, `#repo`...) opens its tab, scrolled to. */
async function everyProjectLinkOpensItsTab({ page, open, app, go }) {
  const project = await app(firstProject)
  const at = `/rules?project=${encodeURIComponent(project)}`
  await open(at)
  await page.locator('#effective').waitFor()
  for (const { id, anchors } of await app(projectTabs)) {
    for (const anchor of anchors) {
      await go(`${at}#${anchor}`)
      try {
        await selectedTab(page, id, PROJECT_TABS_ID)
        await landedOn(page, `#${anchor}`, `${BASE}${at}#${anchor}`)
      } catch (error) {
        error.message = `#${anchor}: ${error.message}`
        throw error
      }
    }
  }
}

/**
 * Your settings for a project, edited in two tabs (Secrets only in Protection, the Live Reviewer on in Reviewers): both
 * tabs get a dot, the edits survive the change of tab, one Save sends both, the dots go, and the tab stays the one shown.
 */
async function projectOneSaveSendsEveryTab({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}`)
  await page.locator('#effective').waitFor()
  await recordPersonalSaves(app)
  const protection = await projectTab(page, app, 'protection')
  const reviewers = await projectTab(page, app, 'reviewers')
  await protection.click()
  await page.locator('#protection label').filter({ has: page.locator('input[value=secrets]') }).click()
  await reviewers.click()
  await page.locator('#project-live-review label').filter({ has: page.locator('input[value=on]') }).click()
  assert.equal(await protection.getAttribute('data-dirty'), '', 'Protection has no dot for its unsaved change')
  assert.equal(await reviewers.getAttribute('data-dirty'), '', 'Reviewers has no dot for its unsaved change')
  assert.equal(await (await projectTab(page, app, 'repo')).getAttribute('data-dirty'), null, 'the repository file, unchanged, has a dot')
  await protection.click()
  assert.ok(await page.locator('#protection input[value=secrets]').isChecked(), 'Secrets only was lost by switching tabs')
  await page.getByRole('button', { name: await word(app, 'projects.personal.save'), exact: true }).click()
  await page.waitForFunction(() => window.latetSaves.length > 0)
  await page.waitForFunction(() => document.querySelectorAll('[role=tab][data-dirty]').length === 0)
  const saves = await page.evaluate(() => window.latetSaves)
  assert.equal(saves.length, 1, 'more than one save')
  assert.equal(saves[0].profile, 'secrets', 'Secrets only, picked in Protection, was not saved')
  assert.equal(saves[0].live_review, true, 'the Live Reviewer, turned on in Reviewers, was not saved')
  await selectedTab(page, 'protection', PROJECT_TABS_ID)
}

/** Every save of your own settings for a project the page sends, in `window.latetSaves`. */
async function recordPersonalSaves(app) {
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const saves = (window.latetSaves = [])
    const save = api.savePersonal
    api.savePersonal = (id, settings, etag) => {
      saves.push(structuredClone(settings))
      return save(id, settings, etag)
    }
  })
}

/** A project's settings as the mock keeps them now: its latet.yaml's and yours. */
async function projectSettings(app, id) {
  return app(async (base, id) => {
    const project = await (await import(`${base}/src/api/client.ts`)).api.project(id)
    return { repo: project.repo.settings, personal: project.personal ?? {} }
  }, id)
}

/** A delete of rows of a project's latet.yaml says, before it, that the file read by everyone is written again. */
async function projectBulkDeleteSaysTheRepoFile({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}#project-terms`)
  const section = page.locator('#project-terms')
  await section.getByRole('table').waitFor()
  const del = await word(app, 'rules.table.delete')
  const confirm = page.getByRole('dialog', { name: del })
  const sentence = await word(app, 'rules.table.repoRows', { count: 1 })
  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'Ashford Paints' }) }).check()
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  await confirm.waitFor()
  assert.equal((await confirm.innerText()).includes(sentence), false, 'a delete of your own rows speaks of latet.yaml')
  await confirm.getByRole('button', { name: await word(app, 'common.cancel'), exact: true }).click()
  await confirm.waitFor({ state: 'hidden' })
  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'Project Falcon' }) }).check()
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  await confirm.waitFor()
  assert.ok((await confirm.innerText()).includes(sentence), `the delete does not say latet.yaml is written: ${await confirm.innerText()}`)
  await confirm.getByRole('button', { name: await word(app, 'common.cancel'), exact: true }).click()
}

/**
 * In a project, each table shows every rule that applies there, a row per source with where it comes from (the global
 * rules, yours for the project, its latet.yaml), a From filter that keeps exactly those of one source, and the tab's
 * count says both how many are yours and how many in all. Values never masked come from no latet.yaml.
 */
async function projectTablesShowEverySource({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}#project-terms`)
  const section = page.locator('#project-terms')
  await section.getByRole('table').waitFor()
  const name = 'billing-app'
  const [global, yours, repo] = await Promise.all([word(app, 'rules.from.global'), word(app, 'rules.from.personal', { project: name }), word(app, 'rules.from.repo')])
  for (const [term, from] of [
    ['Project Aurora', global],
    ['Whitfield & Co', global],
    ['Ashford Paints', yours],
    ['Project Falcon', repo],
  ]) {
    assert.ok((await section.getByRole('row').filter({ hasText: term }).innerText()).includes(from), `${term} does not say it comes from ${from}`)
  }
  const fromFilter = section.getByRole('combobox', { name: await word(app, 'rules.from.title'), exact: true })
  for (const [label, count] of [
    [repo, 1],
    [yours, 1],
    [global, 2],
  ]) {
    await fromFilter.selectOption({ label })
    await page.waitForFunction((count) => document.querySelectorAll('#project-terms tbody tr').length === count, count)
  }
  const tab = await projectTab(page, app, 'terms')
  assert.ok((await tab.innerText()).includes(await word(app, 'rules.tabs.countProject', { count: 1, all: 4 })), `the Terms tab does not count yours and all: ${await tab.innerText()}`)

  await (await projectTab(page, app, 'values')).click()
  const values = page.locator('#project-allow')
  await values.getByRole('table').waitFor()
  assert.ok((await values.getByRole('row').filter({ hasText: 'billing-dev' }).innerText()).includes(yours))
  const offered = await values.getByRole('combobox', { name: await word(app, 'rules.from.title'), exact: true }).locator('option').allInnerTexts()
  assert.ok(!offered.includes(repo), 'values never masked offer latet.yaml as a source')
}

/**
 * A row of the global rules opened in a project is read only there: its panel has no Save, says where it is changed,
 * and its link opens the global rules on that row's tab. It has no checkbox: an action on a selection cannot reach it.
 */
async function projectGlobalRowIsReadOnly({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}#project-terms`)
  const section = page.locator('#project-terms')
  await section.getByRole('table').waitFor()
  const row = section.getByRole('row').filter({ hasText: 'Project Aurora' })
  assert.equal(await row.getByRole('checkbox').count(), 0, 'a global row can be selected in a project')
  await row.click()
  const panel = page.getByRole('dialog', { name: 'Project Aurora' })
  await panel.waitFor()
  assert.equal(await panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).count(), 0, 'a global row can be saved from a project')
  await panel.getByText(await word(app, 'rules.panel.global')).waitFor()
  await panel.getByRole('link', { name: await word(app, 'rules.panel.openGlobal') }).click()
  await page.locator('#terms').waitFor()
  assert.equal(await page.evaluate(() => window.location.pathname + window.location.search + window.location.hash), `${BASE}/rules#terms`)
  await selectedTab(page, 'terms')
}

/** Save to latet.yaml in a project without the file says, before the save, that saving creates it. */
async function saveToSaysLatetYamlIsCreated({ page, app, go }) {
  const sandbox = await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    return (await api.projects()).items.find((item) => item.name === 'sandbox').id
  })
  await go(`/rules?project=${encodeURIComponent(sandbox)}#project-terms`)
  const section = page.locator('#project-terms')
  await section.getByRole('table').waitFor()
  await section.getByRole('button', { name: await word(app, 'rules.table.addFor', { project: 'sandbox' }), exact: true }).click()
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  await panel.getByRole('radiogroup', { name: await word(app, 'rules.saveTo.title') }).locator('label').filter({ hasText: await word(app, 'rules.saveTo.repo') }).click()
  await panel.getByText(await word(app, 'rules.saveTo.created')).waitFor()
}

/**
 * A new row in a project goes to your settings for it unless Save to says latet.yaml, offered only where the dashboard
 * may write the file: saved there, it shows as latet.yaml. Values never masked offer no latet.yaml, nor does a project
 * whose file cannot be written here.
 */
async function projectNewRowSavesToTheChosenPlace({ page, app, go }) {
  const project = await app(firstProject)
  await go(`/rules?project=${encodeURIComponent(project)}#project-terms`)
  const section = page.locator('#project-terms')
  await section.getByRole('table').waitFor()
  const [saveTo, repoChoice, yoursChoice] = await Promise.all([word(app, 'rules.saveTo.title'), word(app, 'rules.saveTo.repo'), word(app, 'rules.saveTo.personal', { project: 'billing-app' })])
  const addFor = await word(app, 'rules.table.addFor', { project: 'billing-app' })
  await section.getByRole('button', { name: addFor, exact: true }).click()
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  const choices = panel.getByRole('radiogroup', { name: saveTo })
  assert.ok(await choices.getByRole('radio', { name: yoursChoice }).isChecked(), 'a new row is not saved in your settings by default')
  await panel.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).fill('Harbor Bank')
  await choices.locator('label').filter({ hasText: repoChoice }).click()
  await panel.getByText(await word(app, 'rules.saveTo.repoHelp')).waitFor()
  // The file is there: written again whole, said before the save.
  await panel.getByText(await word(app, 'rules.saveTo.rewritten')).waitFor()
  assert.equal(await panel.getByText(await word(app, 'rules.saveTo.created')).count(), 0)
  await panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).click()
  await panel.waitFor({ state: 'detached' })
  const row = section.getByRole('row').filter({ hasText: 'Harbor Bank' })
  await row.waitFor()
  assert.ok((await row.innerText()).includes(await word(app, 'rules.from.repo')), 'the row saved to latet.yaml does not say so')
  const settings = await projectSettings(app, project)
  assert.ok(settings.repo.terms.some((term) => term.value === 'Harbor Bank'), 'the term was not written to latet.yaml')
  assert.ok(!(settings.personal.terms ?? []).some((term) => term.value === 'Harbor Bank'), 'the term was saved in your settings too')

  await (await projectTab(page, app, 'values')).click()
  await page.locator('#project-allow').getByRole('button', { name: addFor, exact: true }).click()
  const value = page.getByRole('dialog', { name: await word(app, 'rules.allow.new') })
  await value.waitFor()
  assert.equal(await value.getByRole('radiogroup', { name: saveTo }).count(), 0, 'values never masked offer latet.yaml')
  await value.getByRole('button', { name: await word(app, 'rules.panel.close') }).first().click()

  // A project whose file cannot be written here offers your settings alone.
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const read = api.project
    api.project = async (id, signal) => {
      const project = await read(id, signal)
      return { ...project, repo: { ...project.repo, writable: false } }
    }
  })
  await go('/rules')
  await go(`/rules?project=${encodeURIComponent(project)}#project-terms`)
  await page.locator('#project-terms').getByRole('table').waitFor()
  await page.locator('#project-terms').getByRole('button', { name: addFor, exact: true }).click()
  const readOnly = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  await readOnly.waitFor()
  assert.equal(await readOnly.getByRole('radiogroup', { name: saveTo }).count(), 0, 'latet.yaml is offered where it cannot be written')
}

/**
 * A recognizer saved to latet.yaml cannot let what it finds through: a repository file sets such a recognizer aside, so
 * its panel offers the choice for your settings for the project alone.
 */
async function repoRecognizerOffersNoLetThrough({ page, app, go }) {
  const project = await app(firstProject)
  await go(`/rules?project=${encodeURIComponent(project)}#project-recognizers`)
  const section = page.locator('#project-recognizers')
  await section.getByRole('table').waitFor()
  await section.getByRole('button', { name: await word(app, 'rules.table.addFor', { project: 'billing-app' }), exact: true }).click()
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.recognizers.new') })
  const policy = panel.getByRole('radiogroup', { name: await word(app, 'rules.recognizers.policy') })
  const allow = await word(app, 'rules.recognizers.policyAllow')
  await policy.waitFor()
  assert.equal(await policy.getByRole('radio', { name: allow }).count(), 1, 'your settings for the project do not offer it')
  await panel.getByRole('radiogroup', { name: await word(app, 'rules.saveTo.title') }).locator('label').filter({ hasText: await word(app, 'rules.saveTo.repo') }).click()
  await panel.getByText(await word(app, 'rules.saveTo.repoHelp')).waitFor()
  assert.equal(await policy.getByRole('radio', { name: allow }).count(), 0, 'latet.yaml offers to let what it finds through')
}

/** In the Repository file tab, Suggest recognizers opens a draft in the Recognizers tab's panel, latet.yaml chosen. */
async function projectSuggestionOpensInThePanel({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}#repo`)
  await page.locator('#repo').waitFor()
  await page.getByRole('button', { name: await word(app, 'projects.suggest.run'), exact: true }).click()
  await page.locator('#repo').getByRole('button', { name: await word(app, 'projects.suggest.add'), exact: true }).first().click()
  await selectedTab(page, 'recognizers', PROJECT_TABS_ID)
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.recognizers.new') })
  await panel.waitFor()
  assert.equal(await panel.getByRole('textbox', { name: await word(app, 'rules.recognizers.name'), exact: true }).inputValue(), 'aa-9999-99999')
  const choices = panel.getByRole('radiogroup', { name: await word(app, 'rules.saveTo.title') })
  assert.ok(await choices.getByRole('radio', { name: await word(app, 'rules.saveTo.repo') }).isChecked(), 'a suggestion does not go to latet.yaml')
}

/**
 * In a project as in the global rules, a change of Protection not saved yet stays, unsaved, while a term is saved from
 * its panel: the form is not made again from scratch.
 */
async function projectPanelSaveKeepsTheSaveBarEdits({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}`)
  await page.locator('#effective').waitFor()
  const protection = await projectTab(page, app, 'protection')
  await protection.click()
  await page.locator('#protection label').filter({ has: page.locator('input[value=secrets]') }).click()
  await (await projectTab(page, app, 'terms')).click()
  await page.locator('#project-terms').getByRole('button', { name: await word(app, 'rules.table.addFor', { project: 'billing-app' }), exact: true }).click()
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.terms.newValue') })
  await panel.getByRole('textbox', { name: await word(app, 'rules.terms.value'), exact: true }).fill('Fernsby Studio')
  await panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).click()
  await panel.waitFor({ state: 'detached' })
  await page.locator('#project-terms').getByRole('row').filter({ hasText: 'Fernsby Studio' }).waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await protection.getAttribute('data-dirty'), '', 'Secrets only, not saved, lost its dot')
  await protection.click()
  assert.ok(await page.locator('#protection input[value=secrets]').isChecked(), 'Secrets only, picked and not saved, was lost')
  const settings = await projectSettings(app, project)
  assert.notEqual(settings.personal.profile, 'secrets', 'Secrets only was saved with the term')
  assert.ok(settings.personal.terms.some((term) => term.value === 'Fernsby Studio'))
}

/**
 * The repository file's tab: its own row in the save bar, and no other; a change there dots that tab alone, and its
 * Save writes the file without your settings.
 */
async function projectRepoTabSavesTheFileAlone({ page, open, app }) {
  const project = await app(firstProject)
  await open(`/rules?project=${encodeURIComponent(project)}#repo`)
  await page.locator('#repo').waitFor()
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const saves = (window.latetSaves = { repo: [], personal: [] })
    const repo = api.saveRepo
    const personal = api.savePersonal
    api.saveRepo = (id, settings, tag) => {
      saves.repo.push(structuredClone(settings))
      return repo(id, settings, tag)
    }
    api.savePersonal = (id, settings, etag) => {
      saves.personal.push(structuredClone(settings))
      return personal(id, settings, etag)
    }
  })
  const saveFile = page.getByRole('button', { name: await word(app, 'projects.repo.save'), exact: true })
  assert.equal(await saveFile.isDisabled(), true, 'latet.yaml can be saved with nothing changed')
  assert.equal(await page.getByRole('button', { name: await word(app, 'projects.personal.save'), exact: true }).count(), 0, 'your settings save from the tab of the file')
  await page.locator('#repo label').filter({ has: page.locator('input[value=balanced]') }).first().click()
  assert.deepEqual(
    await page.locator('[role=tab][data-dirty]').evaluateAll((tabs) => tabs.map((tab) => tab.id)),
    [`${PROJECT_TABS_ID}-tab-repo`],
    'a change of the file dots another tab',
  )
  await saveFile.click()
  await page.waitForFunction(() => window.latetSaves.repo.length > 0)
  await page.waitForFunction(() => document.querySelectorAll('[role=tab][data-dirty]').length === 0)
  const saves = await page.evaluate(() => window.latetSaves)
  assert.equal(saves.repo.length, 1, 'more than one save of the file')
  assert.equal(saves.repo[0].profile, 'balanced', 'the profile picked was not written')
  assert.equal(saves.personal.length, 0, 'your settings were saved with the file')
}

/** The tabs of Rules, as the dashboard groups them. */
async function rulesTabs(base) {
  return (await import(`${base}/src/lib/rulesTabs.ts`)).RULES_TABS
}

/** The tabs of a project's rules, as the dashboard groups them. */
async function projectTabs(base) {
  return (await import(`${base}/src/lib/rulesTabs.ts`)).PROJECT_TABS
}

/** The ids of the tabs of a project's rules (`project-rules-tab-<id>`). */
const PROJECT_TABS_ID = 'project-rules'

/**
 * The values and the patterns never masked are one table, a Kind for each row: the search finds one, a pattern RE2
 * refuses is refused in its panel in the words of the page, a pattern is tried on a value there, and a selection is
 * deleted at once.
 */
async function allowedTableFindsAddsAndRemoves({ page, open, app }) {
  await open('/rules#allow')
  const section = page.locator('#allow')
  await section.getByRole('table').waitFor()
  const [valueWord, patternWord] = await Promise.all(['rules.allow.kindValue', 'rules.allow.kindPattern'].map((key) => word(app, key)))
  const rowOf = (text) => section.getByRole('row').filter({ hasText: text })
  for (const [text, kind] of [
    ['noreply@hayes-partners.com', valueWord],
    ['GB00TEST00000000000000', valueWord],
    ['^test-[a-z0-9]{8}@example\\.com$', patternWord],
  ]) {
    assert.ok((await rowOf(text).innerText()).includes(kind), `${text} is not a ${kind}`)
  }
  const search = section.getByRole('searchbox')
  await search.fill('gb00')
  await page.waitForFunction(() => document.querySelectorAll('#allow tbody tr').length === 1)
  await search.fill('')
  await section.getByRole('combobox', { name: await word(app, 'rules.allow.kind') }).selectOption({ label: patternWord })
  await page.waitForFunction(() => document.querySelectorAll('#allow tbody tr').length === 1)
  await section.getByRole('combobox', { name: await word(app, 'rules.allow.kind') }).selectOption({ index: 0 })

  await section.getByRole('button', { name: await word(app, 'rules.allow.add'), exact: true }).click()
  const panel = page.getByRole('dialog', { name: await word(app, 'rules.allow.new') })
  await panel.locator('label.seg-item').filter({ hasText: patternWord }).click()
  const field = panel.getByRole('textbox', { name: await word(app, 'rules.allow.column'), exact: true })
  const save = panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true })
  await field.fill('(?=dev)dev-[0-9]+')
  await panel.getByText(await word(app, 'rules.allow.re2')).waitFor()
  assert.equal(await save.isDisabled(), true, 'a pattern RE2 refuses can be saved')
  await field.fill('dev-[0-9]+')
  const tryIt = panel.getByRole('textbox', { name: await word(app, 'rules.allow.try.label'), exact: true })
  await tryIt.fill('dev-42')
  await panel.getByText(await word(app, 'rules.allow.try.matches')).waitFor()
  await tryIt.fill('my dev-42')
  await panel.getByText(await word(app, 'rules.allow.try.noMatch')).waitFor()
  // What RE2 alone reads gives no verdict here, rather than a wrong one; a class of letters reads as RE2 reads it.
  await field.fill('\\Aabc\\z')
  await tryIt.fill('abc')
  await panel.getByText(await word(app, 'rules.allow.try.cannot')).waitFor()
  await field.fill('\\p{L}+ Test')
  await tryIt.fill('José Test')
  await panel.getByText(await word(app, 'rules.allow.try.matches')).waitFor()
  await field.fill('dev-[0-9]+')
  await save.click()
  await panel.waitFor({ state: 'detached' })
  await rowOf('dev-[0-9]+').waitFor()
  const saved = async () =>
    app(async (base) => {
      const config = await (await import(`${base}/src/api/client.ts`)).api.config()
      return { values: config.allow_values, patterns: config.allow_patterns }
    })
  assert.deepEqual((await saved()).patterns, ['^test-[a-z0-9]{8}@example\\.com$', 'dev-[0-9]+'])

  for (const name of ['noreply@hayes-partners.com', 'dev-[0-9]+']) await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: name }) }).check()
  const del = await word(app, 'rules.table.delete')
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  await page.getByRole('dialog', { name: del }).getByRole('button', { name: del, exact: true }).click()
  await rowOf('dev-[0-9]+').waitFor({ state: 'detached' })
  assert.deepEqual(await saved(), { values: ['GB00TEST00000000000000'], patterns: ['^test-[a-z0-9]{8}@example\\.com$'] })
}

/**
 * The policies are a table, a type each: Add offers the types Latet knows (its own, the common ones, those of your
 * terms and recognizers) but those set already; a type added, its action changed, and a row deleted, each saved at once.
 */
async function criteriaTableAddsAType({ page, open, app }) {
  await open('/rules#policies')
  const section = page.locator('#policies')
  await section.getByRole('table').waitFor()
  const rowOf = (type) => section.getByRole('row').filter({ hasText: type })
  const [redact, mask, save] = await Promise.all(['rules.policies.redact', 'rules.policies.mask', 'rules.panel.save'].map((key) => word(app, key)))
  assert.ok((await rowOf('PRIVATE_KEY').innerText()).includes(redact), 'PRIVATE_KEY is not redacted')

  await section.getByRole('button', { name: await word(app, 'rules.policies.add'), exact: true }).click()
  const adding = page.getByRole('dialog', { name: await word(app, 'rules.policies.new') })
  const picker = adding.getByRole('combobox', { name: await word(app, 'rules.policies.type'), exact: true })
  const offered = await picker.locator('option').evaluateAll((options) => options.map((option) => option.value).filter(Boolean))
  for (const type of ['EMAIL', 'PASSWORD', 'PROJECT', 'CLIENT', 'EMPLOYEE_ID']) assert.ok(offered.includes(type), `${type} is not offered`)
  for (const type of ['PRIVATE_KEY', 'IP_ADDRESS']) assert.ok(!offered.includes(type), `${type}, set already, is offered again`)
  await picker.selectOption('EMAIL')
  await adding.locator('label.seg-item').filter({ hasText: redact }).click()
  await adding.getByRole('button', { name: save, exact: true }).click()
  await adding.waitFor({ state: 'detached' })
  await rowOf('EMAIL').waitFor()
  const policies = async () => app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.config()).policies)
  assert.deepEqual(await policies(), { PRIVATE_KEY: 'redact', IP_ADDRESS: 'allow', EMAIL: 'redact' })

  // A type Latet detects but does not list (a secret rule's, seen in an exchange) is typed.
  await section.getByRole('button', { name: await word(app, 'rules.policies.add'), exact: true }).click()
  await adding.getByRole('combobox', { name: await word(app, 'rules.policies.type'), exact: true }).selectOption({ label: await word(app, 'rules.policies.other') })
  const typed = adding.getByRole('textbox', { name: await word(app, 'rules.policies.typeOther'), exact: true })
  await typed.fill('9x')
  await adding.getByText(await word(app, 'rules.recognizers.problems.type')).waitFor()
  assert.equal(await adding.getByRole('button', { name: save, exact: true }).isDisabled(), true, 'a type that is not one can be saved')
  await typed.fill('slack_webhook_url')
  assert.equal(await typed.inputValue(), 'SLACK_WEBHOOK_URL')
  await adding.getByRole('button', { name: save, exact: true }).click()
  await adding.waitFor({ state: 'detached' })
  await rowOf('SLACK_WEBHOOK_URL').waitFor()
  assert.equal((await policies()).SLACK_WEBHOOK_URL, 'redact')

  await rowOf('IP_ADDRESS').click()
  const editing = page.getByRole('dialog', { name: 'IP_ADDRESS' })
  assert.equal(await editing.getByRole('combobox').count(), 0, "an existing row's type can be changed")
  await editing.locator('label.seg-item').filter({ hasText: mask }).click()
  await editing.getByRole('button', { name: save, exact: true }).click()
  await editing.waitFor({ state: 'detached' })
  assert.deepEqual(await policies(), { PRIVATE_KEY: 'redact', IP_ADDRESS: 'mask', EMAIL: 'redact', SLACK_WEBHOOK_URL: 'redact' })

  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'PRIVATE_KEY' }) }).check()
  const del = await word(app, 'rules.table.delete')
  await section.getByRole('toolbar').getByRole('button', { name: del, exact: true }).click()
  await page.getByRole('dialog', { name: del }).getByRole('button', { name: del, exact: true }).click()
  await rowOf('PRIVATE_KEY').waitFor({ state: 'detached' })
  assert.deepEqual(await policies(), { IP_ADDRESS: 'mask', EMAIL: 'redact', SLACK_WEBHOOK_URL: 'redact' })
}

/**
 * The recognizers are a table: a row shows the name, the type, what it finds (the pattern, cut short), the action and
 * how it loaded; opened in the panel, its pattern is changed, tried on a text and saved; two are selected and turned off
 * at once. A recognizer drafted from a description opens in the panel, new, for you to check and save.
 */
async function recognizersTableEditsInThePanel({ page, app, go }) {
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changeConfig((config) => ({
      recognizers: [...config.recognizers, { name: 'ticket-ref', type: 'TICKET', pattern: '\\bTK-\\d{5}\\b', policy: 'redact', class: 'secret' }],
    }))
  })
  await go('/rules#recognizers')
  const section = page.locator('#recognizers')
  await section.getByRole('table').waitFor()
  const rowOf = (name) => section.getByRole('row').filter({ hasText: name })
  const employee = await rowOf('employee-id').innerText()
  for (const shown of ['EMPLOYEE_ID', '\\b[A-Z]{2}-\\d{6}\\b', await word(app, 'rules.recognizers.policyDefault'), await word(app, 'rules.recognizers.state.active')]) {
    assert.ok(employee.includes(shown), `the employee-id row does not show ${shown}: ${employee}`)
  }
  assert.ok((await rowOf('ticket-ref').innerText()).includes(await word(app, 'rules.recognizers.policyRedact')), 'ticket-ref does not say it redacts')

  await rowOf('employee-id').click()
  const panel = page.getByRole('dialog', { name: 'employee-id' })
  const pattern = panel.getByRole('textbox', { name: await word(app, 'rules.recognizers.pattern'), exact: true })
  assert.equal(await pattern.count(), 1)
  await pattern.fill('\\b[A-Z]{2}-\\d{7}\\b')
  await panel.getByRole('textbox', { name: await word(app, 'rules.recognizers.try.placeholder') }).fill('badge AB-1234567 at the gate')
  await panel.getByRole('button', { name: await word(app, 'rules.recognizers.try.button'), exact: true }).click()
  await panel.locator('mark').filter({ hasText: 'AB-1234567' }).waitFor()
  await panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true }).click()
  await panel.waitFor({ state: 'detached' })
  const recognizers = async () => app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.config()).recognizers)
  assert.equal((await recognizers())[0].pattern, '\\b[A-Z]{2}-\\d{7}\\b', 'the pattern changed in the panel was not saved')

  for (const name of ['employee-id', 'ticket-ref']) await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: name }) }).check()
  const off = await word(app, 'rules.recognizers.turnOff')
  await section.getByRole('toolbar').getByRole('button', { name: off, exact: true }).click()
  await page.getByRole('dialog', { name: off }).getByRole('button', { name: off, exact: true }).click()
  await page.waitForFunction(() => document.querySelectorAll('#recognizers [role=toolbar]').length === 0)
  const offWord = await word(app, 'rules.recognizers.state.off')
  // employee-id's examples now fail (its pattern wants seven digits): its row says so before it says off.
  await page.waitForFunction((word) => document.querySelector('#recognizers tbody tr[data-key="global:recognizer:ticket-ref"]')?.textContent.includes(word), offWord)
  const saved = await recognizers()
  assert.deepEqual(
    saved.map((entry) => [entry.name, entry.enabled]),
    [
      ['employee-id', false],
      ['ticket-ref', false],
    ],
  )
  assert.equal(saved[1].class, 'secret', 'turning a recognizer off lost what else it holds')

  await section.getByRole('textbox', { name: await word(app, 'rules.recognizers.draft.title') }).fill('Badge numbers: BD, a dash and six digits')
  await section.getByRole('button', { name: await word(app, 'rules.recognizers.draft.run'), exact: true }).click()
  const drafted = page.getByRole('dialog', { name: await word(app, 'rules.recognizers.new') })
  await drafted.waitFor()
  assert.equal(await drafted.getByRole('textbox', { name: await word(app, 'rules.recognizers.name'), exact: true }).inputValue(), 'badge-number')
  assert.equal((await recognizers()).length, 2, 'a draft was saved before it was checked')
}

/**
 * A list of terms in a CSV: Import reads it in the browser, the daemon's import shows what it would add (a line with a
 * problem said by its number, a term there already skipped), and Import adds them, replacing none. Export writes the
 * rows the table shows, filtered, and importing that file back adds nothing.
 */
async function termsImportPreviewsThenAdds({ page, open, app }) {
  await open('/rules#terms')
  const section = page.locator('#terms')
  await section.getByRole('table').waitFor()
  const csv = 'value,type\nHarbor Bank,CLIENT\nProject Aurora,PROJECT\n"Smith, Jones",CLIENT\n--\n'
  await section.locator('input[type=file]').setInputFiles({ name: 'clients.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) })
  const dialog = page.getByRole('dialog', { name: await word(app, 'transfer.title') })
  await dialog.waitFor()
  const preview = await dialog.innerText()
  assert.ok(preview.includes('Harbor Bank') && preview.includes('Smith, Jones'), `the preview does not say what it adds: ${preview}`)
  assert.ok(preview.includes(await word(app, 'rules.import.line', { line: 5, reason: await word(app, 'rules.import.reason.noLetterOrDigit') })), `the line with signs alone is not said: ${preview}`)
  await dialog.getByRole('button', { name: await word(app, 'transfer.apply'), exact: true }).click()
  await dialog.waitFor({ state: 'detached' })
  await section.getByRole('row').filter({ hasText: 'Smith, Jones' }).waitFor()
  assert.deepEqual(await savedTerms(app), ['Project Aurora', 'Whitfield & Co', 'Harbor Bank', 'Smith, Jones'], 'the import replaced a term, or did not add')

  // Export the CLIENT terms alone, then import that file: nothing new.
  await section.getByRole('combobox', { name: await word(app, 'rules.terms.type'), exact: true }).selectOption('CLIENT')
  await page.waitForFunction(() => document.querySelectorAll('#terms tbody tr').length === 3)
  const [download] = await Promise.all([page.waitForEvent('download'), section.getByRole('button', { name: await word(app, 'rules.export.button'), exact: true }).click()])
  const chunks = []
  for await (const chunk of await download.createReadStream()) chunks.push(chunk)
  const exported = Buffer.concat(chunks).toString('utf8')
  assert.equal(exported, 'value,type,case_sensitive,whole_word\r\nWhitfield & Co,CLIENT,false,true\r\nHarbor Bank,CLIENT,false,true\r\n"Smith, Jones",CLIENT,false,true\r\n')
  await section.locator('input[type=file]').setInputFiles({ name: download.suggestedFilename(), mimeType: 'text/csv', buffer: Buffer.from(exported) })
  await dialog.waitFor()
  await dialog.getByText(await word(app, 'transfer.nothingNew')).waitFor()
}

/** Recognizers export as a rules file the daemon reads back; the values never masked import from a CSV, held back
 * until you opt in, as what lowers protection always is. */
async function listsExportAndImportTheirOwnFile({ page, open, app }) {
  await open('/rules#recognizers')
  const recognizers = page.locator('#recognizers')
  await recognizers.getByRole('table').waitFor()
  const [download] = await Promise.all([page.waitForEvent('download'), recognizers.getByRole('button', { name: await word(app, 'rules.export.button'), exact: true }).click()])
  const chunks = []
  for await (const chunk of await download.createReadStream()) chunks.push(chunk)
  const file = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  assert.equal(file.latet_rules, 1)
  assert.deepEqual(file.recognizers.map((entry) => entry.name), ['employee-id'])
  assert.ok(download.suggestedFilename().endsWith('.yaml'), download.suggestedFilename())
  // A colleague's whole rules file is not imported from the Recognizers table, which would add its other lists too.
  const whole = 'latet_rules: 1\nterms:\n  - value: Acme\n    type: CLIENT\nrecognizers:\n  - name: badge-id\n    pattern: B-[0-9]{6}\npolicies:\n  EMAIL: allow\n'
  await recognizers.locator('input[type=file]').setInputFiles({ name: 'colleague.yaml', mimeType: 'application/yaml', buffer: Buffer.from(whole) })
  await page.getByText(await word(app, 'rules.import.otherLists', { name: 'colleague.yaml', lists: 'terms, policies' })).waitFor()
  assert.equal(await page.getByRole('dialog', { name: await word(app, 'transfer.title') }).count(), 0, 'a whole rules file was offered for import from the Recognizers table')

  await (await rulesTab(page, app, 'values')).click()
  const values = page.locator('#allow')
  await values.locator('input[type=file]').setInputFiles({ name: 'values.csv', mimeType: 'text/csv', buffer: Buffer.from('kind,value\nvalue,acme-dev\npattern,^ci-[0-9]+$\n') })
  const dialog = page.getByRole('dialog', { name: await word(app, 'transfer.title') })
  await dialog.waitFor()
  const apply = dialog.getByRole('button', { name: await word(app, 'transfer.apply'), exact: true })
  assert.equal(await apply.isDisabled(), true, 'values never masked are imported without opting in')
  await dialog.locator('label').filter({ hasText: await word(app, 'transfer.everything') }).click()
  await apply.click()
  await dialog.waitFor({ state: 'detached' })
  await values.getByRole('row').filter({ hasText: 'acme-dev' }).waitFor()
  const saved = await app(async (base) => {
    const config = await (await import(`${base}/src/api/client.ts`)).api.config()
    return [config.allow_values, config.allow_patterns]
  })
  assert.deepEqual(saved, [
    ['noreply@hayes-partners.com', 'GB00TEST00000000000000', 'acme-dev'],
    ['^test-[a-z0-9]{8}@example\\.com$', '^ci-[0-9]+$'],
  ])
}

/**
 * A panel saves only over the row it found: a recognizer opened and edited, then turned off through a selection while
 * its panel stays open, is not turned back on by the panel's Save. The panel says its row changed and shows it as it
 * is now; a panel without edits follows the change without a word.
 */
async function recognizerPanelKeepsAChangeMadeMeanwhile({ page, app, go }) {
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changeConfig((config) => ({ recognizers: [...config.recognizers, { name: 'ticket-ref', type: 'TICKET', pattern: '\\bTK-\\d{5}\\b' }] }))
  })
  await go('/rules#recognizers')
  const section = page.locator('#recognizers')
  await section.getByRole('table').waitFor()
  await section.getByRole('row').filter({ hasText: 'ticket-ref' }).click()
  const panel = page.getByRole('dialog', { name: 'ticket-ref' })
  const pattern = panel.getByRole('textbox', { name: await word(app, 'rules.recognizers.pattern'), exact: true })
  await pattern.fill('\\bTK-\\d{6}\\b')
  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'ticket-ref' }) }).check()
  const off = await word(app, 'rules.recognizers.turnOff')
  await section.getByRole('toolbar').getByRole('button', { name: off, exact: true }).click()
  await page.getByRole('dialog', { name: off }).getByRole('button', { name: off, exact: true }).click()
  await panel.getByText(await word(app, 'rules.panel.changed')).waitFor()
  assert.equal(await pattern.inputValue(), '\\bTK-\\d{5}\\b', 'the panel does not show the row as it is now')
  const save = panel.getByRole('button', { name: await word(app, 'rules.panel.save'), exact: true })
  if (!(await save.isDisabled())) await save.click()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  const saved = await app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.config()).recognizers.find((entry) => entry.name === 'ticket-ref'))
  assert.equal(saved.enabled, false, 'the panel turned the recognizer back on')
  assert.equal(saved.pattern, '\\bTK-\\d{5}\\b', 'the edit made before the change was saved over it')

  // A panel without edits follows the row: turned on again by a selection, it shows it on.
  await section.getByRole('checkbox', { name: await word(app, 'rules.table.select', { row: 'ticket-ref' }) }).check()
  const on = await word(app, 'rules.recognizers.turnOn')
  await section.getByRole('toolbar').getByRole('button', { name: on, exact: true }).click()
  await page.getByRole('dialog', { name: on }).getByRole('button', { name: on, exact: true }).click()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal(await panel.getByText(await word(app, 'rules.panel.changed')).count(), 0, 'a panel without edits says its row changed')
  await panel.locator('summary').click()
  assert.ok(await panel.getByRole('switch', { name: await word(app, 'rules.recognizers.enabled'), exact: true }).isChecked(), 'the panel did not follow the row')
}

/**
 * A list's Export keeps the guards of the whole export: a recognizer whose examples hold a value Latet has seen for real
 * is left out, and said; the terms' CSV says they are in it in clear.
 */
async function listExportKeepsTheExportGuards({ page, app, go }) {
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.changeConfig((config) => ({
      recognizers: [...config.recognizers, { name: 'leaky', pattern: '\\bDaniel \\w+\\b', examples: { match: ['Daniel Hayes'] } }],
    }))
  })
  await go('/rules#recognizers')
  const section = page.locator('#recognizers')
  await section.getByRole('row').filter({ hasText: 'leaky' }).waitFor()
  const [download] = await Promise.all([page.waitForEvent('download'), section.getByRole('button', { name: await word(app, 'rules.export.button'), exact: true }).click()])
  const chunks = []
  for await (const chunk of await download.createReadStream()) chunks.push(chunk)
  const file = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  assert.deepEqual(file.recognizers.map((entry) => entry.name), ['employee-id'], 'a recognizer whose examples are real values was exported')
  await page.getByText('recognizer leaky: left out, its examples hold values Latet has seen for real').waitFor()

  await (await rulesTab(page, app, 'terms')).click()
  const terms = page.locator('#terms')
  const [csv] = await Promise.all([page.waitForEvent('download'), terms.getByRole('button', { name: await word(app, 'rules.export.button'), exact: true }).click()])
  await page.getByText(await word(app, 'transfer.exported', { name: csv.suggestedFilename() })).waitFor()
}

/** The Playground masks a text when you ask, with its button, never as you type: what you type after shows dimmed. */
async function playgroundMasksOnTheButton({ page, open, app }) {
  await open('/playground')
  const input = page.getByRole('textbox', { name: await word(app, 'playground.input') })
  await input.fill('Write to ann.ross@acme.com about Orion')
  await page.waitForTimeout(1200)
  const result = page.locator('section[aria-labelledby="playground-result"]')
  const before = await result.innerText()
  assert.ok(before.includes(await word(app, 'playground.empty.title')), `it masked as you typed: ${before.slice(0, 200)}`)
  await page.getByRole('button', { name: await word(app, 'playground.mask') }).click()
  await result.getByText(await word(app, 'playground.masked'), { exact: true }).waitFor()
  await input.fill('Write to bob.lee@acme.com')
  await page.waitForTimeout(1200)
  assert.ok((await result.innerText()).includes(await word(app, 'playground.stale')), 'a text changed since is not said stale')
}

/**
 * The Playground offers the kinds of text that mask differently, each said under the menu, and tries a text as another
 * profile would mask it: Secrets only leaves an email in clear that your rules mask.
 */
async function playgroundTriesAnotherProfile({ page, open, app }) {
  await open('/playground')
  const kinds = page.getByRole('combobox', { name: await word(app, 'playground.kind') })
  assert.equal(await kinds.locator('option').count(), 4, 'the kinds that mask alike are not left out')
  await kinds.selectOption('tool_result')
  assert.ok((await page.locator('main').innerText()).includes(await word(app, 'playground.kindHelps.tool_result')), 'the kind chosen is not said')
  const input = page.getByRole('textbox', { name: await word(app, 'playground.input') })
  await input.fill('write to ann.ross@acme.com')
  const result = page.locator('section[aria-labelledby="playground-result"]')
  const mask = page.getByRole('button', { name: await word(app, 'playground.mask') })
  await mask.click()
  await result.getByText(await word(app, 'playground.found', { count: 1 })).waitFor()
  // What will be sent: the text after the result's "Masked" heading.
  const sent = async () => (await result.innerText()).split(await word(app, 'playground.masked')).at(-1) ?? ''
  assert.ok(!(await sent()).includes('ann.ross@acme.com'), 'your rules leave the email in clear')
  await page.getByRole('combobox', { name: await word(app, 'playground.profile') }).selectOption('secrets')
  await mask.click()
  await result.getByText(await word(app, 'playground.nothingFound')).waitFor()
  assert.ok((await sent()).includes('ann.ross@acme.com'), 'Secrets only masks the email')
}

async function word(app, key, options) {
  return app(async (base, key, options) => (await import(`${base}/src/i18n.ts`)).default.t(key, options), key, options ?? {})
}

/** The tab of Rules `id` (`terms`), by its name in the tablist. */
async function rulesTab(page, app, id) {
  const list = page.getByRole('tablist', { name: await word(app, 'rules.tabs.label') })
  return list.getByRole('tab', { name: await word(app, `rules.tabs.${id}`) })
}

/** The tab of a project's rules `id` (`repo`), by its name in the tablist. */
async function projectTab(page, app, id) {
  const list = page.getByRole('tablist', { name: await word(app, 'rules.tabs.label') })
  return list.getByRole('tab', { name: await word(app, id === 'overview' || id === 'repo' ? `projects.tabs.${id}` : `rules.tabs.${id}`) })
}

/** The one selected tab is `id`, and its panel shows (`prefix`: of the ids of the page's tabs). */
async function selectedTab(page, id, prefix = 'rules') {
  await page.waitForFunction(([tab, ids]) => document.querySelector('[role=tab][aria-selected=true]')?.id === `${ids}-tab-${tab}`, [id, prefix])
  assert.equal(await page.locator('[role=tab][aria-selected=true]').count(), 1)
  assert.equal(await page.locator(`#${prefix}-panel-${id}`).isVisible(), true, `the panel of ${id} is hidden`)
}

// ---------------------------------------------------------------------------------------------------------------
// Approvals in two tabs (the plan's Task 5). The mock starts with an empty queue (?mock-approvals-empty) and a check
// puts in it what it needs, as an agent would: the texts and the change to the settings arrive through the stream.

/** A text waiting for approval: `n` tells them apart, `project` is its project's id (null: none), `age` its seconds. */
function waitingText(n, { project = null, age = 60 } = {}) {
  const original = `Text ${n}: ask Emily Carter about invoice 2026/11${n}.`
  const name = original.indexOf('Emily Carter')
  return {
    id: `ap_check${n}`,
    session: `x-session-id:check-000${n}`,
    project,
    agent: 'claude-code',
    created: Date.now() / 1000 - age,
    expires: null,
    items: [
      {
        kind: 'user_text',
        path: null,
        tool: null,
        original,
        masked: original,
        replacements: [],
        suggestions: [{ start: name, end: name + 'Emily Carter'.length, category: 'person' }],
        failed: null,
      },
    ],
  }
}

/** A change to your settings that lowers protection, waiting for you. */
function waitingChange() {
  return {
    id: 'ch_check',
    created: Date.now() / 1000 - 30,
    source: 'command',
    agent: 'hermes',
    loosened: ['values never to mask: 1 added'],
    keys: ['allow_values'],
  }
}

/** Puts texts in the mock's queue, as if agents had sent them. */
async function addTexts(app, texts) {
  await app(async (base, texts) => {
    const { addBatch } = await import(`${base}/src/mock/approvals.ts`)
    for (const text of texts) addBatch(text)
  }, texts)
}

/** Puts a change to your settings in the mock's queue. */
async function addSettingsChange(app) {
  await app(async (base, change) => {
    const { addChange } = await import(`${base}/src/mock/approvals.ts`)
    addChange(change, () => undefined)
  }, waitingChange())
}

/** Opens the dashboard on an empty queue, puts in it what `fill` adds, then goes to Approvals (the queue as it was). */
async function approvalsWith({ page, open, app, go }, fill, path = '/approvals') {
  await open('/live?mock-approvals-empty')
  await fill()
  await go(path)
  if (!path.startsWith('/approvals')) return
  await page.getByRole('heading', { name: await word(app, 'approvals.title'), level: 1 }).waitFor()
  // The queue loaded (the stream's first snapshot): the tabs have their counts.
  await page.waitForFunction(() => /\d$/.test(document.querySelector('#approvals-tab-texts')?.textContent?.trim() ?? ''))
}

/** The tab of Approvals `id` (`texts`, `settings`). */
function approvalsTab(page, id) {
  return page.locator(`#approvals-tab-${id}`)
}

/** The words of a tab, its count included ("Texts 2"). */
async function tabWords(tab) {
  return (await tab.textContent()).replace(/\s+/g, ' ').trim()
}

/** How a tab's count is drawn: `warm` in the warm chip's colours (`--real-low`, `--real-ink`), else `neutral`. */
async function countTone(tab) {
  return tab.evaluate((element) => {
    const chip = element.querySelector('.tabular-nums')
    const probe = document.createElement('span')
    probe.style.background = 'var(--real-low)'
    probe.style.color = 'var(--real-ink)'
    document.body.append(probe)
    const [warm, seen] = [getComputedStyle(probe), getComputedStyle(chip)]
    const warmColours = seen.backgroundColor === warm.backgroundColor && seen.color === warm.color
    probe.remove()
    return chip.dataset.tone === 'warm' && warmColours ? 'warm' : !chip.dataset.tone && !warmColours ? 'neutral' : 'mixed'
  })
}

/** The tab `id` is the one selected, and its panel shows. */
async function approvalsSelected(page, id) {
  await page.waitForFunction((tab) => document.querySelector('[role=tab][aria-selected=true]')?.id === `approvals-tab-${tab}`, id)
  assert.equal(await page.locator('[role=tab][aria-selected=true]').count(), 1)
  assert.equal(await page.locator(`#approvals-panel-${id}`).isVisible(), true, `the panel of ${id} is hidden`)
}

/**
 * Two texts and a change to the settings waiting: the tabs read "Texts 2" and "Settings 1" and Texts is selected.
 * The change's card shows in Settings only, and Approve all in Texts only.
 */
async function approvalsTabsCountAndOpenOnTexts(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, async () => {
    await addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)])
    await addSettingsChange(app)
  })
  const [texts, settings, label] = await Promise.all(['texts', 'settings', 'label'].map((id) => word(app, `approvals.tabs.${id}`)))
  assert.equal(await page.getByRole('tablist', { name: label }).count(), 1, 'Approvals has no tablist named for its tabs')
  assert.equal(await tabWords(approvalsTab(page, 'texts')), `${texts} 2`)
  assert.equal(await tabWords(approvalsTab(page, 'settings')), `${settings} 1`)
  // A change that lowers protection waits: its count is warm, the texts' neutral.
  assert.equal(await countTone(approvalsTab(page, 'settings')), 'warm', 'the count of Settings is not warm with a change waiting')
  assert.equal(await countTone(approvalsTab(page, 'texts')), 'neutral')
  await approvalsSelected(page, 'texts')
  const approveAll = page.getByRole('button', { name: await word(app, 'approvals.approveAll', { count: 2 }) })
  const card = page.locator('[data-settings-change]')
  assert.equal(await approveAll.isVisible(), true, 'Approve all is not in Texts')
  assert.equal(await card.isVisible(), false, 'the change to the settings shows in Texts')
  assert.equal(await page.getByRole('listbox', { name: await word(app, 'approvals.queue.label') }).isVisible(), true)
  await approvalsTab(page, 'settings').click()
  await approvalsSelected(page, 'settings')
  assert.equal(await card.isVisible(), true, 'the change to the settings is not in Settings')
  assert.equal(await approveAll.count(), 0, 'Approve all shows in Settings')
  assert.equal(await page.getByRole('region', { name: await word(app, 'approvals.panes.original') }).count(), 0, 'a text shows in Settings')
}

/** No text waits and a change to the settings does: Approvals opens on Settings. */
async function approvalsOpensOnSettingsWithNoTexts(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () => addSettingsChange(app))
  await approvalsSelected(page, 'settings')
  assert.equal(await tabWords(approvalsTab(page, 'texts')), `${await word(app, 'approvals.tabs.texts')} 0`)
  await page.locator('[data-settings-change]').waitFor()
}

/** A change to the settings arriving while you review texts: the Settings count goes up, the Texts tab stays. */
async function approvalsChangeArrivingKeepsTexts(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)]))
  await approvalsSelected(page, 'texts')
  const settings = approvalsTab(page, 'settings')
  assert.equal(await tabWords(settings), `${await word(app, 'approvals.tabs.settings')} 0`)
  assert.equal(await countTone(settings), 'neutral', 'the count of Settings is warm with nothing waiting')
  await addSettingsChange(app)
  await page.waitForFunction(() => /1$/.test(document.querySelector('#approvals-tab-settings')?.textContent?.trim() ?? ''))
  assert.equal(await countTone(settings), 'warm', 'the count of Settings did not turn warm as a change arrived')
  await page.waitForTimeout(MOCK_SETTLE_MS)
  await approvalsSelected(page, 'texts')
  assert.equal(await page.locator('[data-settings-change]').isVisible(), false, 'the change showed over the texts')
}

/** Rules says a change waits; its Review it opens Approvals on Settings, even with texts waiting; a tab picked stays. */
async function approvalsReviewLinkOpensSettings(helpers) {
  const { page, app } = helpers
  await approvalsWith(
    helpers,
    async () => {
      await addTexts(app, [waitingText(1)])
      await addSettingsChange(app)
    },
    '/rules',
  )
  await page.getByRole('link', { name: await word(app, 'approvals.change.review') }).click()
  await approvalsSelected(page, 'settings')
  assert.equal(await page.evaluate(() => window.location.hash), '#settings')
  await approvalsTab(page, 'texts').click()
  await approvalsSelected(page, 'texts')
  assert.equal(await page.evaluate(() => window.location.hash), '#texts', 'the address does not name the tab picked')
}

// ---------------------------------------------------------------------------------------------------------------
// One text at a time below 1100 px (the plan's Task 6).

/** The id of a project of the mock. */
async function firstProject(base) {
  const { api } = await import(`${base}/src/api/client.ts`)
  return (await api.projects()).items[0].id
}

/** Two texts waiting, of two projects (so the project menu shows), the first the older. */
async function twoTextsOfTwoProjects(app) {
  const project = await app(firstProject)
  await addTexts(app, [waitingText(1, { age: 90 }), waitingText(2, { project })])
}

/** The text shown: its original's words. */
async function shownOriginal(page, app) {
  return page.getByRole('region', { name: await word(app, 'approvals.panes.original'), exact: true }).innerText()
}

/** The narrow layout shows text `number` of `count`. */
async function showsPosition(page, app, number, count) {
  const words = await word(app, 'approvals.narrow.position', { number, count })
  await page.getByRole('status').filter({ hasText: new RegExp(`^${words}$`) }).waitFor()
}

/**
 * In a window `width` px wide: the counter "1 of 2", the project menu and one text, its original and what will be sent
 * one above the other, and the action bar at the bottom with Approve, Don't send, the previous and the next text.
 * Previous and next move through the queue; approving the text shown shows the next; approving the last leaves the
 * empty state, with no action bar.
 */
async function narrowWalk(helpers, width) {
  const { page, app } = helpers
  await page.setViewportSize({ width, height: 844 })
  await approvalsWith(helpers, () => twoTextsOfTwoProjects(app))
  const [batchLabel, queueLabel, project, approve, reject, previous, next, empty] = await Promise.all([
    word(app, 'approvals.batch.label'),
    word(app, 'approvals.queue.label'),
    word(app, 'approvals.filter.label'),
    word(app, 'approvals.approve'),
    word(app, 'approvals.reject'),
    word(app, 'approvals.narrow.previous'),
    word(app, 'approvals.narrow.next'),
    word(app, 'approvals.empty.title'),
  ])
  await showsPosition(page, app, 1, 2)
  assert.ok((await shownOriginal(page, app)).includes('Text 1:'), 'the oldest text is not the one shown')
  assert.equal(await page.getByRole('combobox', { name: project }).isVisible(), true, 'no project menu')
  assert.equal(await page.getByRole('region', { name: batchLabel }).count(), 1, 'more than one text shows')
  assert.equal(await page.getByRole('listbox', { name: queueLabel }).count(), 0, 'the queue is listed in a narrow window')

  const bar = page.locator('.actionbar')
  assert.equal(await bar.count(), 1, 'no action bar')
  assert.equal(await bar.evaluate((element) => getComputedStyle(element).position), 'sticky')
  for (const name of [approve, reject, previous, next]) {
    assert.equal(await bar.getByRole('button', { name, exact: true }).isVisible(), true, `no ${name} in the action bar`)
  }
  const [original, sent] = await Promise.all(
    ['approvals.panes.original', 'approvals.panes.sent'].map(async (key) =>
      page.getByRole('region', { name: await word(app, key), exact: true }).boundingBox(),
    ),
  )
  assert.ok(Math.abs(original.x - sent.x) <= 1 && sent.y >= original.y + original.height - 1, `the original and what will be sent are side by side (${JSON.stringify({ original, sent })})`)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'the page scrolls sideways')

  assert.equal(await bar.getByRole('button', { name: previous, exact: true }).isDisabled(), true, 'Previous before the first text')
  await bar.getByRole('button', { name: next, exact: true }).click()
  await showsPosition(page, app, 2, 2)
  assert.ok((await shownOriginal(page, app)).includes('Text 2:'))
  assert.equal(await bar.getByRole('button', { name: next, exact: true }).isDisabled(), true, 'Next after the last text')
  // The bar of the text shown is drawn anew: the focus stays on a step button (Next is off at the end: Previous).
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), previous, 'the focus left the step buttons')
  await bar.getByRole('button', { name: previous, exact: true }).click()
  await showsPosition(page, app, 1, 2)
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), next, 'the focus left the step buttons')

  await lookAtIt(page, app)
  await bar.getByRole('button', { name: approve, exact: true }).click()
  await showsPosition(page, app, 1, 1)
  assert.ok((await shownOriginal(page, app)).includes('Text 2:'), 'the next text does not show once the first is approved')
  await lookAtIt(page, app)
  await bar.getByRole('button', { name: approve, exact: true }).click()
  await page.getByRole('heading', { name: empty }).waitFor()
  assert.equal(await page.locator('.actionbar').count(), 0, 'the action bar stays with nothing to decide')
}

/** Half a 1920 px screen (960 px): one text at a time. */
async function approvalsNarrowAt960({ page, ...helpers }) {
  await narrowWalk({ page, ...helpers }, 960)
}

/** A phone (390 px): one text at a time, nothing wider than the window. */
async function approvalsNarrowAt390({ page, ...helpers }) {
  await narrowWalk({ page, ...helpers }, 390)
}

/** The text shown is approved by another viewer: the next one shows. */
async function approvalsNarrowNextWhenDecidedElsewhere(helpers) {
  const { page, app } = helpers
  await page.setViewportSize({ width: 960, height: 844 })
  await approvalsWith(helpers, () => twoTextsOfTwoProjects(app))
  await showsPosition(page, app, 1, 2)
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    await api.decide('ap_check1', { action: 'approve', masks: [] })
  })
  await showsPosition(page, app, 1, 1)
  assert.ok((await shownOriginal(page, app)).includes('Text 2:'), 'the next text does not show once another viewer approved the first')
}

/** At 1440 px, as before: the queue beside the text, the panes side by side, and no action bar. */
async function approvalsWideKeepsTheQueue(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () => twoTextsOfTwoProjects(app))
  await page.getByRole('listbox', { name: await word(app, 'approvals.queue.label') }).waitFor()
  assert.equal(await page.locator('.actionbar').count(), 0, 'the action bar shows in a wide window')
  const [original, sent] = await Promise.all(
    ['approvals.panes.original', 'approvals.panes.sent'].map(async (key) =>
      page.getByRole('region', { name: await word(app, key), exact: true }).boundingBox(),
    ),
  )
  assert.ok(sent.x > original.x + original.width - 1, 'the original and what will be sent are not side by side')
}

// ---------------------------------------------------------------------------------------------------------------
// Keyboard shortcuts, off until turned on (the plan's Task 7).

/** The ids of the texts waiting in the mock's queue. */
async function waitingIds(app) {
  return app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    return (await api.approvals()).batches.map((batch) => batch.id)
  })
}

/** The text shown: its number, from the original. */
async function selectedText(page, app) {
  const original = await shownOriginal(page, app)
  return /^Text (\d+):/.exec(original)?.[1] ?? null
}

/** Waits for text `number` to be the one shown. */
async function showsText(page, app, number) {
  const label = await word(app, 'approvals.panes.original')
  await page.waitForFunction(
    ([label, start]) => document.querySelector(`[role=region][aria-label="${label}"]`)?.textContent?.startsWith(start),
    [label, `Text ${number}:`],
  )
}

/** Waits for the texts waiting in the mock's queue to be `ids`. */
async function waitingAre(app, ids, message, timeout = 5000) {
  const end = Date.now() + timeout
  for (;;) {
    const now = await waitingIds(app)
    if (JSON.stringify(now) === JSON.stringify(ids)) return
    if (Date.now() > end) throw new Error(`${message}: ${JSON.stringify(now)} waiting`)
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

/** Nothing has the focus: a key goes to the page. */
async function focusPage(page) {
  await page.evaluate(() => {
    if (document.activeElement instanceof window.HTMLElement) document.activeElement.blur()
  })
}

/** Turns the shortcuts on through their button and the switch in their list, and closes the list. */
async function turnShortcutsOn(page, app) {
  const [title, use, close] = await Promise.all(['approvals.shortcuts.title', 'approvals.shortcuts.switch', 'common.close'].map((key) => word(app, key)))
  await page.getByRole('button', { name: title }).click()
  const dialog = page.getByRole('dialog', { name: title })
  await dialog.waitFor()
  const toggle = dialog.getByRole('switch', { name: use })
  assert.equal(await toggle.isChecked(), false, 'the shortcuts are on before they were turned on')
  // The switch is drawn by its label (the checkbox itself is for screen readers): a click on its words turns it.
  await dialog.locator('label').filter({ hasText: use }).click()
  assert.equal(await toggle.isChecked(), true, 'the switch did not turn on')
  await dialog.getByRole('button', { name: close, exact: true }).click()
  await dialog.waitFor({ state: 'hidden' })
}

/** Three texts and the shortcuts off, as they are at first: A, X, J and the others do nothing. */
async function approvalsShortcutsOffByDefault(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 90 }), waitingText(2, { age: 60 }), waitingText(3)]))
  assert.equal(await page.evaluate(() => window.localStorage.getItem('latet.approvals.shortcuts')), null)
  assert.equal(await page.getByText(await word(app, 'approvals.shortcuts.on')).count(), 0, 'the shortcuts say they are on')
  await focusPage(page)
  for (const key of ['a', 'x', 'j', 'm', 'Shift+A', '?']) await page.keyboard.press(key)
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await waitingIds(app), ['ap_check1', 'ap_check2', 'ap_check3'], 'a key decided a text with the shortcuts off')
  assert.equal(await selectedText(page, app), '1', 'J moved through the queue with the shortcuts off')
  assert.equal(await page.getByRole('dialog').count(), 0, 'a key opened a dialog with the shortcuts off')
}

/**
 * Turned on, the shortcuts say so, and: J and K move, M masks the suggestion, A approves the text shown, X does not send
 * it, ? opens their list, Shift+A asks before approving all; the switch is kept in this browser across a reload.
 */
async function approvalsShortcutsOn(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () =>
    addTexts(app, [waitingText(1, { age: 120 }), waitingText(2, { age: 90 }), waitingText(3, { age: 60 }), waitingText(4)]),
  )
  await turnShortcutsOn(page, app)
  const on = page.getByText(await word(app, 'approvals.shortcuts.on'), { exact: true })
  await on.waitFor()
  assert.equal(await page.evaluate(() => window.localStorage.getItem('latet.approvals.shortcuts')), 'true')
  await focusPage(page)

  await page.keyboard.press('j')
  await showsText(page, app, 2)
  await page.keyboard.press('k')
  await showsText(page, app, 1)

  // M with nothing selected masks the local reviewer's suggestion, as its Mask button does.
  await page.keyboard.press('m')
  await page.getByRole('list', { name: await word(app, 'approvals.added.label') }).waitFor()
  await page.getByRole('button', { name: await word(app, 'approvals.approveWith', { count: 1 }) }).waitFor()

  await focusPage(page)
  await lookAtIt(page, app)
  await page.keyboard.press('a')
  await waitingAre(app, ['ap_check2', 'ap_check3', 'ap_check4'], 'A did not approve the text shown')
  await showsText(page, app, 2)
  await focusOnText(page, app, 2)
  await focusPage(page)
  await lookAtIt(page, app)
  await page.keyboard.press('x')
  await waitingAre(app, ['ap_check3', 'ap_check4'], 'X did not drop the text shown')
  await showsText(page, app, 3)

  await focusPage(page)
  await page.keyboard.press('?')
  const list = page.getByRole('dialog', { name: await word(app, 'approvals.shortcuts.title') })
  await list.waitFor()
  await page.keyboard.press('Escape')
  await list.waitFor({ state: 'hidden' })

  await focusPage(page)
  await page.keyboard.press('Shift+A')
  const question = page.getByRole('dialog', { name: await word(app, 'approvals.approveAllTitle', { count: 2 }) })
  await question.waitFor()
  await page.keyboard.press('a') // in the question: not a shortcut
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await waitingIds(app), ['ap_check3', 'ap_check4'], 'a key pressed in the question decided a text')
  await page.keyboard.press('Escape')
  await question.waitFor({ state: 'hidden' })

  // Kept in this browser: after a reload, on as it was, and A approves.
  await page.reload()
  await page.locator('main').waitFor()
  await approvalsSelected(page, 'texts')
  await on.waitFor()
  await page.getByRole('region', { name: await word(app, 'approvals.panes.original'), exact: true }).waitFor()
  await focusPage(page)
  const [first, ...rest] = await waitingIds(app)
  assert.ok(first, 'no text waits after the reload')
  await lookAtIt(page, app)
  await page.keyboard.press('a')
  await waitingAre(app, rest, 'A did nothing after a reload')
}

/** The shortcuts on: a key typed in a field, a text area or an editable element is typing, never a shortcut. */
async function approvalsShortcutsNeverWhileTyping(helpers) {
  const { page, app } = helpers
  await page.addInitScript(() => window.localStorage.setItem('latet.approvals.shortcuts', 'true'))
  await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)]))
  await page.getByText(await word(app, 'approvals.shortcuts.on'), { exact: true }).waitFor()
  await page.evaluate(() => {
    const main = document.querySelector('main')
    for (const html of ['<input data-typing aria-label="typing">', '<textarea data-typing aria-label="typing"></textarea>', '<div data-typing contenteditable="true" aria-label="typing" style="min-height: 20px"></div>']) {
      main.insertAdjacentHTML('afterbegin', html)
    }
  })
  for (const field of await page.locator('[data-typing]').all()) {
    await field.click()
    await page.keyboard.type('ajxkm?')
    await page.keyboard.press('Shift+A')
  }
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await waitingIds(app), ['ap_check1', 'ap_check2'], 'a key typed in a field decided a text')
  assert.equal(await selectedText(page, app), '1', 'a key typed in a field moved through the queue')
  assert.equal(await page.getByRole('dialog').count(), 0, 'a key typed in a field opened a dialog')
  // Out of the fields, the same keys are shortcuts again.
  await focusPage(page)
  await page.keyboard.press('j')
  await showsText(page, app, 2)
}

/** Enter approves nothing, with the shortcuts off or on. */
async function approvalsEnterNoLongerApproves(helpers) {
  const { page, app } = helpers
  await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)]))
  await focusPage(page)
  await page.keyboard.press('Enter')
  await turnShortcutsOn(page, app)
  await focusPage(page)
  await page.keyboard.press('Enter')
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await waitingIds(app), ['ap_check1', 'ap_check2'], 'Enter approved a text')
}

/** M with text selected in the original opens the menu of what it is, as releasing the mouse on it does. */
async function approvalsShortcutMasksTheSelection(helpers) {
  const { page, app } = helpers
  await page.addInitScript(() => window.localStorage.setItem('latet.approvals.shortcuts', 'true'))
  await approvalsWith(helpers, () => addTexts(app, [waitingText(1)]))
  const original = page.getByRole('region', { name: await word(app, 'approvals.panes.original'), exact: true })
  await original.waitFor()
  // Selected by the page (as the keyboard of caret browsing would), so that no mouse opens the menu first.
  await original.evaluate((region) => {
    const walker = document.createTreeWalker(region, 4)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const at = node.textContent.indexOf('invoice')
      if (at < 0) continue
      const range = document.createRange()
      range.setStart(node, at)
      range.setEnd(node, at + 'invoice'.length)
      window.getSelection().removeAllRanges()
      window.getSelection().addRange(range)
      return
    }
    throw new Error('no "invoice" in the original')
  })
  await page.keyboard.press('m')
  const menu = page.getByRole('dialog', { name: await word(app, 'approvals.select.title') })
  await menu.waitFor()
  assert.ok((await menu.innerText()).includes('invoice'), 'the menu is not about the text selected')
  await menu.getByRole('button', { name: await word(app, 'reviewer.categories.secret'), exact: true }).click()
  await page.getByRole('list', { name: await word(app, 'approvals.added.label') }).getByText('invoice').waitFor()
}

// ---------------------------------------------------------------------------------------------------------------
// The final review's fixes: the focus after a decision, a decision on a text just shown, a letter on Settings.

/** Waits as long as a text shows before it can be decided (lib/approvalsView's DECIDE_AFTER_MS), and a little more. */
async function lookAtIt(page, app) {
  const after = await app(async (base) => (await import(`${base}/src/lib/approvalsView.ts`)).DECIDE_AFTER_MS)
  await page.waitForTimeout(after + 50)
}

/** Waits for the focus to be on the text shown, text `number`: its section, never one of its buttons. */
async function focusOnText(page, app, number) {
  const label = await word(app, 'approvals.batch.label')
  await page.waitForFunction(
    ([label, start]) => {
      const active = document.activeElement
      return active?.tagName === 'SECTION' && active.getAttribute('aria-label') === label && active.textContent.includes(start)
    },
    [label, `Text ${number}:`],
  )
}

/**
 * After Approve or Don't send, the focus is on the next text, its section, never on a button: a keyboard user goes on
 * from there, and a key pressed again decides nothing. In a wide window and in a narrow one.
 */
async function approvalsFocusAfterDecision(helpers) {
  const { page, app } = helpers
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 })
    await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 120 }), waitingText(2, { age: 90 }), waitingText(3)]))
    const [approve, reject] = await Promise.all([word(app, 'approvals.approve'), word(app, 'approvals.reject')])
    await showsText(page, app, 1)
    await lookAtIt(page, app)
    await page.getByRole('button', { name: approve, exact: true }).click()
    await waitingAre(app, ['ap_check2', 'ap_check3'], `${width} px: Approve did not approve the text shown`)
    await focusOnText(page, app, 2)
    await lookAtIt(page, app)
    await page.getByRole('button', { name: reject, exact: true }).click()
    await waitingAre(app, ['ap_check3'], `${width} px: Don't send did not drop the text shown`)
    await focusOnText(page, app, 3)
  }
}

/**
 * Approve pressed twice, the second time as the next text shows (a double click or a double tap, with a daemon that
 * answers at once): the first text is approved, the next stays waiting until it has shown long enough. In a wide
 * window and in a narrow one.
 */
async function approvalsDoubleClickDecidesOneText(helpers) {
  const { page, app } = helpers
  for (const width of [1440, 960]) {
    await page.setViewportSize({ width, height: 844 })
    await approvalsWith(helpers, () => addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)]))
    await showsText(page, app, 1)
    await lookAtIt(page, app)
    const [approve, original] = await Promise.all([word(app, 'approvals.approve'), word(app, 'approvals.panes.original')])
    // In the page, so that the second press comes the moment the next text shows with its Approve on.
    await page.evaluate(
      async ([approve, original]) => {
        const button = () => [...document.querySelectorAll('button')].find((one) => one.textContent.trim() === approve && !one.disabled)
        button().click()
        while (!(document.querySelector(`[role=region][aria-label="${original}"]`)?.textContent?.startsWith('Text 2:') && button())) {
          await new Promise((resolve) => window.requestAnimationFrame(resolve))
        }
        button().click()
      },
      [approve, original],
    )
    await waitingAre(app, ['ap_check2'], `${width} px: the first press did not approve the first text`)
    await page.waitForTimeout(MOCK_SETTLE_MS)
    assert.deepEqual(await waitingIds(app), ['ap_check2'], `${width} px: the second press approved the next text unseen`)
    await page.getByRole('button', { name: approve, exact: true }).click()
    await waitingAre(app, [], `${width} px: Approve did nothing on a text shown long enough`)
  }
}

/**
 * The shortcuts on, two texts and a change to the settings waiting, the Settings tab shown: A, X, J, K, M and Shift+A
 * decide nothing, move nothing, mask nothing and ask nothing. Back on Texts, A approves the text shown.
 */
async function approvalsLettersOnSettingsDecideNothing(helpers) {
  const { page, app } = helpers
  await page.addInitScript(() => window.localStorage.setItem('latet.approvals.shortcuts', 'true'))
  await approvalsWith(
    helpers,
    async () => {
      await addTexts(app, [waitingText(1, { age: 90 }), waitingText(2)])
      await addSettingsChange(app)
    },
    '/approvals#settings',
  )
  await approvalsSelected(page, 'settings')
  await page.locator('[data-settings-change]').waitFor()
  await lookAtIt(page, app) // the text behind the tab could be decided by now
  await focusPage(page)
  for (const key of ['a', 'x', 'j', 'k', 'm', 'Shift+A']) await page.keyboard.press(key)
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.deepEqual(await waitingIds(app), ['ap_check1', 'ap_check2'], 'a key on the Settings tab decided a text')
  assert.equal(await page.getByRole('dialog').count(), 0, 'a key on the Settings tab opened a dialog')
  assert.equal(await page.locator('[data-settings-change]').isVisible(), true, 'the change to the settings went')
  await approvalsTab(page, 'texts').click()
  await approvalsSelected(page, 'texts')
  await showsText(page, app, 1)
  assert.equal(await page.getByRole('list', { name: await word(app, 'approvals.added.label') }).count(), 0, 'M on the Settings tab masked a value')
  await focusPage(page)
  await page.keyboard.press('a')
  await waitingAre(app, ['ap_check2'], 'A on the Texts tab did not approve the text shown')
}

/**
 * A segmented control (Stats' period) is drawn as in the look: its choices in a well, the chosen one raised, as tall as
 * a button; the arrow keys move the choice, as in any radio group, and its focus ring shows.
 */
async function segmentedRaisesTheChoice({ page, open, app }) {
  await open('/stats')
  const group = page.getByRole('radiogroup', { name: await word(app, 'stats.period') })
  await group.waitFor()
  const look = () =>
    group.evaluate((element) => {
      const probe = document.createElement('span')
      probe.style.background = 'var(--raised)'
      document.body.append(probe)
      const raised = getComputedStyle(probe).backgroundColor
      probe.remove()
      return {
        height: element.getBoundingClientRect().height,
        choices: [...element.querySelectorAll('label')].map((label) => ({
          checked: label.querySelector('input').checked,
          raised: getComputedStyle(label).backgroundColor === raised,
          weight: getComputedStyle(label).fontWeight,
        })),
      }
    })
  const before = await look()
  assert.equal(before.height, 30, 'the control is not as tall as a button')
  assert.equal(before.choices.filter((choice) => choice.checked).length, 1)
  for (const choice of before.choices) {
    assert.equal(choice.raised, choice.checked, `a choice ${choice.checked ? 'chosen is not' : 'not chosen is'} raised`)
    assert.equal(choice.weight, choice.checked ? '500' : '400')
  }
  const chosen = before.choices.findIndex((choice) => choice.checked)
  await group.getByRole('radio').nth(chosen).focus()
  await page.keyboard.press('ArrowRight')
  await group.getByRole('radio').nth(chosen + 1).and(page.locator(':checked')).waitFor()
  const after = await look()
  assert.deepEqual(
    after.choices.map((choice) => choice.raised),
    after.choices.map((choice, index) => index === chosen + 1),
    'the arrow key did not raise the next choice',
  )
  const ring = await page.evaluate(() => getComputedStyle(document.activeElement.parentElement.querySelector('span[class*="peer-focus-visible"]')).outlineStyle)
  assert.equal(ring, 'solid', 'no focus ring on the choice reached with the keyboard')
}

/**
 * The tutorial's Live Reviewer chapter: its words point at the Approve button, Enter approves nothing (nor A, with the
 * shortcuts on: the tutorial's queue has none), and Approve sends the request.
 */
async function tutorialLiveChapterApprovesWithTheButton(helpers) {
  const { page, open, app } = helpers
  await page.addInitScript(() => window.localStorage.setItem('latet.approvals.shortcuts', 'true'))
  await open('/tutorial/live?mock-tutorial')
  await page.getByRole('button', { name: 'Send it', exact: true }).click()
  const original = page.getByRole('region', { name: await word(app, 'approvals.panes.original'), exact: true })
  await original.waitFor()
  const hint = await word(app, 'tutorial.latet.approveAsIs')
  await page.getByText(hint, { exact: true }).waitFor()
  assert.equal(await app(async (base) => (await import(`${base}/src/i18n.ts`)).default.exists('tutorial.latet.approveEnter')), false)
  await focusPage(page)
  await page.keyboard.press('Enter')
  await page.keyboard.press('a')
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.equal((await waitingIds(app)).length, 1, 'a key approved the tutorial’s request')
  await page.getByRole('button', { name: await word(app, 'approvals.approve'), exact: true }).click()
  await waitingAre(app, [], 'Approve did not send the tutorial’s request')
}

// ---------------------------------------------------------------------------------------------------------------
// The documentation from every part of the dashboard (notes/dashboard-docs-plan.md, Task 6). The mock says it serves
// the documentation of its version (`docs: true`): every link opens /_latet/docs/, in a new tab. Its dev server has no
// such pages, so a check reads where a link leads, and the daemon's own checks (scripts/docs-checks.mjs) open them.

/** Where a page's "Documentation" link leads, by the page's address (a function: it needs the mock's ids). */
const PAGE_DOCS = [
  ['/live', '/_latet/docs/dashboard/#live'],
  ['/approvals', '/_latet/docs/dashboard/#approvals'],
  ['/sessions', '/_latet/docs/dashboard/#sessions'],
  [({ session }) => `/sessions/${encodeURIComponent(session)}`, '/_latet/docs/dashboard/#sessions'],
  [({ exchange }) => `/exchanges/${exchange}`, '/_latet/docs/dashboard/#request-detail'],
  ['/stats', '/_latet/docs/dashboard/#stats'],
  ['/vault', '/_latet/docs/dashboard/#vault'],
  ['/rules', '/_latet/docs/dashboard/#rules'],
  [({ project }) => `/rules?project=${encodeURIComponent(project)}`, '/_latet/docs/dashboard/#rules-of-a-project'],
  ['/playground', '/_latet/docs/dashboard/#playground'],
  ['/integrations', '/_latet/docs/dashboard/#protected-agents'],
  ['/tutorial?mock-tutorial', '/_latet/docs/getting-started/tutorial/'],
  ['/tutorial/live?mock-tutorial', '/_latet/docs/getting-started/tutorial/'],
]

/** Each page has one "Documentation" link, at the top, to the section that explains it, opened in a new tab. */
async function everyPageHasItsDocumentationLink({ page, open, app }) {
  const ids = await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const { projectOfAgent } = await import(`${base}/src/mock/projects.ts`)
    const exchange = (await api.exchanges()).items[0]
    return { exchange: exchange.id, session: exchange.session, project: projectOfAgent('claude-code').id }
  })
  for (const [where, href] of PAGE_DOCS) {
    const path = typeof where === 'function' ? where(ids) : where
    await open(path)
    const links = page.locator('main').getByRole('link', { name: await word(app, 'docs.page'), exact: true })
    await links.first().waitFor({ timeout: 5000 }).catch(() => {
      throw new Error(`${path}: no Documentation link`)
    })
    assert.equal(await links.count(), 1, `${path}: more than one Documentation link`)
    // Until GET /me says the daemon serves its own documentation, a link leads to docs.latet.ai.
    await page
      .waitForFunction(([link, want]) => link.getAttribute('href') === want, [await links.elementHandle(), href], { timeout: 5000 })
      .catch(() => undefined)
    assert.equal(await links.getAttribute('href'), href, `${path}: the Documentation link leads elsewhere`)
    assert.equal(await links.getAttribute('target'), '_blank', `${path}: the Documentation link opens in the same tab`)
    assert.match((await links.getAttribute('rel')) ?? '', /\bnoreferrer\b/, `${path}: the Documentation link sends the page's address`)
    // At the top: in the page's header, above everything the page shows.
    const top = await links.evaluate((link) => link.getBoundingClientRect().top)
    assert.ok(top < 260, `${path}: the Documentation link is not at the top of the page (${top} px)`)
  }
}

/**
 * Rules › Allowed values: the "?" beside its title opens the section of the profiles page on allow lists, named after
 * the section for a screen reader, and in Italian when the dashboard is Italian; the keyboard reaches it.
 */
async function aSectionHelpOpensItsAnchor({ page, open, app }) {
  for (const [language, href] of [
    ['en', '/_latet/docs/concepts/profiles/#allow-lists'],
    ['it', '/_latet/docs/it/concepts/profiles/#valori-consentiti'],
  ]) {
    await open('/live') // the same address again would only move to its anchor, in the language it had
    await openIn(page, open, language, '/rules#allow')
    const name = await word(app, 'docs.section', { section: await word(app, 'rules.allow.title') })
    const help = page.locator('#allow').getByRole('link', { name, exact: true })
    await help.waitFor()
    await page
      .waitForFunction(([link, want]) => link.getAttribute('href') === want, [await help.elementHandle(), href], { timeout: 5000 })
      .catch(() => undefined)
    assert.equal(await help.getAttribute('href'), href, `${language}: the ? leads elsewhere`)
    assert.equal(await help.getAttribute('title'), name, `${language}: the ? has no tooltip`)
    assert.equal(await help.getAttribute('target'), '_blank')
    await help.focus()
    assert.equal(await help.evaluate((link) => link === document.activeElement), true, `${language}: the keyboard cannot reach the ?`)
  }
}

/**
 * A request stopped by Claude Code's auto mode check: its notice (what happened, what it meant, what you can do) has a
 * "Why" that opens the section of Troubleshooting on that very message; the Withheld-style flags above it have theirs.
 */
async function aBlockedRequestSaysWhyInTheDocumentation({ page, open, app }) {
  const id = await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    return (await api.exchanges()).items.find((item) => item.flags.includes('auto-mode-review') && item.flags.includes('blocked')).id
  })
  await open(`/exchanges/${id}`)
  const notice = page.locator('section[aria-labelledby="why-title"]')
  await notice.waitFor()
  const why = notice.getByRole('link', { name: await word(app, 'docs.why'), exact: true })
  assert.equal(await why.getAttribute('href'), '/_latet/docs/troubleshooting/#auto-mode-says-its-reviewer-is-temporarily-unavailable')
  assert.equal(await why.getAttribute('target'), '_blank')
  // The flag Auto mode review, in the request's header, says why too: where Claude Code's reviewer is explained.
  const flag = page.getByRole('link', { name: await word(app, 'docs.whyAbout', { about: await word(app, 'flags.auto-mode-review.label') }), exact: true })
  assert.equal(await flag.getAttribute('href'), '/_latet/docs/agents/claude-code/#auto-modes-reviewer')
}

// ---------------------------------------------------------------------------------------------------------------
// Words longer than English fit (the follow-ups' section 1).

/** The longer languages: their words are where an English-sized width cuts first. */
const LONGER = ['de', 'it', 'fr', 'es']

/** Opens `path` with the dashboard in `language` (an init script added later runs later, so it wins). */
async function openIn(page, open, language, path) {
  await page.addInitScript((code) => window.localStorage.setItem('latet.language', JSON.stringify(code)), language)
  await open(path)
  await page.waitForTimeout(MOCK_SETTLE_MS)
}

/**
 * The fields shown in the page's main part whose words do not fit: an empty field's placeholder, or a menu's chosen
 * option, measured in the field's own font against the room its padding leaves.
 */
async function cutFields(page) {
  return page.evaluate(() => {
    const context = document.createElement('canvas').getContext('2d')
    const cut = []
    for (const field of document.querySelectorAll('main input, main textarea, main select')) {
      if (field.offsetParent === null) continue
      const words = field.tagName === 'SELECT' ? field.selectedOptions[0]?.textContent ?? '' : field.value ? '' : field.placeholder
      if (!words) continue
      const style = getComputedStyle(field)
      context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
      // A number field keeps room for its up and down arrows on the right, beside its padding.
      const arrows = field.type === 'number' ? 18 : 0
      const room = field.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - arrows
      if (context.measureText(words).width > room + 0.5) cut.push(words)
    }
    return cut
  })
}

/** On the Rules page's Protection and Reviewers tabs, every field's words fit, in each longer language. */
async function rulesFieldsFitInLongerLanguages({ page, open }) {
  for (const language of LONGER) {
    for (const path of ['/rules#data', '/rules#reviewer']) {
      await openIn(page, open, language, path)
      assert.deepEqual(await cutFields(page), [], `cut on ${path} in ${language}`)
    }
  }
}

/** On Approvals, the project menu shows its whole choice, in each longer language. */
async function approvalsProjectMenuFitsInLongerLanguages(helpers) {
  const { page, open, app } = helpers
  for (const language of LONGER) {
    await openIn(page, open, language, '/approvals')
    await twoTextsOfTwoProjects(app)
    await page.locator('main select').first().waitFor()
    assert.deepEqual(await cutFields(page), [], `cut on Approvals in ${language}`)
  }
}

/** On Live, in each longer language, the table of requests is no wider than its panel. */
async function liveTableFitsItsPanelInLongerLanguages({ page, open }) {
  for (const language of LONGER) {
    await openIn(page, open, language, '/live')
    const table = page.locator('main table').first()
    await table.waitFor()
    const { wide, room } = await table.evaluate((element) => ({ wide: element.scrollWidth, room: element.parentElement.clientWidth }))
    assert.ok(wide <= room, `the Live table is ${wide - room} px wider than its panel in ${language}`)
  }
}

/** In each longer language, on a computer and on a server, the brand block keeps room above the name "Latet". */
async function brandBlockKeepsRoomAboveTheName({ page, open }) {
  for (const language of LONGER) {
    for (const path of ['/live', '/live?mock-server']) {
      await openIn(page, open, language, path)
      const room = await page.evaluate(() => {
        const name = [...document.querySelectorAll('aside p, nav p, header p')].find((p) => p.textContent === 'Latet')
        const block = name.parentElement.parentElement
        return name.getBoundingClientRect().top - block.getBoundingClientRect().top
      })
      assert.ok(room >= 8, `only ${room.toFixed(1)} px above "Latet" in ${language} (${path})`)
    }
  }
}

/**
 * The page once called Integrations is named for the question it answers, in the menu and as its title, and one line
 * above its agents table says what "Through Latet" means; its address stays /integrations.
 */
async function protectedAgentsPageSaysWhatItAnswers({ page, open, app }) {
  await open('/integrations')
  await page.getByRole('navigation').getByRole('link', { name: 'Protected agents', exact: true }).waitFor()
  await page.getByRole('heading', { level: 1, name: 'Protected agents', exact: true }).waitFor()
  await page.getByText(await word(app, 'integrations.agents.statusHelp'), { exact: true }).waitFor()
  assert.ok(page.url().endsWith('/integrations'))
}

/**
 * A vault of more than a thousand values (?mock-big-vault) in pages, as GitHub draws them: fifty rows, the page links
 * with the current one marked, the page in the address, "51–100 of …" on the second page, Next to the third, and a
 * search back to the first page.
 */
async function vaultIsPagedLikeGitHub({ page, open, app }) {
  await open('/vault?mock-big-vault')
  const nav = page.getByRole('navigation', { name: await word(app, 'pagination.label'), exact: true })
  await nav.waitFor()
  const rows = page.locator('table.data-table tbody tr')
  assert.equal(await rows.count(), 50)
  assert.equal(await nav.locator('[aria-current="page"]').innerText(), '1')
  await nav.getByRole('link', { name: await word(app, 'pagination.page', { page: 2 }), exact: true }).click()
  await page.waitForURL(/[?&]page=2(&|$)/)
  await nav.locator('[aria-current="page"]', { hasText: /^2$/ }).waitFor()
  await page.getByRole('status').filter({ hasText: /^51–100 / }).waitFor()
  assert.equal(await rows.count(), 50)
  await nav.getByRole('link', { name: await word(app, 'pagination.next'), exact: true }).click()
  await page.waitForURL(/[?&]page=3(&|$)/)
  await page.getByRole('searchbox').fill('example')
  await page.waitForURL((url) => !url.searchParams.has('page') && url.searchParams.get('q') === 'example')
  await nav.locator('[aria-current="page"]', { hasText: /^1$/ }).waitFor()
}

/**
 * A project's notices in the language of the page: the mock's billing-app has a latet.yaml that sets what only your
 * own settings may set, and the list of what is not applied words it in each language, never in the daemon's English
 * (the documentation's screenshots show it).
 */
async function projectNoticesAreWordedInEveryLanguage({ page, open, app }) {
  const { id, english } = await app(async (base) => {
    const { projectOfAgent } = await import(`${base}/src/mock/projects.ts`)
    const { api } = await import(`${base}/src/api/client.ts`)
    const { id } = projectOfAgent('claude-code')
    const project = await api.project(id)
    return { id, english: project.notices.map((notice) => notice.message) }
  })
  assert.ok(english.length > 0, 'the mock project has no notice')
  for (const language of ['en', 'it', 'de', 'fr', 'es']) {
    await openIn(page, open, language, `/rules?project=${encodeURIComponent(id)}`)
    const notices = page.getByRole('list', { name: await word(app, 'projects.notices'), exact: true })
    await notices.waitFor()
    const shown = await notices.innerText()
    if (language === 'en') continue // the daemon's words are English: the page words the reason the same
    for (const message of english) assert.equal(shown.includes(message), false, `${language}: the notice is in the daemon's English: ${shown}`)
    assert.ok(shown.includes(await word(app, 'projects.repoCannotSet')), `${language}: the notice is not worded in the page's language: ${shown}`)
  }
}

/**
 * A page past the last one in the address (`?page=99`): the daemon answers its last page, and the address says that
 * page (the other filters kept, no history entry added), so that a copied address or a reload shows what was seen.
 */
async function vaultAddressKeepsThePageTheDaemonAnswered({ page, open, go, app }) {
  await open('/vault?mock-big-vault')
  const nav = page.getByRole('navigation', { name: await word(app, 'pagination.label'), exact: true })
  await nav.waitFor()
  await go('/vault?mock-big-vault&type=EMAIL&page=99')
  const last = await app(async (base) => (await (await import(`${base}/src/api/client.ts`)).api.vault({ q: '', type: 'EMAIL', class: '', page: 99 })).page)
  assert.ok(last > 1 && last < 99, `the mock's last page of emails is ${last}`)
  await page.waitForURL((url) => url.searchParams.get('page') === String(last), { timeout: 3000 }).catch(() => {
    throw new Error(`the address kept ${new URL(page.url()).search}, not the daemon's page ${last}`)
  })
  assert.equal(new URL(page.url()).searchParams.get('type'), 'EMAIL', 'the type filter was dropped from the address')
  assert.ok(new URL(page.url()).searchParams.has('mock-big-vault'), 'the mock flag was dropped from the address')
  await nav.locator('[aria-current="page"]', { hasText: new RegExp(`^${last}$`) }).waitFor()
  // Replaced, not added: Back leaves the page it came from, the first one.
  await page.goBack()
  await page.waitForURL((url) => !url.searchParams.has('type'))
  assert.equal(new URL(page.url()).searchParams.has('page'), false)
}

/**
 * Strict data mode's lists under Rules: the files and the web addresses that count as data take a pattern each, and
 * one Save sends both.
 */
async function strictDataModeTakesFilesAndAddresses({ page, open, app }) {
  await open('/rules#data')
  const data = page.locator('#data')
  await data.waitFor()
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const saves = (window.latetSaves = [])
    const save = api.saveConfig
    api.saveConfig = (config, etag) => {
      saves.push(structuredClone(config))
      return save(config, etag)
    }
  })
  for (const key of ['data.files.title', 'data.hosts.title', 'data.tools.title']) {
    await data.getByRole('heading', { level: 3, name: await word(app, key), exact: true }).waitFor()
  }
  await data.getByRole('textbox', { name: await word(app, 'data.files.placeholder'), exact: true }).fill('exports/**')
  await data.getByRole('button', { name: await word(app, 'data.files.add'), exact: true }).click()
  await data.getByRole('textbox', { name: await word(app, 'data.hosts.placeholder'), exact: true }).fill('*.salesforce.com')
  await data.getByRole('button', { name: await word(app, 'data.hosts.add'), exact: true }).click()
  await page.getByRole('button', { name: await word(app, 'rules.save'), exact: true }).click()
  await page.waitForFunction(() => window.latetSaves.length > 0)
  const [saved] = await page.evaluate(() => window.latetSaves)
  assert.deepEqual([saved.data_files, saved.data_hosts], [['exports/**'], ['*.salesforce.com']])
}

/**
 * Rules for everyone are not the Community Edition's: on a Latet server, Rules shows a person's own rules alone, with
 * no box of items set for everyone, even when the settings the server answers still carry some (a term, a value never
 * masked and a destination, as the daemon once sent them); the local reviewer's server, set for everyone, still says
 * so; an API key's space says its settings and rules are its own.
 */
async function rulesOnServerSetNothingForEveryone({ page, open, app, go }) {
  await open('/live?mock-server')
  // The person's own rules, as the mock defines them, before the answer is changed.
  const own = await app(async (base) => {
    const config = await (await import(`${base}/src/api/client.ts`)).api.config()
    return { terms: config.terms.map((term) => term.value), values: config.allow_values }
  })
  assert.ok(own.terms.length > 0 && own.values.length > 0, 'the mock has no term or value of its own')
  // The settings of a server that sets items for everyone: the answer's `shared`, as the daemon once sent it.
  await app(async (base) => {
    const { api } = await import(`${base}/src/api/client.ts`)
    const everyone = {
      terms: [{ value: 'Project Falcon', type: 'PROJECT', case_sensitive: false, whole_word: true }],
      allow_values: ['support@acme.com'],
      upstreams: [{ name: 'llm-acme-com', url: 'https://llm.acme.com' }],
    }
    const config = api.config
    window.latetSharedAsked = 0
    api.config = async (signal) => {
      window.latetSharedAsked++
      return { ...(await config(signal)), shared: everyone }
    }
  })
  await go('/rules?mock-server#terms')
  await page.locator('#terms').waitFor()
  await page.waitForTimeout(MOCK_SETTLE_MS)
  assert.ok(await page.evaluate(() => window.latetSharedAsked > 0), 'Rules never asked for the settings the check changed')
  // Every tab's sections are in the page (those of the other tabs hidden): the person's own items are there, and
  // nothing set for everyone.
  const shown = await page.locator('main').evaluate((main) =>
    [main.textContent, ...[...main.querySelectorAll('input, textarea')].map((field) => field.value)].join('\n'),
  )
  for (const mine of [...own.terms, ...own.values]) assert.ok(shown.includes(mine), `the person's own ${mine} does not show`)
  for (const everyone of ['Project Falcon', 'support@acme.com', 'llm.acme.com', 'llm-acme-com']) {
    assert.equal(shown.includes(everyone), false, `${everyone}, set for everyone, shows on Rules`)
  }
  // The heading of what is set for everyone on the server belongs to the local reviewer's box alone (its tab).
  const boxes = await page.getByText(await word(app, 'shared.title'), { exact: true }).evaluateAll((found) => found.filter((el) => !el.closest('[data-server-reviewer]')).length)
  assert.equal(boxes, 0, 'a box outside the local reviewer says what is set for everyone')
  await go('/rules?mock-server#reviewer')
  await page.locator('#reviewer [data-server-reviewer]').getByText(await word(app, 'shared.title'), { exact: true }).waitFor()
}

const CHECKS = [
  rulesOnServerSetNothingForEveryone,
  strictDataModeTakesFilesAndAddresses,
  vaultIsPagedLikeGitHub,
  vaultAddressKeepsThePageTheDaemonAnswered,
  projectNoticesAreWordedInEveryLanguage,
  protectedAgentsPageSaysWhatItAnswers,
  rulesFieldsFitInLongerLanguages,
  approvalsProjectMenuFitsInLongerLanguages,
  liveTableFitsItsPanelInLongerLanguages,
  brandBlockKeepsRoomAboveTheName,
  exchangePageHasNoStrictSwitch,
  exchangePageFitsAPhone,
  exchangeWithStructureLeadsToProjectRules,
  exchangeWithStructureLeadsToGlobalRules,
  exchangeWithoutStructureShowsNothing,
  exchangeAsksNothingWithoutTheTexts,
  rulesLinkOpensItsTab,
  everyRulesLinkOpensItsTab,
  tutorialLinksIntoRulesOpenTheirTab,
  rulesEditSurvivesTabSwitch,
  rulesLocalFieldsSurviveTabSwitch,
  rulesOneSaveSendsEveryTab,
  termsTableAddsEditsAndDeletes,
  ruleFilterLetsGoAValueThatLeft,
  copyToAProjectKeepsTheSelectionUntilDone,
  rulesPanelSaveKeepsTheSaveBarEdits,
  ruleTableLongValueTruncates,
  rulePanelIsAModalOnAPhone,
  rulePanelLeftInAnotherTabStaysQuiet,
  rulePanelStaysThroughTheTopSearch,
  leavingWithAnUnsavedPanelAsksFirst,
  rulesSearchFindsAcrossTabs,
  rulesSearchFindsARowInItsTab,
  rulesTabsMoveWithArrowKeys,
  rulesSelectedTabInSightWhenNarrow,
  rulesHasNoSectionList,
  allowedTableFindsAddsAndRemoves,
  criteriaTableAddsAType,
  recognizersTableEditsInThePanel,
  recognizerPanelKeepsAChangeMadeMeanwhile,
  termsImportPreviewsThenAdds,
  listsExportAndImportTheirOwnFile,
  listExportKeepsTheExportGuards,
  playgroundMasksOnTheButton,
  playgroundTriesAnotherProfile,
  projectRulesHaveTheGlobalTabs,
  everyProjectLinkOpensItsTab,
  projectOneSaveSendsEveryTab,
  projectRepoTabSavesTheFileAlone,
  projectTablesShowEverySource,
  projectBulkDeleteSaysTheRepoFile,
  projectGlobalRowIsReadOnly,
  projectNewRowSavesToTheChosenPlace,
  saveToSaysLatetYamlIsCreated,
  repoRecognizerOffersNoLetThrough,
  projectSuggestionOpensInThePanel,
  projectPanelSaveKeepsTheSaveBarEdits,
  approvalsTabsCountAndOpenOnTexts,
  approvalsOpensOnSettingsWithNoTexts,
  approvalsChangeArrivingKeepsTexts,
  approvalsReviewLinkOpensSettings,
  approvalsNarrowAt960,
  approvalsNarrowAt390,
  approvalsNarrowNextWhenDecidedElsewhere,
  approvalsWideKeepsTheQueue,
  approvalsShortcutsOffByDefault,
  approvalsShortcutsOn,
  approvalsShortcutsNeverWhileTyping,
  approvalsEnterNoLongerApproves,
  approvalsShortcutMasksTheSelection,
  approvalsFocusAfterDecision,
  approvalsDoubleClickDecidesOneText,
  approvalsLettersOnSettingsDecideNothing,
  segmentedRaisesTheChoice,
  tutorialLiveChapterApprovesWithTheButton,
  everyPageHasItsDocumentationLink,
  aSectionHelpOpensItsAnchor,
  aBlockedRequestSaysWhyInTheDocumentation,
]

// ---------------------------------------------------------------------------------------------------------------

/** A fresh browser context for one check: English, light, refused off its origin; what went wrong, collected. */
async function run(browser, origin, check) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'en',
    timezoneId: 'UTC',
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
  })
  const forbidden = []
  const errors = []
  try {
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === origin) return route.continue()
      forbidden.push(`${url.protocol}//${url.host}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await context.routeWebSocket('**/*', (socket) => socket.close())
    await context.addInitScript(() => {
      window.localStorage.setItem('latet.language', '"en"')
      window.localStorage.setItem('latet.theme', '"light"')
    })
    const page = await context.newPage()
    page.setDefaultTimeout(5000)
    page.setDefaultNavigationTimeout(15000)
    page.on('pageerror', (error) => errors.push(error.message))
    // A file the server refuses (a font of a linked node_modules outside the allowed folders: 403) is an error too.
    page.on('response', (response) => {
      if (response.status() >= 400) errors.push(`${response.status()} ${new URL(response.url()).pathname}`)
    })
    const helpers = {
      page,
      open: async (path) => {
        await page.goto(`${origin}${BASE}${path}`)
        await page.locator('main').waitFor()
        await page.evaluate(() => document.fonts.ready)
      },
      app: async (fn, ...args) => {
        if (!page.url().startsWith(origin)) await helpers.open('/live')
        return page.evaluate(`(${fn.toString()})(...${JSON.stringify([BASE, ...args])})`)
      },
      go: async (path) => {
        await page.evaluate((to) => {
          window.history.pushState(null, '', to)
          window.dispatchEvent(new PopStateEvent('popstate'))
        }, `${BASE}${path}`)
        await page.locator('main').waitFor()
      },
    }
    try {
      await check(helpers)
    } catch (error) {
      // What went wrong on the page often says why the check did not find what it looked for.
      const seen = [...errors, ...forbidden.map((url) => `refused ${url}`)]
      if (seen.length && error instanceof Error) error.message += `\n(on the page: ${seen.join('; ')})`
      throw error
    }
    assert.deepEqual(errors, [], 'errors on the page')
    assert.deepEqual(forbidden, [], 'requests off 127.0.0.1')
  } finally {
    await context.close()
  }
}

async function main() {
  const words = process.argv.slice(2).map((word) => word.toLowerCase())
  const selected = CHECKS.filter((check) => !words.length || words.some((word) => check.name.toLowerCase().includes(word)))
  if (!selected.length) {
    console.error(`No check matches ${words.join(', ')}. Checks: ${CHECKS.map((check) => check.name).join(', ')}.`)
    process.exit(2)
  }
  const server = await createServer({
    root,
    configFile: join(root, 'vite.config.ts'),
    mode: 'mock',
    logLevel: 'warn',
    // Port 0: one the system knows is free. node_modules may be a link to another checkout's: Vite serves the
    // files it resolves there (the fonts) only when their real folder is allowed too.
    server: { host: '127.0.0.1', port: 0, hmr: false, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } },
  })
  await server.listen()
  const address = server.httpServer.address()
  const origin = `http://127.0.0.1:${address.port}`
  const browser = await chromium.launch({ headless: true })
  let failed = 0
  try {
    for (const check of selected) {
      try {
        await run(browser, origin, check)
        console.log(`PASS ${check.name}`)
      } catch (error) {
        failed++
        console.log(`FAIL ${check.name}\n     ${String(error.message ?? error).replaceAll('\n', '\n     ')}`)
      }
    }
  } finally {
    await browser.close()
    await server.close()
  }
  console.log(`${selected.length - failed} passed, ${failed} failed`)
  process.exit(failed ? 1 : 0)
}

await main()
