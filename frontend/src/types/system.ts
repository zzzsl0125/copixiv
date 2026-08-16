/** System & search history types */

export interface SystemConfig {
  default_min_like: number
  default_min_text: number
  batch_download_naming: string
  exclude_blocked_tag_novels: boolean
}

export interface SearchHistory {
  id: number
  type: string
  value: string
  display_value?: string | null
  timestamp: string
}
