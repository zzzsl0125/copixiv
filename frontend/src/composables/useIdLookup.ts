import { computed, onUnmounted, ref, watch, type ComputedRef, type Ref } from 'vue'

/**
 * 「填 pixiv ID → 预览解析出的名字」这条交互的**唯一实现**。
 *
 * 作者与系列两个字段用的是同一套行为（docs/NOVEL_EDIT_DESIGN.md §5.5/§5.6）：
 * 输入防抖、过期响应丢弃、"与当前值相同就不查"、失败不拦保存。抽成组合式
 * 函数不是为了省几行，而是为了让两者**在构造上一致**——系列"也一样"这件事
 * 不该靠两段平行代码的自觉。
 *
 * 唯一的例外是两句"留空"文案（`emptyNote` / `unlinkNote`）：解除关联后的收尾
 * 两个字段**刻意不同**（2026-09-22）——作者保留显示名、系列连名字一起清，
 * 理由见后端 `update_metadata.py::_series_columns`。行为共享，承诺各自给。
 */

/**
 * 等多久才发查询：太短会在"1"、"12"、"123"每一步都打一次请求，太长显得迟钝。
 * 与输入框的即时 hint 配合，350ms 是"打字停顿时刚好"的手感。
 */
export const ID_LOOKUP_DEBOUNCE_MS = 350

/**
 * 输入框文本 → 值：空 = `null`（解除关联），非法 = `undefined`（拦保存）。
 *
 * 只收纯数字的**正整数**：pixiv 的作者 / 系列 ID 都是正数，`0`、`-1`、`1e3`、
 * `１２３`（全角）一律算非法——这条字段是身份，不是展示值。
 */
export function parseIdInput(raw: string): number | null | undefined {
  const text = raw.trim()
  if (!text) return null
  if (!/^\d+$/.test(text)) return undefined
  const parsed = Number(text)
  if (!Number.isSafeInteger(parsed) || parsed <= 0) return undefined
  return parsed
}

export interface UseIdLookupOptions<T> {
  /** 输入框绑定的值（`v-model`） */
  input: Ref<string>
  /** 这本书当前挂的 id——相同就不查：卡片手里已经有名字，打开弹窗零请求 */
  currentId: () => number | null
  /** 真正的查询（作者 / 系列各一个端点） */
  fetch: (id: number) => Promise<T>
  /** 允许查询吗（字段只读时不该发请求：pixiv 书的作者 / 系列都不可改） */
  enabled?: () => boolean
}

export interface IdLookup<T> {
  /** 输入解析结果：`null` = 空、`undefined` = 非法、正整数 = 要查的 id */
  idNumber: ComputedRef<number | null | undefined>
  result: Ref<T | null>
  loading: Ref<boolean>
  failed: Ref<boolean>
  /** 重新评估（`reset()` 之后调用：值没变时 watch 不会触发） */
  schedule: () => void
}

export function useIdLookup<T>(options: UseIdLookupOptions<T>): IdLookup<T> {
  const result = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const failed = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  // 过期响应的丢弃依据：输入每变一次就 +1，慢回来的旧响应直接扔掉。
  let seq = 0

  const idNumber = computed(() => parseIdInput(options.input.value))

  function schedule() {
    clearTimeout(timer)
    seq += 1
    failed.value = false

    if (options.enabled && !options.enabled()) {
      result.value = null
      loading.value = false
      return
    }
    const wanted = idNumber.value
    if (wanted == null || wanted === (options.currentId() ?? null)) {
      result.value = null
      loading.value = false
      return
    }
    loading.value = true
    timer = setTimeout(() => void run(wanted), ID_LOOKUP_DEBOUNCE_MS)
  }

  async function run(wanted: number) {
    const mine = ++seq
    try {
      const fetched = await options.fetch(wanted)
      if (mine !== seq) return          // 用户又改了输入，这份结果作废
      result.value = fetched
    } catch {
      if (mine !== seq) return
      failed.value = true
      result.value = null
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  watch(options.input, schedule)
  onUnmounted(() => clearTimeout(timer))

  return { idNumber, result, loading, failed, schedule }
}

export interface IdLookupHint {
  tone: 'muted' | 'ok' | 'warn' | 'error'
  text: string
}

export interface IdHintContext {
  /** '作者' / '系列'——用于拼所有文案 */
  entity: string
  /** 输入的解析结果（`IdLookup.idNumber`） */
  idNumber: number | null | undefined
  /** 当前挂的 id 与它的显示名（卡片手里的值） */
  currentId: number | null
  currentName: string
  loading: boolean
  failed: boolean
  /** 预览结果（`null` = 还没查过） */
  found: { name: string | null | undefined; novelCount: number } | null
  /** 留空、且当前**没有** id 时那句话（作者与系列的文案不同） */
  emptyNote: string
  /**
   * 留空、且当前**有** id（= 解除关联）时那句话——同样由调用方给，因为两个
   * 字段的收尾**刻意不同**（2026-09-22）：作者保留显示名（名字在上传时来自
   * 文件名解析，有独立来源），系列连名字与章节号一起清（本地书的系列名只可能
   * 由 id 解析而来，留着就是用户清不掉的孤儿显示名；
   * 后端 `_series_columns`）。共用一件组合式函数不等于共用同一句承诺。
   */
  unlinkNote: string
}

/**
 * 把一次预览压成一行 hint。语气分四档：灰＝不需要动作、绿＝确认无误、
 * 黄＝能存但名字待解析、红＝拦住保存。
 *
 * 黄档**刻意不禁用保存按钮**：id 才是身份，名字只是缓存，pixiv 抖一下不该
 * 让人存不了档——这是作者与系列共有的产品判断。
 *
 * 两句"留空"的文案都由调用方给（`emptyNote` / `unlinkNote`）：两个字段共用
 * 这套交互，但解除关联的**收尾不同**（作者保留显示名、系列连名字一起清），
 * 所以这里不替它们承诺任何一句。
 */
export function formatIdLookupHint(ctx: IdHintContext): IdLookupHint {
  if (ctx.idNumber === undefined) {
    return { tone: 'error', text: `只能填数字 ID（pixiv ${ctx.entity} ID）` }
  }
  if (ctx.currentId != null && ctx.idNumber === null) {
    return { tone: 'muted', text: ctx.unlinkNote }
  }
  if (ctx.idNumber === null) return { tone: 'muted', text: ctx.emptyNote }
  if (ctx.idNumber === ctx.currentId) {
    return { tone: 'ok', text: `当前${ctx.entity}：${ctx.currentName}` }
  }
  if (ctx.loading) return { tone: 'muted', text: '查询中…' }
  if (ctx.failed) {
    return { tone: 'warn', text: '查询失败；仍可保存，名字之后自动补齐' }
  }
  if (!ctx.found) return { tone: 'muted', text: `将按此 ID 关联 pixiv ${ctx.entity}` }
  if (ctx.found.name) {
    const extra = ctx.found.novelCount > 0 ? ` · 库内已有 ${ctx.found.novelCount} 本` : ''
    return { tone: 'ok', text: `✓ ${ctx.found.name}${extra}` }
  }
  return {
    tone: 'warn',
    text: `未查到该${ctx.entity}（ID 不存在或 pixiv 暂不可达）；仍可保存，名字之后自动补齐`,
  }
}
