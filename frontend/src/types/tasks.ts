/** Task domain types */

export interface TaskArgument {
  name: string
  type: string
  default?: unknown
  required: boolean
}

export interface TaskMethod {
  name: string
  description?: string
  arguments: TaskArgument[]
}

export interface ScheduledTask {
  id: number
  name: string
  task: string
  cron: string
  params?: Record<string, unknown>
  config?: Record<string, unknown>
  is_enabled: boolean
  sort_index?: number
}

export interface ScheduledTaskCreate {
  name: string
  task: string
  cron: string
  params?: Record<string, unknown>
  config?: Record<string, unknown>
  is_enabled: boolean
}

export interface ScheduledTaskUpdate {
  name?: string
  task?: string
  cron?: string
  params?: Record<string, unknown>
  config?: Record<string, unknown>
  is_enabled?: boolean
}

export interface TaskHistory {
  id: number
  name: string
  arguments?: Record<string, unknown> | null
  status: string
  start_time: string
  end_time?: string | null
  duration?: number | null
  result?: Record<string, unknown> | null
}
