// Whether a page comes from a Latet server, and that server's host as the sidebar shows it.

/** The host of the server's address, as the sidebar shows it. */
export function serverHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** Whether this page comes from a Latet server: a computer's dashboard is always on this computer. */
export function servedFromServer(hostname: string): boolean {
  return !['127.0.0.1', 'localhost', '[::1]', '::1'].includes(hostname.toLowerCase())
}

/**
 * Whether the page on screen comes from a Latet server, by its own address (`servedFromServer`): what words need that
 * have no `/me` answer to hand, such as what to do when nothing answers. A computer's daemon answers only to this
 * computer's own names. Where there is no page (the tests' renderer), a computer's.
 */
export function pageOnServer(): boolean {
  return typeof location !== 'undefined' && servedFromServer(location.hostname)
}
