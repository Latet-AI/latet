import type { PiiCatalogEntry } from '../api/types'

/** The types in use when `pii` is not set: the general ones and the ID numbers of the countries. */
export function defaultPii(catalog: readonly PiiCatalogEntry[], base: readonly string[], regions: readonly string[], everyRegion: boolean): string[] {
  const chosen = new Set(regions)
  return catalog
    .filter((entry) => (entry.country === null ? base.includes(entry.type) : everyRegion || chosen.has(entry.country)))
    .map((entry) => entry.type)
}

export interface PiiGroup {
  /** null: the types of no particular country. */
  country: string | null
  types: string[]
}

/** The catalog by country, the general types first, the countries in the order given. */
export function groupByCountry(catalog: readonly PiiCatalogEntry[], countries: readonly string[]): PiiGroup[] {
  const general: PiiGroup = { country: null, types: catalog.filter((e) => e.country === null).map((e) => e.type) }
  const byCountry = countries
    .map((country) => ({ country, types: catalog.filter((e) => e.country === country).map((e) => e.type) }))
    .filter((group) => group.types.length > 0)
  return [general, ...byCountry]
}
