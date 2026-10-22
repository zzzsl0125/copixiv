/** Token domain types — thin aliases over the generated OpenAPI schemas. */
import type { components } from './api.generated'

type S = components['schemas']

export type Token = S['TokenResponse']
export type TokenCreate = S['TokenBase']
export type TokenUpdate = S['TokenUpdate']
