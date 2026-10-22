import { describe, expect, it } from 'vitest'
import { loginRefusal, signInOnServer } from './signin'

describe('why a sign-in link signed no one in', () => {
  it('reads the reasons the daemon and the server send the page back with', () => {
    expect(loginRefusal('?login=expired')).toBe('expired')
    expect(loginRefusal('?login=limited')).toBe('limited')
    expect(loginRefusal('?mock-401&login=busy')).toBe('busy')
  })

  it('reads nothing else: no reason, another word, or what a page of another site wrote', () => {
    expect(loginRefusal('')).toBeNull()
    expect(loginRefusal('?login=')).toBeNull()
    expect(loginRefusal('?login=Expired')).toBeNull()
    expect(loginRefusal('?login=%3Cb%3Ehello%3C%2Fb%3E')).toBeNull()
    expect(loginRefusal('?code=one-time')).toBeNull()
  })
})

describe('whether the sign-in page is a Latet server’s', () => {
  it('believes the health answer when it has one', () => {
    expect(signInOnServer({ server: true }, '127.0.0.1')).toBe(true)
    // A computer's daemon reached under a name of its own (a tunnel, a proxy): its health says it is no server.
    expect(signInOnServer({}, 'latet.acme.lan')).toBe(false)
    expect(signInOnServer({ tutorial: true }, 'latet.acme.lan')).toBe(false)
  })

  it('goes by the page’s host until the health answer comes: a computer’s dashboard is always on this computer', () => {
    expect(signInOnServer(undefined, 'latet.acme.lan')).toBe(true)
    expect(signInOnServer(undefined, '127.0.0.1')).toBe(false)
    expect(signInOnServer(undefined, 'localhost')).toBe(false)
  })
})
