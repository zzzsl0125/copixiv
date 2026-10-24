<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { Loader2 } from '@lucide/vue'
import { useIntersectionObserver } from '@vueuse/core'

// 三个 prop 都是必传的（没有可选 prop，因此不需要 withDefaults）。
//
// 历史教训：这里曾经有过一个 `autoLoad?: boolean`，用来「到上限后停掉自动加载」。
// 那个机制基于一次误诊（用 `window.scrollTo()` 循环模拟连续滚动，测到 25 秒 77 个
// 请求，误以为哨兵会无限自触发），而且它**从来没有真正生效过**：唯一的绑定点
// `:auto-load="!capReached"` 在回退时被删了，prop 于是恒为默认值 true，两个
// `=== false` 的守卫都是死代码。
//
// 真机复测（Firefox 158 + BiDi）推翻了那个假设：到上限后把锚点补偿临时废掉、
// 确保哨兵真的停在视口里，然后完全静止 25 秒 —— 只发了 3 个请求就自己停了。
// 原因是每次加载都会把哨兵推出视口（实测 secTop 564 → 714 → 949），而
// IntersectionObserver 只在「不可见 → 可见」的**变化**时回调，不会因为
// 「一直可见」而持续回调。所以自动加载是自限的，不需要任何节流。
const props = defineProps<{
  loading: boolean
  noMoreData: boolean
  hasData: boolean
}>()

const emit = defineEmits<{ (e: 'load-more'): void }>()

const containerRef = ref<HTMLElement | null>(null)

const { pause, resume, stop } = useIntersectionObserver(
  containerRef,
  (entries) => {
    const target = entries[0]
    if (!target || !target.isIntersecting) return
    if (!props.loading && !props.noMoreData && props.hasData) {
      emit('load-more')
    }
  },
  { rootMargin: '100px', threshold: 0.1 },
)

// 旧实现用 unobserve + observe 让仍停在视口里的哨兵重新触发一次；pause/resume
// 是同一件事（resume 会重建 observer，观察时必发一次初始 entry）。
//
// 这道「加载完再排一次重观察」不能删：用户滚到底时哨兵可能只露出一点，
// 加载完新的卡片把它推到屏幕外一点点又推回来，没有重观察就不会再触发。
let resumeTimer: ReturnType<typeof setTimeout> | undefined

const checkAndReobserve = () => {
  if (!props.loading && !props.noMoreData && props.hasData) {
    pause()
    if (resumeTimer) clearTimeout(resumeTimer)
    resumeTimer = setTimeout(() => {
      resumeTimer = undefined
      resume()
    }, 300)
  }
}

watch(() => props.loading, (newVal) => {
  if (!newVal) checkAndReobserve()
})

onUnmounted(() => {
  if (resumeTimer) { clearTimeout(resumeTimer); resumeTimer = undefined }
  stop()
})
</script>

<template>
  <div v-if="hasData" class="mt-8 flex justify-center py-6" ref="containerRef">
    <button
      v-if="!noMoreData"
      @click="$emit('load-more')"
      :disabled="loading"
      class="px-6 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-full text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
    >
      <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
      {{ loading ? '加载中...' : '加载更多' }}
    </button>
    <p v-else class="text-sm text-gray-500">已经到底啦 ~</p>
  </div>
</template>
