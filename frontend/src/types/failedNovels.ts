/** Failed-novel ledger types — thin aliases over the generated schemas. */
import type { components } from './api.generated'

type S = components['schemas']

export type FailedNovel = S['FailedNovelItem']
export type FailedNovelListResponse = S['FailedNovelListResponse']
export type FailedNovelCountResponse = S['FailedNovelCountResponse']
