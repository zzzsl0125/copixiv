/** Task domain types.
 *
 * API 实体来自 `api.generated.ts`；`JsonSchemaProperty` / `TaskArgumentsSchema`
 * 是任务参数 JSON Schema 的**结构类型**（不是某个 Pydantic 模型的镜像），
 * 因此保持手写。
 */
import type { components } from './api.generated'

type S = components['schemas']

/** One property of a task's Pydantic JSON Schema. */
export interface JsonSchemaProperty {
  type?: string | string[]
  title?: string
  default?: unknown
  items?: JsonSchemaProperty
  anyOf?: JsonSchemaProperty[]
  enum?: unknown[]
}

/** A task's arguments as `model_json_schema()` on the backend. */
export interface TaskArgumentsSchema {
  type?: string
  properties?: Record<string, JsonSchemaProperty>
  required?: string[]
}

/** 后端把 arguments 声明成 `dict[str, Any] | None`；这里收窄成结构化 JSON Schema。 */
export type TaskMethod = Omit<S['TaskMethod'], 'arguments'> & {
  arguments?: TaskArgumentsSchema | null
}

export type ScheduledTask = S['ScheduledTaskResponse']
export type ScheduledTaskCreate = S['ScheduledTaskCreate']
export type ScheduledTaskUpdate = S['ScheduledTaskUpdate']
export type TaskHistory = S['TaskHistoryResponse']
