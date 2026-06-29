/** Tag domain types */

export interface TagPreference {
  id: number
  tag: string
  preference: 'favourite' | 'blocked'
  sort_index: number
}

export interface TagAlias {
  id: number
  source: string
  target: string
}

export interface TagCandidate {
  id: number
  name: string
  reference_count: number
}

export interface TagAliasSuggest {
  target: TagCandidate
  candidates: TagCandidate[]
}

export interface TagAliasSuggestList {
  items: TagAliasSuggest[]
  next_offset: number
}
