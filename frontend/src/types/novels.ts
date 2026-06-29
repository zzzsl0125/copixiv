/** Novel domain types */

export interface Novel {
  id: number
  title: string
  author_id?: number
  author_name?: string
  series_id?: number
  series_name?: string
  series_index?: number
  like: number
  view: number
  text: number
  caption?: string
  create_time?: string
  has_epub: number
  tags: string[]
  is_favourite: number
  is_special_follow: number
}

export interface NovelFilters {
  keyword: string
  order_by: string
  order_direction: string
  min_like?: number
  min_text?: number
}

export interface GetNovelsParams {
  queries?: Record<string, unknown>
  order_by?: string
  order_direction?: string
  cursor?: Record<string, unknown>
  per_page?: number
  min_like?: number
  min_text?: number
}
