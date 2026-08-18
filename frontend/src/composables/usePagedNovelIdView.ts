import { ref, computed } from 'vue'
import { novelApi } from '../api'
import type { Novel } from '../types'

// 与浏览列表的 per_page 一致（useNovels 中为 30）：体验统一、DOM 累积
// 最慢。太小只会在极端滚动场景下多几次请求，本地单用户服务无感。
const PAGE_SIZE = 30

/**
 * 按 id 列表分页展示小说的「查看视图」——「查看已选」与「查看被排除」共用。
 *
 * id 列表由外部一次性提供（可能非常大），展示层按页惰性加载
 * （getNovelsByIds，每页 PAGE_SIZE 条），与批量模式「查看已选」的
 * 无限滚动行为一致。
 *
 * PAGE_SIZE 取 30：旧值 5000 会一次性渲染五千张卡片（封面图/标签/
 * 按钮的 DOM 爆炸）导致浏览器卡死；30/页与普通浏览列表的累积量级
 * 完全一致，配合进入视图时回顶，渲染始终平滑。
 */
export function usePagedNovelIdView() {
  const ids = ref<number[]>([])
  const novels = ref<Novel[]>([])
  const offset = ref(0)
  const loading = ref(false)
  const loadingMore = ref(false)

  const totalIds = computed(() => ids.value.length)
  const hasMore = computed(() => offset.value < ids.value.length)

  function reset() {
    ids.value = []
    novels.value = []
    offset.value = 0
    loading.value = false
    loadingMore.value = false
  }

  async function fetchPage() {
    const pageIds = ids.value.slice(offset.value, offset.value + PAGE_SIZE)
    if (pageIds.length === 0) return
    const result = await novelApi.getNovelsByIds(pageIds)
    offset.value += pageIds.length
    novels.value.push(...result.novels)
  }

  /** 从第一页开始展示 *idList*（重置旧状态；loading 覆盖首屏加载）。 */
  async function start(idList: number[]) {
    reset()
    ids.value = [...idList]
    if (ids.value.length === 0) return
    loading.value = true
    try {
      await fetchPage()
    } finally {
      loading.value = false
    }
  }

  /** 加载下一页（惰性，无更多时 no-op）。 */
  async function loadMore() {
    if (!hasMore.value || loadingMore.value) return
    loadingMore.value = true
    try {
      await fetchPage()
    } finally {
      loadingMore.value = false
    }
  }

  /**
   * 就地从分页视图里摘掉一个 id —— 供「查看已选」视图在取消勾选时让
   * 卡片立即从列表消失（所见即所得），而不是留下一张无蓝框的卡片、
   * 直到退出视图才生效。
   *
   * 保持分页游标自洽：已分页进 ``novels`` 的 id（index < offset）从两
   * 个数组里同时摘除并把游标回退一格；尚未分页的 id（index >= offset）
   * 仅离开 ``ids``，下一次 ``slice(offset, …)`` 取页不受影响。
   *
   * 返回该 id 是否原本存在并已被移除。
   */
  function removeId(id: number): boolean {
    const idx = ids.value.indexOf(id)
    if (idx === -1) return false
    ids.value.splice(idx, 1)
    if (idx < offset.value) {
      offset.value -= 1
      novels.value = novels.value.filter(n => n.id !== id)
    }
    return true
  }

  return {
    ids,
    novels,
    loading,
    loadingMore,
    totalIds,
    hasMore,
    reset,
    start,
    loadMore,
    removeId,
  }
}
