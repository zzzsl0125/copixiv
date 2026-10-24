import { reactive, ref, onMounted, onUnmounted } from 'vue'
import { novelApi } from '../api'
import type { Novel, NovelFilters } from '../types'
import { DEFAULT_PAGE_SIZE, DEFAULT_MAX_ITEMS, SECTIONS, resolveSection } from '../config'
import { useCursorPagination } from './useCursorPagination'
import { useSystem } from './useSystem'

export function useNovels() {
  const urlParams = new URLSearchParams(window.location.search)

  const filters = reactive<NovelFilters>({
    keyword: urlParams.get('keyword') || '',
    order_by: urlParams.get('order_by') || 'random',
    order_direction: urlParams.get('order_direction') || 'DESC',
    min_like: urlParams.get('min_like') ? Number(urlParams.get('min_like')) : undefined,
    min_text: urlParams.get('min_text') ? Number(urlParams.get('min_text')) : undefined,
  })

  // ---- 厌恶标签排除（跟随全局设置；被排除集合在 Novels.vue 中按需加载）----
  const { systemConfig } = useSystem()

  /** 本次请求是否排除厌恶标签小说（跟随全局设置，缺省视为开启）。 */
  const excludeBlocked = () =>
    systemConfig.value ? systemConfig.value.exclude_blocked_tag_novels : true

  /** 当前搜索范围内是否存在被厌恶标签排除的小说（首屏响应附带）——
   * 控制 ExclusionBar（查看被隐藏的小说）的显示；load-more 不覆盖。 */
  const hasExcluded = ref(false)

  /**
   * 最近一次**真正发起**取数时用的 keyword。
   *
   * 用来回答「这个 keyword 是不是已经取过数了」。`handleSearch` 是
   * 「同步赋 keyword → 立即取数」，而 App 的 keyword watcher 要到 flush 才跑；
   * 没有这个记号，watcher 会把同一份 keyword 再取一遍（实测发两次完全相同的
   * 请求）。侧边栏点击只赋 keyword、不自己取数，所以那个记号对不上，
   * watcher 该取还是会取——这正好是两条路径要的区分。
   */
  const lastFetchedKeyword = ref<string | null>(null)

  const fetchNovels = async (cursor?: unknown) => {
    const res = await novelApi.getNovels({
      keyword: filters.keyword.trim() || undefined,
      order_by: filters.order_by,
      order_direction: filters.order_direction,
      min_like: filters.min_like,
      min_text: filters.min_text,
      cursor: (cursor as Record<string, unknown>) || undefined,
      per_page: DEFAULT_PAGE_SIZE,
      exclude_blocked: excludeBlocked(),
    })

    // 仅首屏（无 cursor）更新 hasExcluded；load-more 沿用首屏值。
    if (cursor === undefined) {
      hasExcluded.value = res.has_excluded ?? false
    }

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
    capReached,
    loadData: loadNovelsBase,
    reset,
    handleLoadMore,
  } = useCursorPagination<Novel>(fetchNovels, { maxItems: DEFAULT_MAX_ITEMS })

  const loadNovels = (isLoadMore = false) => loadNovelsBase(isLoadMore)

  const applySearchOrdering = (keyword: string) => {
    if (!keyword.trim()) return

    // 命中某个书架 section 的 keyword（`is_favourite:true;` / `is_special_follow:true;`
    // / `source:local;`）→ 排序**必须**取该 section 的默认排序，和点侧边栏完全等价。
    //
    // 早先这里是一串手写的 `type === '...'` 分支，`source:local;` 漏掉了，于是
    // 在搜索框里敲它得到 `order_by=like`（书架是 `id DESC`）——实测真实浏览器：
    // 侧边栏 → 我的上传发 `order_by=id`，搜索框敲同一串发 `order_by=like`。
    // 两条入口对同一个书架给出不同列表，这是"表在别处被绕过"的老问题。
    const section = resolveSection(keyword)
    if (section) {
      const def = SECTIONS[section]
      filters.order_by = def.order_by
      filters.order_direction = def.order_direction
      filters.min_like = def.min_like
      filters.min_text = def.min_text
      return
    }

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

    // 记账：这次 keyword 已经取过数了（watcher 看到同一个值就不许再取）。
    lastFetchedKeyword.value = filters.keyword
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
    capReached,
    filters,
    hasExcluded,
    lastFetchedKeyword,
    loadNovels,
    handleSearch,
    handleLoadMore,
    handleCardSearch,
    resetFilters,
  }
}
