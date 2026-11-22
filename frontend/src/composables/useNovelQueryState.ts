import { computed } from 'vue'
import { useRoute, useRouter, type LocationQuery, type LocationQueryRaw } from 'vue-router'
import { resolveSection, SECTIONS, type SectionId } from '../config'
import { isOrderBy, isOrderDirection, type NovelFilters } from '../types'
import { useNovelShellState } from './useNovelShellState'

function readQueryString(value: LocationQuery[string]): string | null {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    const first = value[0]
    return typeof first === 'string' ? first : null
  }
  return null
}

function parseNonNegInt(raw: string | null): number | undefined {
  if (raw === null) return undefined
  const trimmed = raw.trim()
  if (!trimmed || !/^\d+$/.test(trimmed)) return undefined
  const n = Number(trimmed)
  return Number.isSafeInteger(n) ? n : undefined
}

/** Resolve default ordering + thresholds when submitting a search string. */
export function computeSearchFilters(keyword: string): NovelFilters {
  const trimmed = keyword.trim()
  if (!trimmed) {
    return {
      keyword: '',
      order_by: SECTIONS.novels.order_by,
      order_direction: SECTIONS.novels.order_direction,
      min_like: SECTIONS.novels.min_like,
      min_text: SECTIONS.novels.min_text,
    }
  }

  const section = resolveSection(keyword)
  if (section) {
    const def = SECTIONS[section]
    return {
      keyword,
      order_by: def.order_by,
      order_direction: def.order_direction,
      min_like: def.min_like,
      min_text: def.min_text,
    }
  }

  const conditions = keyword.split(/[;；]/).filter((cond) => cond.trim())
  let orderBy: NovelFilters['order_by'] = 'like'
  let orderDirection: NovelFilters['order_direction'] = 'DESC'

  if (conditions.length === 1 && conditions[0]) {
    const condition = conditions[0]
    const colonIndex = condition.indexOf(':')
    if (colonIndex > 0) {
      const type = condition.substring(0, colonIndex).trim()
      if (type === 'id' || type === 'author_id') {
        orderBy = 'id'
        orderDirection = 'DESC'
      } else if (type === 'series_id') {
        orderBy = 'id'
        orderDirection = 'ASC'
      }
    } else if (/^\d{7,}$/.test(condition.trim())) {
      orderBy = 'id'
      orderDirection = 'DESC'
    }
  }

  return {
    keyword,
    order_by: orderBy,
    order_direction: orderDirection,
    min_like: 0,
    min_text: 0,
  }
}

/**
 * Canonicalize `NovelFilters` into a minimal `LocationQueryRaw` object.
 * Keys equal to `computeSearchFilters(filters.keyword)` defaults are omitted so `/`
 * and search/shelf links stay clean, while explicit overrides (e.g. `min_like=0`
 * under browse mode) are preserved in the URL.
 */
export function buildNovelQuery(filters: NovelFilters): LocationQueryRaw {
  const defaults = computeSearchFilters(filters.keyword)
  const query: LocationQueryRaw = {}

  if (filters.keyword) query.keyword = filters.keyword
  if (filters.order_by !== defaults.order_by) query.order_by = filters.order_by
  if (filters.order_direction !== defaults.order_direction) {
    query.order_direction = filters.order_direction
  }
  if (filters.min_like !== undefined && filters.min_like !== defaults.min_like) {
    query.min_like = String(filters.min_like)
  }
  if (filters.min_text !== undefined && filters.min_text !== defaults.min_text) {
    query.min_text = String(filters.min_text)
  }
  return query
}

/** Derive validated `NovelFilters` from a raw `vue-router` query map. */
export function parseNovelQuery(query: LocationQuery): NovelFilters {
  const keyword = readQueryString(query.keyword) ?? ''
  const defaults = computeSearchFilters(keyword)

  const rawOrderBy = readQueryString(query.order_by)
  const rawOrderDir = readQueryString(query.order_direction)
  const rawMinLike = parseNonNegInt(readQueryString(query.min_like))
  const rawMinText = parseNonNegInt(readQueryString(query.min_text))

  return {
    keyword,
    order_by: rawOrderBy !== null && isOrderBy(rawOrderBy) ? rawOrderBy : defaults.order_by,
    order_direction:
      rawOrderDir !== null && isOrderDirection(rawOrderDir)
        ? rawOrderDir
        : defaults.order_direction,
    min_like: rawMinLike ?? defaults.min_like,
    min_text: rawMinText ?? defaults.min_text,
  }
}

function queriesEqual(current: LocationQuery, next: LocationQueryRaw): boolean {
  const currentEntries = Object.entries(current).filter(
    ([, v]) => v !== undefined && v !== null && v !== '',
  )
  const nextEntries = Object.entries(next).filter(
    ([, v]) => v !== undefined && v !== null && v !== '',
  )
  if (currentEntries.length !== nextEntries.length) return false
  return nextEntries.every(([k, v]) => readQueryString(current[k]) === String(v))
}

export function useNovelQueryState() {
  const route = useRoute()
  const router = useRouter()
  const shell = useNovelShellState()

  const filters = computed<NovelFilters>(() => parseNovelQuery(route.query))
  const activeSection = computed<SectionId | null>(() => resolveSection(filters.value.keyword))

  async function commitFilters(next: NovelFilters) {
    const query = buildNovelQuery(next)
    if (route.path === '/' && queriesEqual(route.query, query)) {
      shell.bumpRefreshNonce()
      return
    }
    await router.push({ path: '/', query })
  }

  async function navigateToSection(sectionId: SectionId) {
    const def = SECTIONS[sectionId]
    await commitFilters({
      keyword: def.keyword,
      order_by: def.order_by,
      order_direction: def.order_direction,
      min_like: def.min_like,
      min_text: def.min_text,
    })
  }

  async function navigateSearch(keyword: string) {
    await commitFilters(computeSearchFilters(keyword))
  }

  async function updateFilters(patch: Partial<NovelFilters>) {
    await commitFilters({ ...filters.value, ...patch })
  }

  async function navigateCardSearch(type: string, value: string | number) {
    let field = type
    if (type === 'author') field = 'author_id'
    if (type === 'series') field = 'series_id'
    if (type === 'tag') field = 'tags'

    await navigateSearch(`${field}:${value};`)
  }

  return {
    filters,
    activeSection,
    navigateToSection,
    navigateSearch,
    updateFilters,
    navigateCardSearch,
  }
}
