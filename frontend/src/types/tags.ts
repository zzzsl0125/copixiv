/** Tag domain types — thin aliases over the generated OpenAPI schemas. */
import type { components } from './api.generated'

type S = components['schemas']

export type TagPreference = S['TagPreferenceResponse']
export type TagAlias = S['TagAliasResponse']
export type TagCandidate = S['TagCandidate']
export type TagAliasSuggest = S['TagAliasSuggestResponse']
export type TagAliasSuggestList = S['TagAliasSuggestListResponse']
