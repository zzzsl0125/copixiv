/** System & search history types — thin aliases over the generated schemas. */
import type { components } from './api.generated'

type S = components['schemas']

export type SystemConfig = S['SystemConfigResponse']
export type SearchHistory = S['SearchHistoryResponse']
