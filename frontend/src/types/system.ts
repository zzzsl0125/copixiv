/** System & search history types */

export interface SystemConfig {
  default_min_like: number
  default_min_text: number
  batch_download_naming: string
}

export interface SearchHistory {
  id: number
  type: string
  value: string
  display_value?: string
  timestamp: string
}
