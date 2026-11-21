import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { Run } from './useRun'

// The toasts the page's changes raise, as a toast would have shown them.
const toasts: { message: string; tone?: string }[] = []
vi.mock('../../components/Toast', () => ({
  useToast: () => ({ show: (message: string, tone?: string) => toasts.push({ message, tone }) }),
}))

const { useRun } = await import('./useRun')

/** The `run` a page holds after it rendered (the state setters are no-ops on a server render: the toasts are what shows). */
function runOf(reload: () => void, onChanged?: () => void): Run {
  const taken: Run[] = []
  const Probe = ({ take }: { take: (run: Run) => void }) => {
    take(useRun(reload, onChanged)[1])
    return null
  }
  renderToStaticMarkup(h(Probe, { take: (run) => taken.push(run) }))
  const [run] = taken
  if (!run) throw new Error('the page did not render')
  return run
}

const COMPANY = 'sign in with your company account to change what applies to everyone'
const refusedAsCode = () => new ApiError(403, COMPANY, [], { error: COMPANY, code: 'company-sign-in' })

describe('the change runner of the Enterprise pages', () => {
  beforeEach(() => {
    toasts.length = 0
  })

  it('says a change made, and reads the page again', async () => {
    const reload = vi.fn()
    expect(await runOf(reload)(async () => undefined, 'Group added.')).toBe(true)
    expect(toasts).toEqual([{ message: 'Group added.', tone: 'ok' }])
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("says in a toast why a change was refused, with the server's words", async () => {
    const reload = vi.fn()
    const refused = new ApiError(409, 'this is the last administrator of this server', [], { error: 'this is the last administrator of this server' })
    expect(await runOf(reload)(async () => Promise.reject(refused))).toBe(false)
    expect(toasts).toHaveLength(1)
    expect(toasts[0]?.tone).toBe('fail')
    expect(reload).not.toHaveBeenCalled()
  })

  it("raises no toast for a change the company sign-in is needed for: the page's banner says it, and its button needs no sign-out", async () => {
    const reload = vi.fn()
    expect(await runOf(reload)(async () => Promise.reject(refusedAsCode()))).toBe(false)
    expect(toasts).toEqual([]) // no "Sign out, ..." beside a button that goes to the company sign-in without one
    expect(reload).not.toHaveBeenCalled()
  })

  const changedMeanwhile = () => {
    const error = 'not changed: the sign-in settings were changed since they were read here: read them again, then change them'
    return new ApiError(409, error, [], { error, code: 'sign-in-changed' })
  }

  it('reads the page again and has it say that someone else changed the settings, with no toast and no second change', async () => {
    const reload = vi.fn()
    const onChanged = vi.fn()
    expect(await runOf(reload, onChanged)(async () => Promise.reject(changedMeanwhile()), 'Sign-in settings saved')).toBe(false)
    expect(toasts).toEqual([]) // neither a failure in the server's words nor the "saved" it was not
    expect(reload).toHaveBeenCalledTimes(1)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it("says the server's reason in a toast for a page that is not ready to read again", async () => {
    const reload = vi.fn()
    expect(await runOf(reload)(async () => Promise.reject(changedMeanwhile()))).toBe(false)
    expect(toasts).toHaveLength(1)
    expect(toasts[0]?.tone).toBe('fail')
    expect(reload).not.toHaveBeenCalled()
  })

  it('keeps its toast for any other refusal, on a page ready to read again too', async () => {
    const onChanged = vi.fn()
    const reload = vi.fn()
    const refused = new ApiError(409, 'this is the last administrator of this server', [], { error: 'this is the last administrator of this server' })
    expect(await runOf(reload, onChanged)(async () => Promise.reject(refused))).toBe(false)
    expect(toasts).toHaveLength(1)
    expect(onChanged).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })
})
