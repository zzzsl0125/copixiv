/** Task domain types — direct aliases of the OpenAPI contract. */
import type { components } from './api.generated'

type S = components['schemas']

export type TaskArgumentField = S['TaskArgumentField']
export type TaskMethod = S['TaskMethod']
export type ScheduledTask = S['ScheduledTaskResponse']
export type ScheduledTaskCreate = S['ScheduledTaskCreate']
export type ScheduledTaskUpdate = S['ScheduledTaskUpdate']
export type TaskHistory = S['TaskHistoryResponse']
