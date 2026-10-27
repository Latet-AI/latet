// The tutorial's sandbox in mock mode (?mock-tutorial, or ?mock-tutorial=terms to open a chapter; with
// &mock-tutorial-fail each step fails once, to see how the page says so and tries again; with
// &mock-tutorial-no-ocr the sandbox finds none of your text recognition models, to see how the page says what the
// screenshots chapter needs): the nine chapters as the sandbox plays them (the texts, values, waits and fakes of
// src/latet/tutorial/chapters/), answering the API the real sandbox does (docs/dashboard-api.md,
// Tutorial). The agent, the model, the local reviewer, the text recognition and the history are made up; the
// dashboard's own pages read them as they would read the real ones, so the tutorial page can be built and tried
// without a daemon. Nothing here touches `window`: the host (mock/index.ts) lends the rest.

import { ApiError } from '../api/client'
import type {
  Batch,
  ConfigView,
  EntityClass,
  ExchangeSummary,
  Header,
  LiveItem,
  ReviewCategory,
  ReviewFinding,
  ReviewRecord,
  SettingsChange,
  Slot,
  TutorialCard,
  TutorialChapter,
  TutorialEnd,
  TutorialLine,
  TutorialMe,
  TutorialMenu,
  TutorialNext,
  TutorialShown,
  TutorialState,
  TutorialStepKind,
  TutorialStepResult,
  TutorialWaitKind,
  TutorialWaiting,
} from '../api/types'
import { isUnlocked } from '../lib/fields'
import { CATEGORY_TYPE, REVIEW_CATEGORIES } from '../lib/reviewer'
import type { Decision } from './approvals'
import { slot, textOf, type MockSlot, type Part, type Value } from './builder'
import { drawing, initialConfig, makeExchange, MOCK_ASSETS, vault, type MockExchange, type MockImage, type MockLeak } from './fixtures'
import { ALNUM, chars, hashSeed, seeded, shape } from './rng'

/** What the mock borrows from the rest of the mock dashboard. */
export interface TutorialHost {
  /** Milliseconds, as `Date.now()`. */
  now(): number
  config(): ConfigView
  /** The settings change (what an approved change does). */
  patchConfig(patch: Partial<ConfigView>): void
  /** The sandbox's settings start over from its base, with these changed (a chapter starts). */
  resetConfig(patch: Partial<ConfigView>): void
  history: { clear(): void; add(exchange: MockExchange): void }
  queue: {
    reset(): void
    addBatch(batch: Batch): void
    addChange(change: SettingsChange, apply: () => void): void
    onDecided(listener: (decision: Decision) => void): () => void
  }
}

/** The settings of a fresh sandbox: nothing of the user's, no reviewer, no name detection. */
export function sandboxBase(initial: ConfigView): ConfigView {
  const base = structuredClone(initial)
  return {
    ...base,
    terms: [],
    recognizers: [],
    recognizer_status: [],
    allow_values: [],
    allow_patterns: [],
    policies: {},
    fields: [],
    data: null,
    history: 'full',
    ner: 'off',
    ocr: 'off',
    identity: { enabled: false, usernames: [], hostname: null, full_name: null, emails: [] },
    llm_review: { ...base.llm_review, enabled: false },
    live_review: { ...base.live_review, enabled: false },
  }
}

// ---- The invented data --------------------------------------------------------------------------------

const AGENT = { name: 'Tutorial Agent', model: 'Tutorial Model' }
/** How long the agent works on a step (a step held for the user's decision takes that long too). */
export const BUSY_MS = 700
/** A run the daemon could not finish, as `&mock-tutorial-fail` makes each step fail once. */
export const STEP_FAILED = 'the agent ran, but its request was not recorded in time'

// Generated, not written out: a key in the source would be taken for a real one by a secret scanner.
const KEY = chars(seeded(4041), ALNUM, 40)
const KEY_FAKE = shape(seeded(4042), KEY)
// The other values are the chapters' (src/latet/tutorial/chapters/), with the fakes the sandbox's fixed key gives
// them: what the user sees at every run.
const EMAIL = 'james.wilson@acme.com'
const EMAIL_FAKE = 'emily.adams@proseware-4795.example'
const PHONE = '+44 20 7946 0958'
const PHONE_FAKE = '+44 28 9578 4573'
const CODENAME = 'Project Falcon'
const CODENAME_FAKE = 'Woodgrove Tailspin'
const CARD = '4242 4242 4242 4242'
const CARD_FAKE = '4365 0952 1057 8408'
const CUSTOMER = 'Emily Carter'
const CUSTOMER_EMAIL = 'emily.carter@acme.com'
const CUSTOMER_EMAIL_FAKE = 'michael.smith@proseware-4795.example'
/** What the sandbox gives a part of the customer's name masked by hand as a person (another part: a made-up one). */
const BY_HAND: Record<string, string> = { [CUSTOMER]: 'David Green', Emily: 'Chloe' }
const KEPT_COLUMNS = ['id', 'signup_date', 'plan_type']
/** What the pane says when the agent starts a new conversation. */
const CLEARED = '/clear: a new conversation, with no history'
const RESUMED = "/resume: yesterday's conversation, from before Latet was on"

const HEADERS: Header[] = [
  ['host', 'pretend-model.invalid'],
  ['user-agent', 'tutorial-agent/0.1'],
  ['anthropic-version', '2023-06-01'],
  ['content-type', 'application/json'],
]
const RESPONSE_HEADERS: Header[] = [['content-type', 'text/event-stream; charset=utf-8']]

function value(type: string, cls: EntityClass, rule: string, original: string, fake: string): Value {
  return vault.value(type, cls, rule, original, fake, Date.now() / 1000)
}
const email = (original: string, fake: string) => value('EMAIL', 'PII', 'pii:email', original, fake)
const key = () => value('GENERIC_API_KEY', 'SECRET', 'gitleaks:generic-api-key', KEY, KEY_FAKE)
const json = (part: Value): Value => ({ ...part, json: true })

// ---- A conversation of the agent ----------------------------------------------------------------------

interface Answer {
  text?: Part[]
  tool?: { name: string; args: Part[] }
}
interface Sent {
  id: string
  /** The request's blocks, in order: `blockAt(sent, block)` is the index the page points at. */
  slots: MockSlot[]
}

/** Where ``block`` is in the request: a block sent before is there as a copy, found by its path. */
const blockAt = (sent: { slots: readonly MockSlot[] }, block: MockSlot) => sent.slots.findIndex((b) => b.path === block.path)

/** The path of the text read in an image: the image's own, with this after it (as the daemon records it). */
const TEXT_READ = '#text'

/** What the agent has said to the model so far: each request carries it again, as the real agents' do. */
class Conv {
  private sent: MockSlot[] = []
  private fresh: MockSlot[] = []
  private n = 0
  constructor(readonly session: string) {
    this.fresh.push(slot('$.system', 'system', ['You are Tutorial Agent, a coding agent.']))
  }

  private add(kind: 'user_text' | 'tool_result', parts: Part[]): MockSlot {
    const block = slot(`$.messages[${this.n++}].content`, kind, parts)
    this.fresh.push(block)
    return block
  }
  user(parts: Part[]): MockSlot {
    return this.add('user_text', parts)
  }
  /** A conversation resumed: what was said in it goes with the next request, new to Latet. The user's block. */
  earlier(user: Part[], model: string): MockSlot {
    const said = this.add('user_text', user)
    this.fresh.push(slot(`$.messages[${this.n++}].content`, 'assistant_text', [model], { fromModel: true }))
    return said
  }
  result(parts: Part[]): MockSlot {
    return this.add('tool_result', parts)
  }
  /**
   * A picture the tool answers with (the agent's Read of a screenshot): the image is no block of text, but the text Latet read
   * in it is one (kind `image_text`), at the image's path in the request. The block: give it to `send` as an image it carries.
   */
  picture(parts: Part[]): MockSlot {
    const block = slot(`$.messages[${this.n++}].content[0].content[0].source.data${TEXT_READ}`, 'image_text', parts)
    this.fresh.push(block)
    return block
  }

  /** From the next request on, ``block`` is sent as ``parts`` (what the rules mask in it changed): the new block. */
  replace(block: MockSlot, parts: Part[]): MockSlot {
    const swapped = { ...block, parts }
    this.sent = this.sent.map((b) => (b === block ? swapped : b))
    this.fresh = this.fresh.map((b) => (b === block ? swapped : b))
    return swapped
  }

  /**
   * The request goes to the model; its answer (or, if stopped, nothing) is recorded in the sandbox's history. ``images``: the
   * blocks (`picture`) of the text read in the images it carries, which Latet painted the fakes over before they left.
   */
  send(
    host: TutorialHost,
    turn: { answer?: Answer; stop?: { reason: string; leaks: MockLeak[] }; subscription?: boolean; images?: MockSlot[] },
  ): Sent {
    const slots = [...this.sent.map((block) => ({ ...block, new: false })), ...this.fresh]
    const { answer, stop } = turn
    // As the daemon records an image: where it is in the request, how it went, the block of the text read in it; and flags it.
    const images = (turn.images ?? []).map(
      (block): MockImage => ({ path: block.path.slice(0, -TEXT_READ.length), state: 'masked', slot: blockAt({ slots }, block) }),
    )
    const exchange = makeExchange({
      ts: host.now() / 1000,
      session: this.session,
      agent: 'tutorial-agent',
      agentVersion: '0.1',
      upstream: 'anthropic',
      path: '/v1/messages',
      protocol: 'anthropic',
      mode: turn.subscription ? 'verify' : 'rewrite',
      transport: 'http',
      model: 'tutorial-model',
      status: stop ? 400 : 200,
      latency: stop ? 4 : 40,
      overhead: stop ? null : 3,
      tokens: null,
      slots,
      headers: HEADERS,
      response: stop || !answer ? null : { headers: RESPONSE_HEADERS, text: answer.text ?? [], tools: answer.tool ? [answer.tool] : [] },
      flags: [...(stop ? ['blocked'] : []), ...new Set(images.map((image) => `image:${image.state}`))],
      ...(stop ? { blockReason: stop.reason, leaks: stop.leaks } : {}),
      ...(images.length > 0 ? { images } : {}),
    })
    host.history.add(exchange)
    // A stopped request is never marked sent; one that went is history, with the model's answer.
    if (!stop) {
      this.sent.push(...this.fresh)
      this.fresh = []
      if (answer?.text) this.sent.push(slot(`$.messages[${this.n++}].content`, 'assistant_text', answer.text, { fromModel: true, isNew: false }))
      if (answer?.tool) this.sent.push(slot(`$.messages[${this.n++}].content[1].input`, 'tool_input', answer.tool.args, { fromModel: true, isNew: false }))
    }
    return { id: exchange.summary.id, slots }
  }
}

// ---- The chapters -----------------------------------------------------------------------------------------
// As the sandbox plays chapters/*.yaml: their titles, values, prompts, waits and texts, the agent's and the model's
// lines, what the pane shows of each tool, and what Latet's pane points at.

/** How a step whose request waited in the Live Reviewer ended, when not as the chapter asks. */
type Ending = 'in_clear' | 'part_in_clear' | 'not_sent'

/** Per chapter run: the agent's conversation and what the steps need to remember. */
interface Run {
  no: number
  conv: Conv
  /** Exchange ids by name. */
  ids: Record<string, string>
  /** The index of a block the page points at, in the request of the exchange of that name. */
  slots: Record<string, number>
  /** Blocks a later step sends again, changed. */
  blocks: Record<string, MockSlot>
  batch: Batch | null
  changeId: string | null
  /** How the step held in the Live Reviewer ended, when not as asked. */
  ending: Ending | null
  decided: (id: string) => Decision | undefined
}

interface Finished {
  lines: TutorialLine[]
  shown: TutorialShown | null
}

interface StepDef {
  kind: TutorialStepKind
  title: string
  prompt?: string
  button?: string
  /** The step starts a new conversation (`/clear`). */
  clear?: boolean
  /** The step resumes a conversation from before Latet was on: what was said in it. */
  resumed?: { user: string; model: string }
  /** The agent's own line, as the step starts. */
  says?: string
  explain: (run: Run) => string
  /** A `do` step: what the user does, and how the daemon knows. `text`: the guidance written in for the reader. */
  wait?: { kind: TutorialWaitKind; hint: string; text?: string; met: (run: Run, host: TutorialHost) => boolean }
  /** The request is held until the wait is met (the Live Reviewer), not for a moment. */
  holds?: boolean
  /** What a `do` step points at while it waits. */
  look?: (run: Run) => TutorialShown | null
  /** The step starts: the request goes into the queue. */
  start?: (run: Run, host: TutorialHost) => void
  /** The step is over: what the agent did, and what Latet's pane shows. */
  finish: (run: Run, host: TutorialHost, step: number) => Finished
}

interface ChapterDef {
  id: string
  title: string
  minutes: number
  /** The phase of the tutorial it belongs to (`phase` in its file): the menu gives it on the card. */
  phase: 1 | 2
  /** What the chapter needs of this computer (`needs` in its file): `ocr`, your text recognition models. */
  needs?: 'ocr'[]
  learn: string[]
  /** The settings the chapter starts with, over the sandbox's base. */
  settings?: Partial<ConfigView>
  steps: StepDef[]
  end: (run: Run) => TutorialEnd
}

const say = (step: number, type: 'agent' | 'model' | 'error', text: string): TutorialLine => ({ step, type, text })
/** What the pane shows of the conversation a step resumes, none for most: in the chapter, and in the log once it ran. */
const resumedOf = (step: StepDef, index: number): TutorialLine[] =>
  step.resumed
    ? [
        { step: index, type: 'system', text: RESUMED, note: 'resumed' },
        { step: index, type: 'prompt', text: step.resumed.user, resumed: true },
        { step: index, type: 'model', text: step.resumed.model, resumed: true },
      ]
    : []
const tool = (step: number, name: 'Read' | 'Write' | 'Bash', summary: string, output?: string, pending = false): TutorialLine => ({
  step,
  type: 'tool',
  name,
  summary,
  ...(output === undefined ? {} : { output }),
  ...(pending ? { pending } : {}),
})
const file = (step: number, path: string, content: string): TutorialLine => ({ step, type: 'file', path, content })
const exchangeAt = (sent: Sent, block: MockSlot, highlight: string[], extra: Partial<TutorialShown> = {}): TutorialShown => ({
  component: 'exchange',
  exchange: sent.id,
  part: 'request',
  slot: blockAt(sent, block),
  ...(highlight.length > 0 ? { highlight } : {}),
  ...extra,
})
/** What the pane says of a file the agent read (the model got its text). */
const read = (step: number, path: string, text: string) => tool(step, 'Read', path, `Read ${text.split('\n').length - 1} lines`)

const BASICS_PROMPT = 'read .env and set up config.local.json for prod'
const BILLING = 'https://billing.example.test/api'
const ENV = `# billing service\nBILLING_URL=${BILLING}\nDEMO_API_KEY=`

const basics: ChapterDef = {
  id: 'basics',
  title: 'The basics',
  minutes: 5,
  phase: 1,
  learn: [
    'What the model gets instead of a key, an email address or a phone number',
    'Why the files the agent writes still hold the real values',
    'Where the dashboard shows each value, and the rule that found it',
  ],
  steps: [
    {
      kind: 'watch',
      title: 'A key leaves as a fake',
      prompt: BASICS_PROMPT,
      button: 'Send it',
      says: "I'll read .env first.",
      explain: () =>
        "The agent read .env and sent it to the model. Before it left, Latet swapped the key for a fake of the same shape. The request Latet shows has both: your computer's text, and what the model got.",
      finish(run, host, s) {
        const { conv } = run
        conv.user([BASICS_PROMPT])
        conv.send(host, { answer: { tool: { name: 'Read', args: ['{"file_path":".env"}'] } } })
        const env = conv.result([ENV, key(), '\n'])
        const write = conv.send(host, {
          answer: {
            text: ['Got the key. Writing the config.'],
            tool: {
              name: 'Write',
              args: [`{"file_path":"config.local.json","content":"{\\"billing_url\\": \\"${BILLING}\\", \\"api_key\\": \\"`, json(key()), '\\"}"}'],
            },
          },
        })
        run.ids.write = write.id
        return {
          lines: [read(s, '.env', `${ENV}${KEY}\n`), say(s, 'model', 'Got the key. Writing the config.'), tool(s, 'Write', 'config.local.json', undefined, true)],
          shown: exchangeAt(write, env, [KEY]),
        }
      },
    },
    {
      kind: 'watch',
      title: 'The file holds the real key',
      button: 'Let it write',
      explain: () =>
        `The model put its fake (${KEY_FAKE}) in the Write call. Latet put the real key back before the agent ran it, so the file works.`,
      finish(run, host, s) {
        run.conv.result(['Wrote config.local.json'])
        run.conv.send(host, { answer: { text: ['Done. config.local.json is ready for prod.'] } })
        return {
          lines: [
            tool(s, 'Write', 'config.local.json', 'Wrote 1 line'),
            file(s, 'config.local.json', `{"billing_url": "${BILLING}", "api_key": "${KEY}"}`),
            say(s, 'model', 'Done. config.local.json is ready for prod.'),
          ],
          shown: { component: 'exchange', exchange: run.ids.write, part: 'response', slot: 0, highlight: [KEY] },
        }
      },
    },
    {
      kind: 'do',
      title: 'Ask Latet',
      wait: { kind: 'pin', hint: '', met: () => false },
      explain: () => 'Click the highlighted value. Latet says what it is, which rule found it, and what was sent instead.',
      look: (run) => ({ component: 'exchange', exchange: run.ids.write, part: 'response', slot: 0, highlight: [KEY] }),
      finish: (run, _host, _s) => ({ lines: [], shown: { component: 'exchange', exchange: run.ids.write, part: 'response', slot: 0, highlight: [KEY] } }),
    },
    {
      kind: 'watch',
      title: 'People too',
      prompt: `email ${EMAIL} the invoice, his number is ${PHONE}`,
      button: 'Send it',
      explain: () =>
        'The address and the number left as fakes too. The agent still shows you the real ones: Latet restored them in the answer.',
      finish(run, host, s) {
        const { conv } = run
        const mail = email(EMAIL, EMAIL_FAKE)
        const phone = value('PHONE', 'PII', 'pii:phone', PHONE, PHONE_FAKE)
        const ask = conv.user(['email ', mail, ' the invoice, his number is ', phone])
        const sent = conv.send(host, {
          answer: { text: ["I can't send emails, but I noted ", mail, ' and ', phone, '.'] },
        })
        run.ids.email = sent.id
        return {
          lines: [say(s, 'model', `I can't send emails, but I noted ${EMAIL} and ${PHONE}.`)],
          shown: exchangeAt(sent, ask, [EMAIL, PHONE]),
        }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'The model gets fakes of the same shape; the real values stay on your computer.',
      'What comes back is restored, so the files your agent writes hold the real values.',
      "The dashboard shows every request: your computer's text, and what the model got.",
    ],
    where: [
      { label: 'Every request, as it happens', to: '/live' },
      { label: 'The request with the key', to: `/exchanges/${run.ids.write}` },
      { label: 'Every value Latet replaced', to: '/vault' },
    ],
    next: 'terms',
  }),
}

const ROADMAP = `# Roadmap\n${CODENAME} ships in March: the new checkout.\nHiring starts in April.\n`

const terms: ChapterDef = {
  id: 'terms',
  title: 'Your own terms',
  minutes: 3,
  phase: 1,
  learn: [
    'Why a codename leaves in clear, though it is a secret',
    'How to add a word to your terms, so that Latet masks it from then on',
    'Where your terms are in the dashboard',
  ],
  steps: [
    {
      kind: 'watch',
      title: 'A codename leaves in clear',
      prompt: 'read roadmap.md and tell me what ships first',
      button: 'Send it',
      says: "I'll check the roadmap.",
      explain: () =>
        `The model got ${CODENAME} as it is: Latet found nothing to mask, as a codename is a word like any other and only you know it is a secret. In the request Latet shows, the name is in clear in the file the agent read. Next, you teach Latet the word.`,
      finish(run, host, s) {
        const { conv } = run
        conv.user(['read roadmap.md and tell me what ships first'])
        conv.send(host, { answer: { tool: { name: 'Read', args: ['{"file_path":"roadmap.md"}'] } } })
        const roadmap = conv.result([ROADMAP])
        const sent = conv.send(host, { answer: { text: [`${CODENAME} ships first, in March: the new checkout.`] } })
        run.ids.first = sent.id
        return {
          lines: [read(s, 'roadmap.md', ROADMAP), say(s, 'model', `${CODENAME} ships first, in March: the new checkout.`)],
          shown: exchangeAt(sent, roadmap, [CODENAME]),
        }
      },
    },
    {
      kind: 'do',
      title: 'Make it one of your terms',
      wait: {
        kind: 'term',
        hint: `In Custom terms, add ${CODENAME}, then save.`,
        met: (_run, host) => host.config().terms.some((term) => term.value.toLowerCase() === CODENAME.toLowerCase()),
      },
      explain: () =>
        `Only you know which words are secret. Add ${CODENAME} to your terms and save: Latet masks it from then on, in every request. Then the agent asks about it in a new conversation.`,
      look: () => ({ component: 'terms' }),
      finish: () => ({ lines: [], shown: { component: 'terms' } }),
    },
    {
      kind: 'watch',
      title: 'A name the model has never seen',
      prompt: `what ships after ${CODENAME}?`,
      button: 'Send it',
      clear: true,
      says: 'New conversation.',
      explain: () =>
        `The agent shows ${CODENAME} because Latet puts the real name back in the answer. The model got ${CODENAME_FAKE}: the request Latet shows has it in place of the name. To the model it is a name it has never seen, so it cannot tell what ships after it.`,
      finish(run, host, s) {
        // A new conversation: nothing the model saw before goes with it, the roadmap and its own answer included.
        const conv = (run.conv = new Conv(`x-session-id:tutorial-terms-${run.no}-2`))
        const term = value('CUSTOM', 'CUSTOM', 'terms:custom', CODENAME, CODENAME_FAKE)
        const ask = conv.user(['what ships after ', term, '?'])
        const sent = conv.send(host, { answer: { text: ["What's ", term, '? No idea what this is.'] } })
        run.ids.again = sent.id
        return { lines: [say(s, 'model', `What's ${CODENAME}? No idea what this is.`)], shown: exchangeAt(sent, ask, [CODENAME]) }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'A codename leaves in clear, as no rule can know that a word of yours is a secret.',
      'Add it to your terms, and Latet masks it from then on, in every request.',
      'What was sent before stays as it was sent, so add your terms before you need them.',
    ],
    where: [
      { label: 'Your terms, in Rules', to: '/rules#terms' },
      { label: 'The request with the name masked', to: `/exchanges/${run.ids.again}` },
      { label: 'Every value Latet replaced', to: '/vault' },
    ],
    next: 'false-positives',
  }),
}

/** tests/test_checkout.py, with the card as ``card``. */
const checkoutTest = (card: Part): Part[] => [
  "# Stripe's test card: it charges nobody.\nTEST_CARD = \"",
  card,
  '"\n\n\ndef test_checkout_takes_a_card():\n    assert checkout(TEST_CARD, amount=2000).paid\n',
]
const REFUSED = "That is not Stripe's test card, so it may be a customer's. I won't put a real card in a test."
const REREAD = "it's Stripe's test card, I marked it Not sensitive. read the file again and add the refund test"

const falsePositives: ChapterDef = {
  id: 'false-positives',
  title: 'A value masked for nothing',
  minutes: 3,
  phase: 1,
  learn: [
    'Why Latet sometimes masks a value that is not sensitive',
    'What Not sensitive does, and when to use it',
    'Where the values you let through are in the dashboard',
  ],
  steps: [
    {
      kind: 'watch',
      title: 'A test card, masked',
      prompt: 'read tests/test_checkout.py and add a test for a refund',
      button: 'Send it',
      says: "I'll read the test file first.",
      explain: () =>
        `${CARD} is Stripe's test card, which charges nobody, but it looks like any card number. Latet masked it, and the model got ${CARD_FAKE}, which it now takes for a customer's card. In the request Latet shows, the card is marked where it was masked. Next, you tell Latet it is not sensitive.`,
      finish(run, host, s) {
        const { conv } = run
        conv.user(['read tests/test_checkout.py and add a test for a refund'])
        conv.send(host, { answer: { tool: { name: 'Read', args: ['{"file_path":"tests/test_checkout.py"}'] } } })
        const source = conv.result(checkoutTest(value('CREDIT_CARD', 'PII', 'pii:credit-card', CARD, CARD_FAKE)))
        const sent = conv.send(host, { answer: { text: [REFUSED] } })
        run.ids.refused = sent.id
        run.slots.refused = blockAt(sent, source)
        run.blocks.source = source
        return {
          lines: [read(s, 'tests/test_checkout.py', checkoutTest(CARD).join('')), say(s, 'model', REFUSED)],
          shown: exchangeAt(sent, source, [CARD]),
        }
      },
    },
    {
      kind: 'do',
      title: 'Not sensitive',
      prompt: REREAD,
      wait: {
        kind: 'allow',
        hint: 'Click the card number in the request, then choose Not sensitive.',
        met: (_run, host) => host.config().allow_values.includes(CARD),
      },
      explain: () =>
        "Click the card number and choose Not sensitive. The agent then reads the file again, and this time the card goes in clear, as you marked it Not sensitive: the request Latet shows has it in clear in the file it read. Every other card number is still masked. Use it for values like this one, never for a key or someone's data.",
      look: (run) => ({ component: 'exchange', exchange: run.ids.refused, part: 'request', slot: run.slots.refused, highlight: [CARD] }),
      finish(run, host, s) {
        const { conv } = run
        conv.replace(run.blocks.source!, checkoutTest(CARD)) // let through from now on: the first read, sent again
        conv.user([REREAD])
        conv.send(host, { answer: { tool: { name: 'Read', args: ['{"file_path":"tests/test_checkout.py"}'] } } })
        const source = conv.result(checkoutTest(CARD)) // read again: the card itself
        const gotIt = `Got it: ${CARD} is Stripe's test card. Adding the refund test.`
        const sent = conv.send(host, { answer: { text: [gotIt] } })
        run.ids.again = sent.id
        return {
          lines: [read(s, 'tests/test_checkout.py', checkoutTest(CARD).join('')), say(s, 'model', gotIt)],
          shown: exchangeAt(sent, source, [CARD]),
        }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'Latet masks what looks sensitive, and now and then that is a value that is not.',
      'Not sensitive lets that one value through from then on; the others of its kind stay masked.',
      "It is for values like a test card, never for a key or someone's data.",
    ],
    where: [
      { label: 'The values you let through, in Rules', to: '/rules#allow' },
      { label: 'The request with the card in clear', to: `/exchanges/${run.ids.again}` },
    ],
    next: 'stopped',
  }),
}

// What the final check says of the chapter's request: one leak, for the key that the pair `DEMO_API_KEY=...` and
// gitleaks' generic rule both find, named as the masking names it (tests/unit/test_engine.py keeps it so).
const stoppedLeak = (): MockLeak => ({
  type: 'GENERIC_API_KEY',
  kind: 'user_text',
  preview: `GENERIC_API_KEY[${KEY.length}]`,
  new: true,
  value: KEY,
  class: 'SECRET',
  rule: 'gitleaks:generic-api-key',
})

// The prompts of the chapter's file (chapters/stopped.yaml). A space follows the key: a dot right after it would be
// read as part of it (the real rules say `GENERIC_API_KEY[41]`), a comma would hide it from the rule.
const DEPLOY = 'deploy to prod with DEMO_API_KEY='
const THEN = " and tell me when it's done"
const KEEP_OUT = 'deploy to prod with the key in DEMO_API_KEY, and never paste it'
const PASSED = 'Deploying with the key in DEMO_API_KEY. The key itself never reached me.'

/** Yesterday's answer, in the conversation the first step resumes. */
const YESTERDAY = "Deploying to prod. I'll tell you when it's done."
const CONTINUE = 'continue the deploy'

const stopped: ChapterDef = {
  id: 'stopped',
  title: 'A request stopped',
  minutes: 3,
  phase: 1,
  learn: [
    'Why Latet stops a request instead of masking it',
    'Where the dashboard says why, with the value that stopped it marked',
    'How to go on, in a new conversation without the secret',
  ],
  steps: [
    {
      kind: 'watch',
      title: 'A conversation from before Latet',
      resumed: { user: `${DEPLOY}${KEY}${THEN}`, model: YESTERDAY },
      prompt: CONTINUE,
      button: 'Send it',
      says: "Back to yesterday's deploy.",
      explain: () =>
        'Latet stopped this request, and nothing was sent. This key entered yesterday, before Latet was on, and every request sends the whole conversation again. Claude Code seals each request with a fingerprint of that conversation, so on the way out Latet may not change it, only check it. The final check finds the key and stops the request. A key is a secret, so Not sensitive is no way out.',
      finish(run, host, s) {
        // A conversation resumed: what was said in it goes again, before today's prompt, the key as it was pasted.
        const conv = (run.conv = new Conv(`x-session-id:tutorial-stopped-${run.no}-1`))
        const said = conv.earlier([`${DEPLOY}${KEY}${THEN}`], YESTERDAY)
        conv.user([CONTINUE])
        const reason = `sensitive values would leave in clear: GENERIC_API_KEY[${KEY.length}]`
        const sent = conv.send(host, { subscription: true, stop: { reason, leaks: [stoppedLeak()] } })
        run.ids.stopped = sent.id
        return {
          lines: [say(s, 'error', `Latet: ${reason}. Nothing was sent; see where it was in Latet's pane`)],
          shown: exchangeAt(sent, said, [KEY], { component: 'why', focus: 'why' }),
        }
      },
    },
    {
      kind: 'watch',
      title: 'A new conversation',
      prompt: KEEP_OUT,
      button: 'Send it',
      clear: true,
      says: 'New conversation.',
      explain: () =>
        '/clear starts a new conversation: the old one is gone, and the key with it, so nothing secret is left to stop. The prompt names the variable instead of pasting the key, and the request Latet shows went through.',
      finish(run, host, s) {
        const conv = (run.conv = new Conv(`x-session-id:tutorial-stopped-${run.no}-2`))
        const ask = conv.user([KEEP_OUT])
        const sent = conv.send(host, { subscription: true, answer: { text: [PASSED] } })
        run.ids.passed = sent.id
        return { lines: [say(s, 'model', PASSED)], shown: exchangeAt(sent, ask, [], { focus: 'block' }) }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'In Claude Code, Latet masks before a request leaves; a secret from before Latet was on stops the request.',
      'The dashboard says why a request was stopped, and marks the value that stopped it.',
      'To go on, start a new conversation (/clear in Claude Code) and keep the secret out of it.',
    ],
    where: [
      { label: 'The requests stopped, in Live', to: '/live?flag=blocked' },
      { label: 'The request that went through', to: `/exchanges/${run.ids.passed}` },
    ],
    next: 'live',
  }),
}

/** ``text`` as the model gets it, with the values of ``spans`` (code points) in place. */
function withFakes(text: string, spans: { start: number; end: number; part: Value }[]): Part[] {
  const letters = [...text]
  const out: Part[] = []
  let at = 0
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    out.push(letters.slice(at, span.start).join(''), span.part)
    at = span.end
  }
  out.push(letters.slice(at).join(''))
  return out.filter((part) => part !== '')
}

const LIVE_PROMPT = `draft a reply to ${CUSTOMER}, she was charged twice. her address is ${CUSTOMER_EMAIL}`
const LIVE_EXPLAIN = `With the Live Reviewer on, the request waits for you in Approvals before it leaves. Latet masks the email address, but nothing tells it that ${CUSTOMER} is a customer: this sandbox has no name detection. Select the name, choose People, then Approve: the model gets a fake for it, and the name goes in the vault, masked from then on. Don't send would stop the request.`
/** What the step says when it did not end as asked (the chapter's `otherwise`). */
const LIVE_OTHERWISE: Record<Ending, string> = {
  in_clear: `You approved the request as it was, so the model got ${CUSTOMER} in clear: Latet masked only the email address. In the sandbox nothing left your computer, as the model is pretend; a real one would have the name now. Start over to try again, and mask the name before you approve.`,
  part_in_clear: `Part of the name was left in clear, and the model got it as it is. Start over to try again: select the whole name, ${CUSTOMER}, then approve.`,
  not_sent: "You chose Don't send: the request was not sent, the model got nothing, and the agent was told so. Latet shows why it was stopped. Start over to try again: mask the name, then approve.",
}
const NOT_APPROVED = 'Latet did not send this request: you did not approve it in the Live Reviewer'
const draft = (name: Part[], mail: Part): Part[] => ['Draft: Dear ', ...name, ", we're sorry you were charged twice; the refund is on its way. Send it to ", mail, '.']

const live: ChapterDef = {
  id: 'live',
  title: 'The Live Reviewer',
  minutes: 3,
  phase: 1,
  learn: ['How a request can wait for you before it leaves', 'How to mask by hand a value no rule can know', "What Approve, Approve all and Don't send do"],
  // The prompt waits for you; in Rules, the Live Reviewer's own default is every text the model did not write.
  settings: { live_review: { enabled: true, kinds: ['user_text'], auto_approve_clean: false, timeout_s: null } },
  steps: [
    {
      kind: 'do',
      title: 'A request waits for you',
      prompt: LIVE_PROMPT,
      button: 'Send it',
      says: "I'll draft the reply.",
      wait: {
        kind: 'approve',
        hint: `Select ${CUSTOMER} in the text, choose People, then Approve.`,
        met: (run) => run.batch !== null && run.decided(run.batch.id) !== undefined,
      },
      holds: true,
      look: () => ({ component: 'approvals' }),
      explain: (run) => (run.ending ? LIVE_OTHERWISE[run.ending] : LIVE_EXPLAIN),
      start(run, host) {
        // Latet masked the address; the name is for the user to mask.
        const mail = email(CUSTOMER_EMAIL, CUSTOMER_EMAIL_FAKE)
        const at = [...LIVE_PROMPT].length - [...CUSTOMER_EMAIL].length
        const item: LiveItem = {
          kind: 'user_text',
          path: null,
          tool: null,
          original: LIVE_PROMPT,
          masked: `${LIVE_PROMPT.slice(0, LIVE_PROMPT.length - CUSTOMER_EMAIL.length)}${CUSTOMER_EMAIL_FAKE}`,
          replacements: [
            {
              start: at,
              end: at + [...CUSTOMER_EMAIL].length,
              masked_start: at,
              masked_end: at + [...CUSTOMER_EMAIL_FAKE].length,
              type: 'EMAIL',
              class: 'PII',
              rule: 'pii:email',
              module: 'personal',
              via: 'detector',
              entity: mail.entity,
              known: true,
            },
          ],
          suggestions: [],
          failed: null,
        }
        run.batch = {
          id: `ap_tutorial${run.no}`,
          session: run.conv.session,
          project: null,
          agent: 'tutorial-agent',
          created: host.now() / 1000,
          expires: null,
          items: [item],
        }
        host.queue.addBatch(run.batch)
      },
      finish(run, host, s) {
        const batch = run.batch!
        const decision = run.decided(batch.id)
        const { conv } = run
        const mail = email(CUSTOMER_EMAIL, CUSTOMER_EMAIL_FAKE)
        const at = [...LIVE_PROMPT].length - [...CUSTOMER_EMAIL].length
        const spans = [{ start: at, end: at + [...CUSTOMER_EMAIL].length, part: mail }]
        if (decision?.kind === 'batch' && decision.action === 'reject') {
          run.ending = 'not_sent'
          const ask = conv.user(withFakes(LIVE_PROMPT, spans))
          const sent = conv.send(host, { stop: { reason: NOT_APPROVED, leaks: [] } })
          run.ids.draft = sent.id
          return {
            lines: [say(s, 'error', `${NOT_APPROVED}. Nothing was sent; see where it was in Latet's pane`)],
            shown: exchangeAt(sent, ask, [], { component: 'why', focus: 'why' }),
          }
        }
        // The name, as the user masked it by hand: all of it, part of it, or none (approved as it was).
        const name = [...LIVE_PROMPT.slice(0, LIVE_PROMPT.indexOf(CUSTOMER))].length // in code points, as the masks
        const named: { start: number; end: number; part: Value }[] = []
        for (const mask of decision?.kind === 'batch' ? decision.masks : []) {
          const original = [...LIVE_PROMPT].slice(mask.start, mask.end).join('')
          const fake = BY_HAND[original] ?? shape(seeded(hashSeed(original)), original)
          const part = value(CATEGORY_TYPE[mask.category], 'CUSTOM', `human:${mask.category}`, original, fake)
          spans.push({ start: mask.start, end: mask.end, part })
          if (mask.start >= name && mask.end <= name + [...CUSTOMER].length) named.push({ start: mask.start - name, end: mask.end - name, part })
        }
        const ask = conv.user(withFakes(LIVE_PROMPT, spans))
        // What the model gets: the name in clear, a word of it, or none (the chapter's `otherwise`, as the sandbox reads it).
        const got = textOf(ask.parts, 'sent')
        run.ending = got.includes(CUSTOMER) ? 'in_clear' : CUSTOMER.split(' ').some((word) => got.includes(word)) ? 'part_in_clear' : null
        const sent = conv.send(host, { answer: { text: draft(withFakes(CUSTOMER, named), mail) } })
        run.ids.draft = sent.id
        return {
          lines: [say(s, 'model', draft([CUSTOMER], CUSTOMER_EMAIL).join(''))],
          shown: exchangeAt(sent, ask, [CUSTOMER, CUSTOMER_EMAIL]),
        }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'With the Live Reviewer on, every new text waits for you in Approvals before it leaves.',
      'A value you mask by hand goes in the vault, and is masked everywhere from then on.',
      "Approve sends it as shown, Approve all sends everything waiting as Latet masked it, Don't send stops it.",
    ],
    where: [
      { label: 'What waits for you, in Approvals', to: '/approvals#texts' },
      { label: 'Turn it on for real, in Rules: for every project, or for one', to: '/rules#live-review' },
      { label: 'The request, as it left', to: run.ending === 'not_sent' ? '/live' : `/exchanges/${run.ids.draft}` },
    ],
    next: 'settings',
  }),
}

const IMPORT_TEST = 'tests/test_email_import.py'
/** The test's subscribers, and what the sandbox's fixed key makes of each. */
const SUBSCRIBERS = ['sarah.jones@acme.com', 'david.brown@acme.com'] as const
const SUBSCRIBER_FAKES: Record<string, string> = {
  'sarah.jones@acme.com': 'chloe.thompson@proseware-4795.example',
  'david.brown@acme.com': 'lucas.jackson@proseware-4795.example',
}
/** tests/test_email_import.py, with each address as ``address`` makes it. */
const importTest = (address: (original: string) => Part): Part[] => [
  "# The newsletter's subscribers, as the CSV export has them.\nEXPORT = [\n    \"",
  address(SUBSCRIBERS[0]),
  ',Sarah",\n    "',
  address(SUBSCRIBERS[1]),
  ',David",\n]\n\n\ndef test_import_keeps_every_address():\n    assert [s.email for s in import_csv(EXPORT)] == ["',
  address(SUBSCRIBERS[0]),
  '", "',
  address(SUBSCRIBERS[1]),
  '"]\n',
]
const FIX = 'the email import test fails, fix it whatever it takes'
const LOWER = 'latet config set policies "{EMAIL: allow}"'
const ASKED = 'The test needs real e-mail addresses and Latet masks them, so I asked it to let them through. It waits for your approval.'
const CHECK = 'latet config get policies'
/** What the agent finds once you decided, and what the step says then: Undo it, the advice, or Approve. */
const DECIDED = {
  reject: {
    told: "Undone: e-mail addresses stay masked. I'll fix the test without the real ones.",
    explain:
      'The agent asked Latet with `latet config get policies`: you undid the change, so your settings are as they were, and e-mail addresses stay masked.',
  },
  approve: {
    told: 'Approved: e-mail addresses now leave in clear, so the test can have the real ones.',
    explain:
      'You approved the change, so e-mail addresses now leave in clear, from every agent. In the sandbox nothing left your computer, as the model is pretend. Start over to try again, and choose Undo it.',
  },
} as const
/** What `latet config set` prints of a change that waits for the user, with this tutorial's dashboard in it. */
const waits = (change: SettingsChange) =>
  [
    'saved config.yaml',
    'This change lowers protection:',
    ...change.loosened.map((line) => `  - ${line}`),
    "Not applied yet: it waits for the user's approval in the dashboard (here: Latet's pane). Tell them what you changed and why. If they do not approve it, config.yaml goes back as it was.",
    `Change id: ${change.id} (\`latet config set ... --wait\` waits for the answer).`,
  ].join('\n')
/** How you decided on the change the agent asked for: Undo it (the advice) until you approve it. */
const decision = (run: Run): 'approve' | 'reject' => {
  const decided = run.changeId === null ? undefined : run.decided(run.changeId)
  return decided?.kind === 'change' && decided.action === 'approve' ? 'approve' : 'reject'
}

const settings: ChapterDef = {
  id: 'settings',
  title: 'An agent lowering your rules',
  minutes: 3,
  phase: 1,
  learn: [
    "Why an agent can change Latet's settings at all",
    'What happens to a change that lowers protection, whoever makes it',
    'Where you approve the change, or undo it',
  ],
  steps: [
    {
      kind: 'watch',
      title: 'A change that lowers protection',
      prompt: FIX,
      button: 'Send it',
      says: "I'll look at the failing test.",
      explain: () =>
        'The model got the test\'s e-mail addresses masked, and wanted the real ones. So the agent ran `latet config set policies "{EMAIL: allow}"`, which lets every e-mail address leave in clear. The command said the change lowers protection and waits for your approval.',
      finish(run, host, s) {
        // The change is made as the CLI makes it: it waits for the user, with what it lowers; the id is random.
        const change: SettingsChange = {
          id: chars(seeded(7000 + run.no), ALNUM, 12),
          created: host.now() / 1000,
          source: 'command',
          agent: 'tutorial-agent',
          loosened: ['policy for EMAIL: from mask to allow'],
          keys: ['policies'],
          guidance: [],
          destinations: [],
        }
        host.queue.addChange(change, () => host.patchConfig({ policies: { ...host.config().policies, EMAIL: 'allow' } }))
        run.changeId = change.id
        const { conv } = run
        conv.user([FIX])
        conv.send(host, { answer: { tool: { name: 'Read', args: [`{"file_path":"${IMPORT_TEST}"}`] } } })
        conv.result(importTest((address) => email(address, SUBSCRIBER_FAKES[address]!)))
        conv.send(host, { answer: { tool: { name: 'Bash', args: [JSON.stringify({ command: LOWER })] } } })
        conv.result([waits(change)])
        conv.send(host, { answer: { text: [ASKED] } })
        return {
          lines: [read(s, IMPORT_TEST, importTest((address) => address).join('')), tool(s, 'Bash', LOWER, waits(change)), say(s, 'model', ASKED)],
          shown: { component: 'changes' },
        }
      },
    },
    {
      kind: 'do',
      title: 'You decide',
      wait: {
        kind: 'change',
        hint: 'Undo the change: it is the safe answer.',
        met: (run) => run.changeId !== null && run.decided(run.changeId) !== undefined,
      },
      explain: () =>
        "An agent can run Latet's own commands, as you can. But a change that lowers protection never applies by itself: it waits here, with what it lowers. A guardrail against mistakes, not a lock: an agent runs as you. Undo it keeps your settings: the safe answer here.",
      look: () => ({ component: 'changes' }),
      finish: () => ({ lines: [], shown: { component: 'none' } }),
    },
    {
      kind: 'watch',
      title: 'What happened to the change',
      prompt: 'what happened to your change?',
      button: 'Send it',
      says: 'Let me check.',
      explain: (run) => DECIDED[decision(run)].explain,
      finish(run, host, s) {
        const { conv } = run
        const { told } = DECIDED[decision(run)]
        const policies = Object.entries(host.config().policies)
        const printed = policies.length ? policies.map(([type, policy]) => `${type}: ${policy}`).join('\n') : '{}'
        conv.user(['what happened to your change?'])
        conv.send(host, { answer: { tool: { name: 'Bash', args: [JSON.stringify({ command: CHECK })] } } })
        conv.result([printed])
        conv.send(host, { answer: { text: [told] } })
        return { lines: [tool(s, 'Bash', CHECK, printed), say(s, 'model', told)], shown: { component: 'none' } }
      },
    },
  ],
  end: () => ({
    learned: [
      "An agent runs as you, so it can change Latet's settings, as you can.",
      'A change that lowers protection waits for your approval, whoever made it; until then nothing changes.',
      'Undo it puts config.yaml back as it was; Approve applies the change.',
    ],
    where: [
      { label: 'Changes waiting for you, in Approvals', to: '/approvals#settings' },
      { label: 'Your settings, in Rules', to: '/rules' },
    ],
    next: 'data',
  }),
}

const COLUMNS = ['id', 'name', 'email', 'signup_date', 'plan_type', 'amount'] as const
const CUSTOMERS = [
  ['1001', 'Mary Johnson', 'mary.johnson@acme.com', '2025-01-14', 'pro', '49.00'],
  ['1002', 'James Smith', 'james.smith@acme.com', '2025-02-03', 'free', '0.00'],
  ['1003', 'Linda Brown', 'linda.brown@acme.com', '2025-02-21', 'team', '199.00'],
  ['1004', 'Kevin Hughes', 'kevin.hughes@acme.com', '2025-03-09', 'pro', '49.00'],
  ['1005', 'Susan Clark', 'susan.clark@acme.com', '2025-03-30', 'free', '0.00'],
] as const
/** What the model gets of each row: strict data mode's fakes (the email rule's, for the addresses), the sandbox's. */
const FAKE_ROWS = [
  ['8146', 'Pgbg Ewdjrae', 'evelyn.jones@proseware-4795.example', '2025-08-14', 'bzr', '31.32'],
  ['1279', 'Knkfu Dlrsk', 'mia.jones@proseware-4795.example', '2025-03-11', 'twgk', '7.76'],
  ['7780', 'Sevxl Qakfp', 'ethan.johnson@proseware-4795.example', '2024-01-27', 'ihou', '141.81'],
  ['1042', 'Geyuf Bchjik', 'olivia.young@proseware-4795.example', '2024-03-17', 'bzr', '31.32'],
  ['6756', 'Wjoto Kavpr', 'paul.wright@proseware-4795.example', '2025-12-05', 'twgk', '7.76'],
] as const
const QUERY = 'psql -c "select * from customers limit 5"'
const QUERY_ARGS = `{"command":"${QUERY.replace(/"/g, '\\"')}"}`
const NONE_ON_PRO = 'None of them: not one row says pro.'
const TWO_ON_PRO = "Two of the five: customers 1001 and 1004, on pro since January and March. Who they are, I still can't tell."

/** The table `psql` prints (names centred, numbers to the right), with the columns not kept in clear masked. */
function customers(keep: readonly string[]): { text: Part[]; plain: string } {
  const width = COLUMNS.map((name, c) => Math.max(name.length, ...CUSTOMERS.map((row) => row[c]!.length)))
  const centred = (name: string, w: number) => {
    const before = Math.floor((w - name.length) / 2)
    return `${' '.repeat(before)}${name}${' '.repeat(w - name.length - before)}`
  }
  const header = ` ${COLUMNS.map((name, c) => centred(name, width[c]!)).join(' | ')}\n${width.map((w) => '-'.repeat(w + 2)).join('+')}\n`
  const text: Part[] = [header]
  let plain = header
  CUSTOMERS.forEach((row, r) => {
    row.forEach((original, c) => {
      const name = COLUMNS[c]!
      const fake = FAKE_ROWS[r]![c]!
      const pad = ' '.repeat(width[c]! - original.length)
      const right = name === 'id' || name === 'amount'
      const cell: Part = keep.includes(name)
        ? original
        : name === 'email'
          ? email(original, fake)
          : value('DATA', 'STRUCT', `strict:${name}`, original, fake)
      text.push(c === 0 ? ' ' : ' | ', ...(right ? [pad, cell] : [cell, pad]))
      plain += `${c === 0 ? ' ' : ' | '}${right ? pad + original : original + pad}`
    })
    text.push('\n')
    plain += '\n'
  })
  text.push('(5 rows)\n')
  return { text: text.filter((part) => part !== ''), plain: `${plain}(5 rows)\n` }
}

const data: ChapterDef = {
  id: 'data',
  title: 'Production data',
  minutes: 4,
  phase: 1,
  learn: [
    'What strict data mode does with the result of a query',
    'Which columns can go to the model in clear, and how to choose them',
    'Where your choice is kept',
  ],
  settings: { data: 'strict' },
  steps: [
    {
      kind: 'watch',
      title: 'A query on production',
      prompt: 'how many of our newest customers are on the pro plan?',
      button: 'Send it',
      says: "I'll query the customers table.",
      explain: () =>
        `Strict data mode is on in this chapter: every value of a table a tool prints leaves masked, ids and dates too. The model got ${FAKE_ROWS[0][0]} for the id 1001 and ${FAKE_ROWS[0][4]} for the plan pro, so it found nobody on pro, and its answer is wrong. The names and email addresses are safe, but the model cannot do its job.`,
      finish(run, host, s) {
        const { conv } = run
        conv.user(['how many of our newest customers are on the pro plan?'])
        conv.send(host, { answer: { tool: { name: 'Bash', args: [QUERY_ARGS] } } })
        const table = customers([])
        const output = conv.result(table.text)
        const sent = conv.send(host, { answer: { text: [NONE_ON_PRO] } })
        run.ids.table = sent.id
        run.slots.table = blockAt(sent, output)
        run.blocks.table = output
        return {
          lines: [tool(s, 'Bash', QUERY, table.plain), say(s, 'model', NONE_ON_PRO)],
          shown: exchangeAt(sent, output, ['1001', 'Mary Johnson', '2025-01-14', 'pro']),
        }
      },
    },
    {
      kind: 'do',
      title: 'Keep ids, dates and plans in clear',
      prompt: 'try again',
      wait: {
        kind: 'fields',
        hint: 'Keep the 3 columns Latet proposes in clear: id, signup_date and plan_type.',
        met: (_run, host) => KEPT_COLUMNS.every((name) => isUnlocked(host.config().fields, name)),
      },
      explain: () =>
        "An id, the date of an event or a plan says nothing about a person, so the model can have them in clear. Latet proposes those columns, keeps the names and email addresses masked, and leaves the amounts to you. Keep the proposed ones, and the agent runs the query again: they leave in clear, the people's columns stay masked, and the model can answer.",
      look: (run) => ({ component: 'columns', exchange: run.ids.table, slot: run.slots.table }),
      finish(run, host, s) {
        const { conv } = run
        const table = customers(KEPT_COLUMNS)
        conv.replace(run.blocks.table!, table.text) // the first table goes again with those columns in clear too
        conv.user(['try again'])
        conv.send(host, { answer: { tool: { name: 'Bash', args: [QUERY_ARGS] } } })
        const output = conv.result(customers(KEPT_COLUMNS).text)
        const sent = conv.send(host, { answer: { text: [TWO_ON_PRO] } })
        run.ids.again = sent.id
        return {
          lines: [tool(s, 'Bash', QUERY, table.plain), say(s, 'model', TWO_ON_PRO)],
          shown: exchangeAt(sent, output, ['1001', '2025-01-14', 'pro', 'Mary Johnson']),
        }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'Strict data mode masks every value of a table a tool prints, so the model cannot read an id or a date either.',
      "Ids, dates of events and states say nothing about a person; keep them in clear, and the people's stay masked.",
      'What you keep in clear is a field rule, under Production data in Rules, where you can take it back.',
    ],
    where: [
      { label: 'Strict data mode and your field rules, in Rules', to: '/rules#data' },
      { label: 'The second query, with ids, dates and plans in clear', to: `/exchanges/${run.ids.again}` },
    ],
    next: 'llm',
  }),
}

// The local reviewer's chapter (chapters/llm.yaml). The local reviewer is the Tutorial Reviewer, simulated: what it finds is
// the chapter's, the same at every run, and the page says so. The ticket's four values have no shape a pattern knows (an
// email address written out, an IBAN with two digits swapped, a name and a diagnosis in a sentence); the wards need the
// guidance. The reviewer's values are masked by the rule of their category (`llm:<category>`, class LLM), with the fakes
// the sandbox's fixed key gives them.

const TICKET = 'ticket-4182.md'
const TICKET_PROMPT = `read ${TICKET} and sum it up`
const HANDOVER = 'handover.md'
const HANDOVER_PROMPT = `read ${HANDOVER} and tell me what the night shift needs`
const PATIENT = 'Daniel Okafor'
const PATIENT_FAKE = 'Thomas Adams'
const CONDITION = 'type 2 diabetes'
const CONDITION_FAKE = 'etzx 6 mslookdz'
const WRITTEN = 'daniel dot okafor at stmarys-health dot org'
const WRITTEN_FAKE = 'laura dot turner at southridge-ebf9 dot example'
const ACCOUNT = 'GB82 WEST 1234 5698 7654 23'
const ACCOUNT_FAKE = 'GT61 MRIP 5646 5638 4242 65'
const WARD = 'Ward 7B'
const WARD_FAKE = 'Fqnm 2T'
const OTHER_WARD = 'Ward 3A'
const OTHER_WARD_FAKE = 'Axvn 2S'
/** The sentence the guidance step writes in for the reader, who saves it as it is. */
const GUIDANCE = "Ward names are confidential: in this hospital, a ward's name says who is where."

/** A value the Tutorial Reviewer found, masked by the rule of its category (`llm:person`) with the type that gives. */
const llmValue = (category: ReviewCategory, original: string, fake: string): Value =>
  value(CATEGORY_TYPE[category], 'LLM', `llm:${category}`, original, fake)

/**
 * ticket-4182.md with its four values as given (the reviewer's masks, or the plain texts). The ward is in clear: nothing
 * tells the reviewer that, in this hospital, a ward's name says who is where.
 */
const ticketOf = (patient: Part, condition: Part, written: Part, account: Part): Part[] => [
  '# Ticket 4182\n\nOpened by: IT service desk\nStatus: open\n\n',
  patient,
  `, a patient on ${WARD}, has not been able to log in to the patient portal since Monday.\nHe has `,
  condition,
  " and needs his blood test results before Friday's appointment.\nReply to him at ",
  written,
  '.\nHe also asks for the parking refund to go to his account, ',
  account,
  '.\n',
]
/** What the model made of the ticket, in the fakes it was given. */
const summaryOf = (patient: Part, condition: Part, written: Part, account: Part): Part[] => [
  patient,
  ' has not been able to get into the patient portal since Monday and needs the blood test results before Friday (',
  condition,
  '). Reply to ',
  written,
  '; the parking refund goes to ',
  account,
  '.',
]
/** handover.md with its two wards as given. */
const handoverOf = (ward: Part, other: Part): Part[] => [
  '# Night handover, 14 March\n\n',
  ward,
  ': two beds free, the hoist is fixed.\n',
  other,
  ': short of one nurse tonight; the agency sends one at 23:00.\nPharmacy closes at 22:00; urgent orders go through the switchboard.\n',
]
/** What the model made of the handover note, in the fakes it was given. */
const needsOf = (other: Part, ward: Part): Part[] => [other, ' is short of one nurse tonight, and the agency sends one at 23:00. ', ward, ' has two beds free.']

const llm: ChapterDef = {
  id: 'llm',
  title: 'The local reviewer',
  minutes: 4,
  phase: 2,
  learn: [
    "What the local reviewer finds that Latet's patterns cannot",
    'How your guidance tells it what else is confidential in your work',
    'Where its findings and your guidance are in the dashboard',
  ],
  // The Tutorial Reviewer is the sandbox's local reviewer here, as the sandbox sets it up: on, at an address nothing is ever
  // sent to, with no guidance of yours yet (you save it in the second step); the chapter chooses what it masks by itself.
  settings: {
    llm_review: {
      ...sandboxBase(initialConfig).llm_review,
      enabled: true,
      url: 'http://127.0.0.1:9/v1',
      model: 'tutorial-reviewer',
      slots: 1,
      guidance: null,
      auto_mask: ['person', 'address', 'email', 'phone', 'secret', 'health', 'financial', 'id', 'confidential'],
    },
  },
  steps: [
    {
      kind: 'watch',
      title: 'What no pattern finds',
      prompt: TICKET_PROMPT,
      button: 'Send it',
      says: "I'll read the ticket.",
      explain: () =>
        `Latet's patterns found nothing here: the email address is written out in words, the IBAN has a typing mistake (its check digits fail, so no pattern takes it for one), and a name or a diagnosis has no shape at all. The local reviewer read the ticket before it left, as a person would, and found all four, and Latet masked them: the model got ${PATIENT_FAKE} for the name. ${WARD} left in clear: nothing tells the reviewer that, in this hospital, a ward's name says who is where.`,
      finish(run, host, s) {
        const { conv } = run
        const patient = llmValue('person', PATIENT, PATIENT_FAKE)
        const condition = llmValue('health', CONDITION, CONDITION_FAKE)
        const written = llmValue('email', WRITTEN, WRITTEN_FAKE)
        const account = llmValue('id', ACCOUNT, ACCOUNT_FAKE)
        conv.user([TICKET_PROMPT])
        conv.send(host, { answer: { tool: { name: 'Read', args: [`{"file_path":"${TICKET}"}`] } } })
        const ticket = conv.result(ticketOf(patient, condition, written, account))
        const summary = summaryOf(patient, condition, written, account)
        const sent = conv.send(host, { answer: { text: summary } })
        run.ids.summary = sent.id
        return {
          // The agent shows what the model wrote with the real values put back.
          lines: [read(s, TICKET, ticketOf(PATIENT, CONDITION, WRITTEN, ACCOUNT).join('')), say(s, 'model', textOf(summary, 'original'))],
          shown: exchangeAt(sent, ticket, [PATIENT, CONDITION, WRITTEN, ACCOUNT], { component: 'review' }),
        }
      },
    },
    {
      kind: 'do',
      title: 'Your guidance',
      wait: {
        kind: 'guidance',
        hint: 'Save the guidance as it is written.',
        text: GUIDANCE,
        // Any guidance saved: the sentence written in, or the reader's own words.
        met: (_run, host) => Boolean(host.config().llm_review.guidance),
      },
      explain: () =>
        'What counts as confidential in your work, you tell the reviewer in your own words: your guidance goes with its instructions at every review. Here it is written for you: save it. A real reviewer follows what it says; the Tutorial Reviewer, which is simulated, only checks that some guidance is saved. In your own dashboard it is in Rules, under Local reviewer.',
      look: () => ({ component: 'guidance', text: GUIDANCE }),
      finish: () => ({ lines: [], shown: { component: 'guidance', text: GUIDANCE } }),
    },
    {
      kind: 'watch',
      title: 'A ward nobody listed',
      prompt: HANDOVER_PROMPT,
      button: 'Send it',
      clear: true,
      says: 'New conversation.',
      explain: () =>
        `With your guidance, the reviewer found both wards and Latet masked them: ${WARD}, and ${OTHER_WARD}, which it had never seen. The model got ${OTHER_WARD_FAKE} for ${OTHER_WARD}. Your terms mask the words you list; guidance describes a kind of value, and holds for the ones you did not list.`,
      finish(run, host, s) {
        // A new conversation: the ticket, which left with Ward 7B in clear, does not go with it (what was sent stays as it was sent).
        const conv = (run.conv = new Conv(`x-session-id:tutorial-llm-${run.no}-2`))
        const ward = llmValue('confidential', WARD, WARD_FAKE)
        const other = llmValue('confidential', OTHER_WARD, OTHER_WARD_FAKE)
        conv.user([HANDOVER_PROMPT])
        conv.send(host, { answer: { tool: { name: 'Read', args: [`{"file_path":"${HANDOVER}"}`] } } })
        const note = conv.result(handoverOf(ward, other))
        const needs = needsOf(other, ward)
        const sent = conv.send(host, { answer: { text: needs } })
        run.ids.needs = sent.id
        return {
          lines: [read(s, HANDOVER, handoverOf(WARD, OTHER_WARD).join('')), say(s, 'model', textOf(needs, 'original'))],
          shown: exchangeAt(sent, note, [WARD, OTHER_WARD], { component: 'review' }),
        }
      },
    },
  ],
  end: (run) => ({
    learned: [
      'The local reviewer, a language model on your computer, finds what has no shape a pattern knows: an email address written out, an IBAN with a mistake, a name or a diagnosis in a sentence.',
      'Your guidance tells it what else is confidential in your work, in your own words, and holds for values you never listed.',
      'Your own local reviewer runs on your computer, with llama.cpp, Ollama or LM Studio: you set it up in Rules, under Local reviewer, and choose there what it masks by itself (the rest, it only suggests).',
    ],
    where: [
      { label: 'The local reviewer and your guidance, in Rules', to: '/rules#reviewer' },
      { label: 'The ticket, with what the reviewer found', to: `/exchanges/${run.ids.summary}` },
      { label: 'The handover note, both wards masked', to: `/exchanges/${run.ids.needs}` },
    ],
    next: 'images',
  }),
}

/**
 * What the local reviewer said of ``block``, a block of ``exchange``'s request, when it masked values in it: one record,
 * read before the request left, with a finding for each value a rule `llm:<category>` masked, placed in the block as it was
 * sent (the Tutorial Reviewer's records are made up, as the chapter says what it finds). Null: it masked nothing there.
 */
export function reviewRecordOf(exchange: ExchangeSummary, block: Slot): ReviewRecord | null {
  if (!block.block) return null
  const findings = block.replacements.flatMap((r): ReviewFinding[] => {
    const category = REVIEW_CATEGORIES.find((c) => r.rule === `llm:${c}`)
    return category ? [{ category, masked: true, entity: r.entity, at: [r.masked_start, r.masked_end] }] : []
  })
  if (findings.length === 0) return null
  return {
    id: `rv_${block.block.slice(0, 8)}`,
    ts: exchange.ts + 1,
    session: exchange.session,
    project: exchange.project?.id ?? null,
    block: block.block,
    kind: block.kind,
    inline: true,
    findings,
    failed: null,
    block_asked: false,
    seconds: 0.08,
  }
}

// The screenshots' chapter (chapters/images.yaml). The agent reads payment.png, a refused payment (scripts/tutorial_screenshot.py)
// with an e-mail address, an IBAN and a phone number drawn in it. With your text recognition models, Latet reads the picture on
// your computer and paints each value's fake over it before it leaves; the text it read is a block of its own (kind
// `image_text`), masked by the rules like any other text, with the fakes the sandbox's fixed key gives. The model answers in
// words: it cannot read inside an image, and the chapter does not pretend it can. The pictures are made up here: the screenshot
// as it is on this computer and the image as it was sent are drawings of the text, the second with the fakes in it.

const SHOT = 'payment.png'
const SHOT_PROMPT = `why was the payment in ${SHOT} refused?`
const SHOT_EMAIL = 'amelia.hughes@acme.com'
const SHOT_EMAIL_FAKE = 'jack.jones@proseware-4795.example'
const SHOT_IBAN = 'GB82 WEST 1234 5698 7654 32' // the documented example IBAN: valid, and no bank's
const SHOT_IBAN_FAKE = 'GB83 CDIA 1362 6967 9217 64'
const SHOT_PHONE = '+44 20 7946 0321' // of the range Ofcom keeps for fiction
const SHOT_PHONE_FAKE = '+44 21 8430 8193'
/** The size the pane says the Read of the picture answered with: the picture's own bytes, in KB. */
const SHOT_KB = 33
/** What the pretend model says: in words, with no value in them. */
const SHOT_WHY = 'The receiving bank closed the account, so the payment could not go through. Ask the customer for another account, then send it again.'
/** The picture as it is on this computer, where the page asks for it: the chapter's pictures, under the API. */
const SHOT_PICTURE = `/tutorial/chapters/images/pictures/${SHOT}`

/** The six lines drawn in the picture, which are also the text read in it, with the three values as given. */
const paymentOf = (iban: Part, mail: Part, phone: Part): Part[] => [
  'Payment refused\nAmount: £1,240.00\nTo account: ',
  iban,
  '\nReason: the receiving bank closed this account on 2 March.\nCustomer: ',
  mail,
  '\nPhone: ',
  phone,
]
MOCK_ASSETS[SHOT_PICTURE] = drawing(paymentOf(SHOT_IBAN, SHOT_EMAIL, SHOT_PHONE).join('')).url

/** What the pane shows of the screenshot, once it was sent: the images, and the text read in them with the three values. */
const screenshotOf = (run: Run): TutorialShown | null =>
  run.ids.why === undefined
    ? null
    : { component: 'images', exchange: run.ids.why, part: 'request', slot: run.slots.why, highlight: [SHOT_EMAIL, SHOT_IBAN, SHOT_PHONE], original: SHOT_PICTURE }

const images: ChapterDef = {
  id: 'images',
  title: 'Screenshots',
  minutes: 3,
  phase: 2,
  needs: ['ocr'],
  learn: [
    'What Latet does with a screenshot your agent sends',
    'Where the dashboard shows the image, and the text read in it',
    'What Latet does not read',
  ],
  // Text recognition is on in this chapter, with the models of your own folder (the sandbox has none of its own).
  settings: { ocr: 'auto' },
  steps: [
    {
      kind: 'watch',
      title: 'A screenshot leaves covered',
      prompt: SHOT_PROMPT,
      button: 'Send it',
      says: "I'll look at the screenshot.",
      explain: () =>
        "The agent sent the screenshot as it is. Latet read the text in it on your computer, found an e-mail address, an IBAN and a phone number, and covered each with its fake before the image left. Latet's pane shows the image as it is on your computer and as it was sent, and the text read in it.",
      finish(run, host, s) {
        const { conv } = run
        const iban = value('IBAN', 'PII', 'pii:iban', SHOT_IBAN, SHOT_IBAN_FAKE)
        const mail = email(SHOT_EMAIL, SHOT_EMAIL_FAKE)
        const phone = value('PHONE', 'PII', 'pii:phone', SHOT_PHONE, SHOT_PHONE_FAKE)
        conv.user([SHOT_PROMPT])
        conv.send(host, { answer: { tool: { name: 'Read', args: [`{"file_path":"${SHOT}"}`] } } })
        // The Read answers with the picture alone; the text Latet read in it is the request's last block, masked.
        const text = conv.picture(paymentOf(iban, mail, phone))
        const sent = conv.send(host, { answer: { text: [SHOT_WHY] }, images: [text] })
        run.ids.why = sent.id
        run.slots.why = blockAt(sent, text)
        return { lines: [tool(s, 'Read', SHOT, `Read image (${SHOT_KB} KB)`), say(s, 'model', SHOT_WHY)], shown: screenshotOf(run) }
      },
    },
    {
      kind: 'do',
      title: 'Ask Latet',
      wait: { kind: 'pin', hint: `Click ${SHOT_EMAIL} in the text read from the image.`, met: () => false },
      explain: () =>
        'Click a highlighted value in the text Latet read from the image. Latet says what it is, which rule found it, and what was sent instead.',
      look: screenshotOf,
      finish: (run) => ({ lines: [], shown: screenshotOf(run) }),
    },
  ],
  end: (run) => ({
    learned: [
      'With text recognition installed, Latet reads the text in a screenshot on your computer, and covers each value with its fake before the image leaves.',
      'The dashboard shows the image as it was sent, and the text read in it with each value marked.',
      "A PDF, or an image sent by its address, is not read: it leaves as it is, marked Not inspected, unless Images and PDFs in Rules is set to Only masked or Don't send, and then the request is not sent.",
    ],
    where: [
      { label: 'Images and PDFs, in Rules', to: '/rules#data' },
      { label: 'The request with the screenshot', to: `/exchanges/${run.ids.why}` },
    ],
    next: null, // the last chapter the sandbox has
  }),
}

const CHAPTERS: ChapterDef[] = [basics, terms, falsePositives, stopped, live, settings, data, llm, images]

// ---- The sandbox ----------------------------------------------------------------------------------------------

interface Session {
  def: ChapterDef
  run: Run
  next: number
  lines: TutorialLine[]
  results: TutorialStepResult[]
  /** The agent is working on this step. */
  running: { step: number; until: number } | null
  /** Why the last run of the step failed (the state's `error`), until the step is asked for again. */
  error: string | null
  /** The step the error is about. */
  errorStep: number | null
}

/** A call that does not fit where the chapter is, with the state to go on from (null: not the chapter in progress). */
const conflict = (message: string, state: TutorialState | null) => new ApiError(409, message, [], { error: message, state })

export class MockTutorial {
  private session: Session | null = null
  private runs = 0
  private done = new Set<string>()
  private decisions = new Map<string, Decision>()
  private decidedAt = new Map<string, number>()
  private failed = new Set<string>()
  private ended = false

  /**
   * `requested`: the chapter named on the address, which the menu offers once. `failOnce`: every step whose
   * request is not held fails its first run, as a daemon that could not record it would say (`error`). `noOcr`: the
   * sandbox finds none of your text recognition models, so a chapter that needs them cannot start.
   */
  constructor(
    private host: TutorialHost,
    private requested: string | null = null,
    private options: { failOnce?: boolean; noOcr?: boolean } = {},
  ) {
    host.queue.onDecided((decision) => {
      this.decisions.set(decision.id, decision)
      this.decidedAt.set(decision.id, host.now())
    })
  }

  /**
   * `GET /me`'s `tutorial`: the chapter in progress, until its last step ran; and whether the local reviewer is on (the
   * sandbox's settings say so): the Tutorial Reviewer, in a chapter that has it, until another chapter starts.
   */
  me(): TutorialMe {
    const s = this.inProgress()
    return {
      current: s ? { chapter: s.def.id, number: this.number(s.def), title: s.def.title, step: s.next } : null,
      reviewer: this.host.config().llm_review.enabled,
    }
  }

  /** Answers a request under /tutorial; undefined when it is none of the tutorial's. */
  route(method: string, path: string, body: unknown): unknown {
    if (method === 'POST' && path === '/tutorial/end') {
      this.ended = true
      return { ending: true }
    }
    const match = path.match(/^\/tutorial\/chapters(?:\/([^/]+)(?:\/(start|next|state))?)?$/)
    if (!match) return undefined
    const [, id, action] = match
    if (id === undefined) return method === 'GET' ? this.menu() : undefined
    const def = CHAPTERS.find((chapter) => chapter.id === decodeURIComponent(id))
    if (!def) throw new ApiError(404, `there is no chapter ${decodeURIComponent(id)}`)
    if (action === undefined) return method === 'GET' ? this.chapter(def) : undefined
    if (action === 'start' && method === 'POST') return this.start(def)
    if (action === 'next' && method === 'POST') return this.step(def, body as TutorialNext)
    if (action === 'state' && method === 'GET') return { state: this.current(def) }
    return undefined
  }

  get hasEnded(): boolean {
    return this.ended
  }

  private number(def: ChapterDef): number {
    return CHAPTERS.indexOf(def) + 1
  }

  /** What the chapter needs that the sandbox lacks: your text recognition models, when the mock runs without them. */
  private missing(def: ChapterDef): 'ocr'[] {
    return this.options.noOcr ? (def.needs ?? []) : []
  }

  private card(def: ChapterDef): TutorialCard {
    return {
      id: def.id,
      number: this.number(def),
      title: def.title,
      minutes: def.minutes,
      learn: def.learn,
      phase: def.phase,
      done: this.done.has(def.id),
      missing: this.missing(def),
    }
  }

  /**
   * The chapter in progress: the sandbox's, until its last step ran (then it is done). A step whose time is up is
   * over by now, asked about or not: the daemon works in the background.
   */
  private inProgress(): Session | null {
    const s = this.session
    if (!s) return null
    this.settle(s)
    return s.next < s.def.steps.length ? s : null
  }

  private menu(): TutorialMenu {
    const requested = this.requested
    this.requested = null // given once
    const s = this.inProgress()
    return {
      chapters: CHAPTERS.map((def) => this.card(def)),
      requested,
      current: s ? { chapter: s.def.id, step: s.next } : null,
    }
  }

  private chapter(def: ChapterDef): TutorialChapter {
    return {
      ...this.card(def),
      agent: AGENT,
      steps: def.steps.map((step, index) => ({
        index,
        kind: step.kind,
        title: step.title,
        prompt: step.prompt ?? null,
        button: step.button ?? null,
        wait: step.wait ? { kind: step.wait.kind, hint: step.wait.hint, ...(step.wait.text === undefined ? {} : { text: step.wait.text }) } : null,
        resumed: resumedOf(step, index),
      })),
    }
  }

  private start(def: ChapterDef) {
    // A chapter that needs what the sandbox lacks does not start, and nothing is reset: the state is that of the chapter in progress.
    const missing = this.missing(def)
    if (missing.length > 0) throw conflict(`needs ${missing.join(', ')}`, this.session ? this.stateOf(this.session) : null)
    if (this.session) this.settle(this.session) // a step whose time is up is over, asked about or not
    const held = this.session?.running ? this.session.def.steps[this.session.running.step]?.holds : false
    if (this.session?.running && !held) throw conflict('busy', this.stateOf(this.session))
    this.host.queue.reset()
    this.host.history.clear()
    vault.entities.clear()
    this.decisions.clear()
    this.decidedAt.clear()
    this.host.resetConfig(def.settings ?? {})
    const no = ++this.runs
    const run: Run = {
      no,
      conv: new Conv(`x-session-id:tutorial-${def.id}-${no}`),
      ids: {},
      slots: {},
      blocks: {},
      batch: null,
      changeId: null,
      ending: null,
      decided: (id) => this.decisions.get(id),
    }
    this.session = { def, run, next: 0, lines: [], results: [], running: null, error: null, errorStep: null }
    return { chapter: this.chapter(def), state: this.stateOf(this.session) }
  }

  /** The state of a chapter that is the one in progress; 409 otherwise. */
  private current(def: ChapterDef): TutorialState {
    if (this.session?.def.id !== def.id) throw conflict('not started', null)
    return this.stateOf(this.session)
  }

  private step(def: ChapterDef, body: TutorialNext) {
    const s = this.session
    if (!s || s.def.id !== def.id) throw conflict('not started', null)
    this.settle(s)
    if (s.running) throw conflict('busy', this.stateOf(s))
    const index = s.next
    if (body.step !== index || index >= def.steps.length) throw conflict('not this step', this.stateOf(s))
    const step = def.steps[index]!
    // A wait the user must meet first. A held request (the Live Reviewer) starts first and waits inside the step.
    if (step.wait && !step.holds) {
      const met = step.wait.kind === 'pin' ? body.seen?.includes('pin') === true : step.wait.met(s.run, this.host)
      if (!met) throw conflict('waiting', this.stateOf(s))
    }
    s.error = null
    // What the pane shows as the step starts; a step tried again after a failed run has it there already.
    if (!s.lines.some((line) => line.step === index)) {
      if (step.clear) s.lines.push({ step: index, type: 'system', text: CLEARED, note: 'cleared' })
      s.lines.push(...resumedOf(step, index))
      if (step.prompt) s.lines.push({ step: index, type: 'prompt', text: step.prompt })
      if (step.says) s.lines.push({ step: index, type: 'agent', text: step.says })
    }
    step.start?.(s.run, this.host)
    s.running = { step: index, until: this.host.now() + BUSY_MS }
    return { state: this.stateOf(s) }
  }

  /** The agent finishes its step, once its time is up and, for a held request, the user has decided. */
  private settle(s: Session): void {
    const running = s.running
    if (!running) return
    const index = running.step
    const step = s.def.steps[index]!
    if (step.holds) {
      if (!step.wait!.met(s.run, this.host)) return // however long it waits, the request does not go
      // Decided: it goes on, and the agent takes a moment from then.
      running.until = Math.max(running.until, (this.decidedAt.get(s.run.batch?.id ?? '') ?? this.host.now()) + BUSY_MS)
    }
    if (this.host.now() < running.until) return
    const attempt = `${s.run.no}:${index}`
    if (this.options.failOnce && !step.holds && !this.failed.has(attempt)) {
      this.failed.add(attempt)
      s.running = null
      s.error = STEP_FAILED
      s.errorStep = index
      return
    }
    const done = step.finish(s.run, this.host, index)
    for (const line of s.lines) if (line.type === 'tool' && line.pending) line.pending = false // the next step ran it
    s.lines.push(...done.lines)
    s.results[index] = { index, ran: true, explain: step.explain(s.run), shown: done.shown }
    s.next = index + 1
    s.running = null
    if (s.next >= s.def.steps.length) this.done.add(s.def.id)
  }

  private waiting(s: Session): TutorialWaiting | null {
    const step = s.def.steps[s.next]
    if (!step?.wait) return null
    return { step: s.next, kind: step.wait.kind, satisfied: step.wait.kind === 'pin' ? false : step.wait.met(s.run, this.host) }
  }

  private stateOf(s: Session): TutorialState {
    this.settle(s)
    const finished = s.next >= s.def.steps.length
    return structuredClone({
      chapter: s.def.id,
      run: `run-${s.run.no}`,
      next: s.next,
      busy: s.running !== null,
      finished,
      lines: s.lines,
      // A step not run yet has nothing to say, except a `do` step, which says what it asks for.
      steps: s.def.steps.map(
        (step, index): TutorialStepResult =>
          s.results[index] ?? {
            index,
            ran: false,
            explain: step.kind === 'do' ? step.explain(s.run) : '',
            shown: step.kind === 'do' ? (step.look?.(s.run) ?? null) : null,
          },
      ),
      waiting: this.waiting(s),
      end: finished ? s.def.end(s.run) : null,
      error: s.error,
      error_step: s.error === null ? null : s.errorStep,
    })
  }
}
