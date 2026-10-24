import { nextTick } from 'vue'

/**
 * 列表「裁掉头部」后的滚动位置补偿。
 *
 * 为什么需要：`DEFAULT_MAX_ITEMS` 让 load-more 越过上限时从**头部**裁掉最老的
 * 卡片。卡片是绝对定位的，裁掉头部会让整份列表上移（实测 30 张约 2500px），
 * 用户正看着的那张会瞬间跳走。原生 scroll anchoring 对绝对定位的子元素
 * 不生效，所以必须自己做。
 *
 * 做法是**锚定首张可见卡片**而不是补差值：
 * 1. 变更前：找出视口顶部那张卡（id + 它相对视口顶的偏移）；
 * 2. DOM 更新 + 重排完成后：把同一张卡放回同一个偏移。
 * 因为卡片 id 稳定（`:key="novel.id"`），列重新打包也不影响这个锚点——
 * 比"算裁掉了多少像素"精确，且不依赖列高。
 */

export interface AnchorSnapshot {
  id: string
  /** 卡片顶边相对视口顶的偏移（px，可正可负）。 */
  offset: number
}

/** 卡片 id 的 DOM 契约：`data-anchor-id` 由网格渲染方写死（= novel.id）。 */
const ANCHOR_ATTR = 'data-anchor-id'

/**
 * 抓一份锚点快照。
 *
 * @param root 网格容器（`position: relative` 的那一层）
 * @returns 视口顶部第一张可见卡片；容器里没有卡片时返回 null
 */
export function captureScrollAnchor(root: ParentNode | null): AnchorSnapshot | null {
  if (!root) return null
  const cards = Array.from(root.querySelectorAll<HTMLElement>(`[${ANCHOR_ATTR}]`))
  if (cards.length === 0) return null

  // 找「顶边在视口顶部之下、且最靠上」的那张：它才是用户正在读的起点。
  // 完全滚过头的卡片（底边也已在顶部之上）不参与。
  let best: { id: string; offset: number; top: number } | null = null
  for (const el of cards) {
    const rect = el.getBoundingClientRect()
    if (rect.bottom <= 0) continue // 已在视口上方之外
    if (best === null || rect.top < best.top) {
      best = { id: el.getAttribute(ANCHOR_ATTR) ?? '', offset: rect.top, top: rect.top }
      if (rect.top <= 0) break // 已经跨过顶部，这张就是锚点（卡片有序，再往下不会更靠上）
    }
  }
  if (best === null) return null
  return { id: best.id, offset: best.offset }
}

/**
 * 把锚点卡片放回原来的视口偏移。
 *
 * @returns 是否真的做了补偿（锚点丢了 / 没滚动过就返回 false）
 */
export function restoreScrollAnchor(root: ParentNode | null, anchor: AnchorSnapshot | null): boolean {
  if (!root || !anchor) return false
  const el = root.querySelector<HTMLElement>(`[${ANCHOR_ATTR}="${CSS.escape(anchor.id)}"]`)
  if (!el) return false // 锚点自身被裁掉了：这是最坏情况，宁可不补偿也不要跳到别处
  const delta = el.getBoundingClientRect().top - anchor.offset
  if (Math.abs(delta) < 1) return false
  window.scrollBy(0, delta)
  return true
}

/**
 * 包一次「会让列表变短的操作」：抓锚点 → 等 DOM 与重排稳定 → 还原。
 *
 * `afterLayout` 由调用方提供（通常是瀑布流的 `relayout()`），它在 DOM 更新后
 * 重算位置；还原必须排在它**之后**，否则量到的是旧坐标。
 */
export async function withScrollAnchor(
  root: () => ParentNode | null,
  mutate: () => void,
  afterLayout: () => void,
): Promise<boolean> {
  const anchor = captureScrollAnchor(root())
  mutate()
  await nextTick()
  afterLayout()
  await nextTick()
  return restoreScrollAnchor(root(), anchor)
}
