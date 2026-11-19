<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { Loader2 } from '@lucide/vue'
import { useIntersectionObserver } from '@vueuse/core'

const ROOT_MARGIN_PX = 100

const props = defineProps<{
  loading: boolean
  noMoreData: boolean
  hasData: boolean
}>()

const emit = defineEmits<{ (e: 'load-more'): void }>()

const containerRef = ref<HTMLElement | null>(null)

// Prevents back-to-back auto-pagination from a single viewport visit before the
// newly appended items have pushed the sentinel below the viewport (or if the
// browser scrollbar remains pinned at the bottom).
let autoTriggeredForCurrentVisit = false
let sawIntersectionWhileLoading = false
let postLoadTimer: ReturnType<typeof setTimeout> | undefined

const isSentinelInViewport = (): boolean => {
  const el = containerRef.value
  if (!el || typeof window === 'undefined') return false
  const rect = el.getBoundingClientRect()
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0
  if (viewportHeight <= 0) return false
  return rect.top <= viewportHeight + ROOT_MARGIN_PX && rect.bottom >= -ROOT_MARGIN_PX
}

const { stop } = useIntersectionObserver(
  containerRef,
  (entries) => {
    const target = entries[0]
    if (!target) return
    if (!target.isIntersecting) {
      autoTriggeredForCurrentVisit = false
      sawIntersectionWhileLoading = false
      return
    }
    if (props.loading) {
      sawIntersectionWhileLoading = true
      return
    }
    if (!props.noMoreData && props.hasData && !autoTriggeredForCurrentVisit) {
      autoTriggeredForCurrentVisit = true
      sawIntersectionWhileLoading = false
      emit('load-more')
    }
  },
  { rootMargin: `${ROOT_MARGIN_PX}px`, threshold: 0.1 },
)

watch(
  () => props.loading,
  (isLoading) => {
    if (isLoading) return
    if (postLoadTimer) clearTimeout(postLoadTimer)
    void nextTick(() => {
      postLoadTimer = setTimeout(() => {
        postLoadTimer = undefined
        if (props.loading || props.noMoreData || !props.hasData) return
        const inView = isSentinelInViewport()
        if (!inView) {
          // New items expanded totalHeight and pushed the sentinel below the fold;
          // re-arm for the next time the user scrolls down to the bottom.
          autoTriggeredForCurrentVisit = false
          sawIntersectionWhileLoading = false
          return
        }
        // Only auto-fire after an in-flight load if the sentinel entered the
        // viewport *during* that load and has not yet triggered its own fetch.
        if (sawIntersectionWhileLoading && !autoTriggeredForCurrentVisit) {
          autoTriggeredForCurrentVisit = true
          sawIntersectionWhileLoading = false
          emit('load-more')
        }
      }, 150)
    })
  },
)

onUnmounted(() => {
  if (postLoadTimer) {
    clearTimeout(postLoadTimer)
    postLoadTimer = undefined
  }
  stop()
})
</script>

<template>
  <div
    v-if="hasData"
    ref="containerRef"
    class="mt-8 flex justify-center py-6 [overflow-anchor:none]"
  >
    <button
      v-if="!noMoreData"
      :disabled="loading"
      class="px-6 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-full text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
      @click="emit('load-more')"
    >
      <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
      {{ loading ? '加载中...' : '加载更多' }}
    </button>
    <p v-else class="text-sm text-gray-500">已经到底啦 ~</p>
  </div>
</template>
