export interface FailedNovel {
  novel_id: number
  title: string | null
  failure_type: string | null
  error_message: string | null
  failed_times: number
  last_failed_at: string | null
}

export interface FailedNovelListResponse {
  items: FailedNovel[]
  total: number
  offset: number
  limit: number
}

export interface FailedNovelCountResponse {
  count: number
}
