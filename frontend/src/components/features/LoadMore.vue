<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { Loader2 } from '@lucide/vue'
import { useIntersectionObserver } from '@vueuse/core'

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
    if (target && target.isIntersecting && !props.loading && !props.noMoreData && props.hasData) {
      emit('load-more')
    }
  },
  { rootMargin: '100px', threshold: 0.1 },
)

// 旧实现用 unobserve + observe 让仍停在视口里的哨兵重新触发一次；pause/resume
// 是同一件事（resume 会重建 observer，观察时必发一次初始 entry）。
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
