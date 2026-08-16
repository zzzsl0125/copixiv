import { reactive, onMounted, onUnmounted } from 'vue'
import { novelApi } from '../api'
import type { Novel, NovelFilters } from '../types'
import { useCursorPagination } from './useCursorPagination'

export function useNovels() {
  const urlParams = new URLSearchParams(window.location.search)

  const filters = reactive<NovelFilters>({
    keyword: urlParams.get('keyword') || '',
    order_by: urlParams.get('order_by') || 'random',
    order_direction: urlParams.get('order_direction') || 'DESC',
    min_like: urlParams.get('min_like') ? Number(urlParams.get('min_like')) : undefined,
    min_text: urlParams.get('min_text') ? Number(urlParams.get('min_text')) : undefined,
  })

  const fetchNovels = async (cursor?: unknown) => {
    const res = await novelApi.getNovels({
      keyword: filters.keyword.trim() || undefined,
      order_by: filters.order_by,
      order_direction: filters.order_direction,
      min_like: filters.min_like,
      min_text: filters.min_text,
      cursor: (cursor as Record<string, unknown>) || undefined,
      per_page: 30,
    })

    return {
      items: res.novels || [],
      cursor: res.cursor,
    }
  }

  const {
    items: novels,
    loading,
    error,
    cursor,
    noMoreData,
    loadData: loadNovelsBase,
    reset,
    handleLoadMore,
  } = useCursorPagination<Novel>(fetchNovels)

  const loadNovels = (isLoadMore = false) => loadNovelsBase(isLoadMore)

  const applySearchOrdering = (keyword: string) => {
    if (!keyword.trim()) return

    const conditions = keyword.split(/[;；]/).filter(cond => cond.trim())
    let isSpecialCase = false
    if (conditions.length === 1 && conditions[0]) {
      const condition = conditions[0]
      const colonIndex = condition.indexOf(':')
      if (colonIndex > 0) {
        const type = condition.substring(0, colonIndex).trim()
        if (type === 'id') {
          filters.order_by = 'id'
          filters.order_direction = 'DESC'
          isSpecialCase = true
        } else if (type === 'author_id' || type === 'author') {
          filters.order_by = 'id'
          filters.order_direction = 'DESC'
          isSpecialCase = true
        } else if (type === 'series_id' || type === 'series') {
          filters.order_by = 'id'
          filters.order_direction = 'ASC'
          isSpecialCase = true
        } else if (type === 'is_special_follow') {
          filters.order_by = 'id'
          filters.order_direction = 'DESC'
          isSpecialCase = true
        } else if (type === 'is_favourite') {
          filters.order_by = 'id'
          filters.order_direction = 'DESC'
          isSpecialCase = true
        }
      } else if (/^\d{7,}$/.test(condition.trim())) {
        // Bare 7+ digit number — the backend parser treats it as a novel ID.
        filters.order_by = 'id'
        filters.order_direction = 'DESC'
        isSpecialCase = true
      }
    }

    if (!isSpecialCase) {
      filters.order_by = 'like'
      filters.order_direction = 'DESC'
    }

    filters.min_like = 0
    filters.min_text = 0
  }

  const resetFilters = () => {
    filters.keyword = ''
    filters.order_by = 'random'
    filters.order_direction = 'DESC'
    filters.min_like = undefined
    filters.min_text = undefined
  }

  const handleSearch = (keyword?: string, options?: { updateUrl?: boolean; setOrdering?: boolean }) => {
    const { updateUrl = true, setOrdering = false } = options || {}

    if (typeof keyword === 'string') {
      filters.keyword = keyword
    }

    if (filters.keyword.trim() && setOrdering) {
      applySearchOrdering(filters.keyword)
    }

    reset()

    if (updateUrl) {
      const url = new URL(window.location.href)
      if (filters.keyword) url.searchParams.set('keyword', filters.keyword)
      else url.searchParams.delete('keyword')

      if (filters.order_by !== 'random') url.searchParams.set('order_by', filters.order_by)
      else url.searchParams.delete('order_by')

      if (filters.order_direction !== 'DESC') url.searchParams.set('order_direction', filters.order_direction)
      else url.searchParams.delete('order_direction')

      if (filters.min_like !== undefined) url.searchParams.set('min_like', filters.min_like.toString())
      else url.searchParams.delete('min_like')

      if (filters.min_text !== undefined) url.searchParams.set('min_text', filters.min_text.toString())
      else url.searchParams.delete('min_text')

      // Skip identical URLs so re-submitting the same search does not stack
      // duplicate history entries (Back would appear to do nothing).
      if (url.href !== window.location.href) {
        if (document.visibilityState === 'hidden') {
          // While the tab is hidden, stay on the replace path: main.ts guards
          // replaceState against waking a minimized browser, and a raw
          // pushState here would bypass that guard.
          window.history.replaceState({}, '', url)
        } else {
          // One history entry per search state, so Back/Forward step through
          // previous queries instead of leaving the app.
          window.history.pushState({}, '', url)
        }
      }
    }

    loadNovels()
  }

  const onPopState = () => {
    const params = new URLSearchParams(window.location.search)
    filters.keyword = params.get('keyword') || ''
    filters.order_by = params.get('order_by') || 'random'
    filters.order_direction = params.get('order_direction') || 'DESC'
    filters.min_like = params.get('min_like') ? Number(params.get('min_like')) : undefined
    filters.min_text = params.get('min_text') ? Number(params.get('min_text')) : undefined

    // Always sync the filters above (Back/Forward may fire while hidden);
    // only the refetch is deferred until the tab is visible again.
    if (document.visibilityState === 'hidden') return

    handleSearch(undefined, { updateUrl: false })
  }

  onMounted(() => {
    window.addEventListener('popstate', onPopState)
  })

  onUnmounted(() => {
    window.removeEventListener('popstate', onPopState)
  })

  const handleCardSearch = (type: string, value: string | number) => {
    let field = type
    if (type === 'author') field = 'author_id'
    if (type === 'series') field = 'series_id'
    if (type === 'tag') field = 'tags'

    const formattedValue = typeof value === 'string' && value.toString().includes(' ') ? `"${value}"` : value
    filters.keyword = `${field}:${formattedValue};`
    handleSearch(undefined, { setOrdering: true })
  }

  return {
    novels,
    loading,
    error,
    cursor,
    noMoreData,
    filters,
    loadNovels,
    handleSearch,
    handleLoadMore,
    handleCardSearch,
    resetFilters,
  }
}
