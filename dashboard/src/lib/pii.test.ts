import { describe, expect, it } from 'vitest'
import { defaultPii, groupByCountry } from './pii'

const catalog = [
  { type: 'EMAIL', country: null },
  { type: 'IT_FISCAL_CODE', country: 'IT' },
  { type: 'VAT_NUMBER', country: null },
  { type: 'DE_TAX_ID', country: 'DE' },
  { type: 'US_SSN', country: 'US' },
]

describe('personal-data types', () => {
  it('uses the general types and the chosen countries by default', () => {
    expect(defaultPii(catalog, ['EMAIL', 'VAT_NUMBER'], ['IT', 'US'], false)).toEqual(['EMAIL', 'IT_FISCAL_CODE', 'VAT_NUMBER', 'US_SSN'])
    expect(defaultPii(catalog, ['EMAIL'], [], true)).toEqual(['EMAIL', 'IT_FISCAL_CODE', 'DE_TAX_ID', 'US_SSN'])
  })

  it('groups the catalog by country, general types first', () => {
    expect(groupByCountry(catalog, ['US', 'IT', 'FR'])).toEqual([
      { country: null, types: ['EMAIL', 'VAT_NUMBER'] },
      { country: 'US', types: ['US_SSN'] },
      { country: 'IT', types: ['IT_FISCAL_CODE'] },
    ])
  })
})
