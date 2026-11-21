import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '../../components/Toast'
import { errorText } from '../../components/ui'
import { refusalOf } from '../lib/changes'

/** A change of an Enterprise page: `done` in a toast when it is made, the server's reason when it is refused. */
export type Run = (action: () => Promise<unknown>, done?: string) => Promise<boolean>

/**
 * Runs the page's changes one at a time (`busy` meanwhile), then `reload`. The third value is true while the last
 * change was refused because this session was not opened with the company sign-in: the page shows `CompanySignInWay`
 * for it, which says why and leads to the company sign-in without a sign-out, so no toast repeats it.
 *
 * `onChanged`, for a page whose changes name the settings they were made from: a change refused because they changed
 * meanwhile (409 `sign-in-changed`) is not said in a toast; the page reads them again (`reload`) and `onChanged` has it
 * say that someone else changed them.
 */
export function useRun(reload: () => void, onChanged?: () => void): [boolean, Run, boolean] {
  const { t } = useTranslation()
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [company, setCompany] = useState(false)
  const run: Run = async (action, done) => {
    setBusy(true)
    setCompany(false)
    try {
      await action()
      if (done) toast.show(done, 'ok')
      reload()
      return true
    } catch (error) {
      const refusal = refusalOf(error, onChanged !== undefined)
      setCompany(refusal === 'company')
      if (refusal === 'changed') {
        reload()
        onChanged?.()
      } else if (refusal === 'toast') toast.show(errorText(t, error), 'fail')
      return false
    } finally {
      setBusy(false)
    }
  }
  return [busy, run, company]
}
