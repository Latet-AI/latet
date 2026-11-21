import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n, { LANGUAGES } from '../../i18n'

// The page's one read, whether the server serves single sign-on: its answer is what the case under test says.
const served: { login: { login: string } | null | undefined } = { login: undefined }
vi.mock('../../lib/useAsync', () => ({
  useAsync: () => ({ data: served.login ?? undefined, error: undefined, loading: false, stale: false, reload: () => undefined, mutate: () => undefined }),
}))

const { CompanySignIn } = await import('./CompanySignIn')
const { CompanySignInWay } = await import('./CompanyWay')

const LOGIN = '/_latet/sso/login'

/** The sign-in page's block as it renders when the dashboard was opened at `search` and the server answered `login`. */
function render(search: string, login: { login: string } | null): { html: string; text: string } {
  served.login = login
  vi.stubGlobal('window', { location: { search } })
  const html = renderToStaticMarkup(h(CompanySignIn))
  return { html, text: html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() }
}

afterEach(() => vi.unstubAllGlobals())

describe('the company sign-in on the page that asks for a code', () => {
  it("is a button to the server's own single sign-on, with the terminal's way under it", () => {
    const { html, text } = render('', { login: LOGIN })
    expect(html).toContain(`href="${LOGIN}"`)
    expect(text).toContain('Sign in with your company account')
    expect(text).toContain('Or open the dashboard from your terminal:')
    expect(html).not.toContain('role="alert"') // nothing refused it
  })

  it('says why the last sign-in did not happen above the button, so that the person can try again', () => {
    const { html, text } = render('?sign-in=expired', { login: LOGIN })
    expect(html).toContain('role="alert"')
    expect(text).toContain('That sign-in took more than ten minutes, or was used already.')
    expect(html).toContain(`href="${LOGIN}"`)
    expect(html.indexOf('role="alert"')).toBeLessThan(html.indexOf(`href="${LOGIN}"`))
  })

  it('says a reason it does not know as a sign-in that could not be finished', () => {
    expect(render('?sign-in=whatever', { login: LOGIN }).text).toContain('The sign-in could not be finished.')
  })

  it('says the reason alone, with no button, where the server does not serve single sign-on (a trial that ended)', () => {
    const { html, text } = render('?sign-in=trial-ended', null)
    expect(text).toContain('Single sign-on stopped when the Latet Enterprise trial ended.')
    expect(html).not.toContain(LOGIN)
    expect(text).not.toContain('Or open the dashboard from your terminal')
  })

  it('is nothing at all where the server does not serve single sign-on and no sign-in was refused', () => {
    expect(render('', null).html).toBe('')
    expect(render('?login=expired', null).html).toBe('') // a query the dashboard did not send is no reason
  })

  it('says every reason in every language', async () => {
    for (const language of LANGUAGES) {
      await i18n.changeLanguage(language)
      for (const reason of ['expired', 'failed', 'not-allowed', 'not-provisioned', 'disabled', 'unavailable', 'trial-ended']) {
        expect(render(`?sign-in=${reason}`, null).text.length, `${language}: ${reason}`).toBeGreaterThan(20)
      }
      expect(render('', { login: LOGIN }).text, language).not.toContain('auth.company') // the button is worded, not a key
    }
    await i18n.changeLanguage('en')
  })
})

describe('the banner that leads to the company sign-in after a refused change', () => {
  const way = (props: { quiet?: boolean; text?: string }) => {
    served.login = { login: LOGIN }
    return renderToStaticMarkup(h(CompanySignInWay, props))
  }

  it('is an alert that can be focused, once the change was refused', () => {
    const html = way({})
    expect(html).toContain('role="alert"')
    expect(html).toContain('tabindex="-1"') // it is brought to the person and focused when it appears
    expect(html).toContain('This change applies to everyone on this server')
    expect(html).toContain(`href="${LOGIN}"`)
  })

  it('is a status, not focused, on a page that says it from the start', () => {
    const html = way({ quiet: true, text: 'The policy is shown in a session opened with your company account.' })
    expect(html).toContain('role="status"')
    expect(html).not.toContain('tabindex')
    expect(html).toContain('The policy is shown in a session')
  })

  it('has no button where the server does not serve single sign-on', () => {
    served.login = null
    const html = renderToStaticMarkup(h(CompanySignInWay, {}))
    expect(html).toContain('role="alert"')
    expect(html).not.toContain('href=')
  })
})
