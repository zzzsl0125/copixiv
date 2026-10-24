<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { Loader2 } from '@lucide/vue'
import { useIntersectionObserver } from '@vueuse/core'

// 注意 `withDefaults` 不是可选的：Vue 会把**缺失的 Boolean prop 强制转成 false**
// （boolean casting），所以 `autoLoad?: boolean` 不给默认值就等于「默认关闭自动
// 加载」——那会让整个无限滚动彻底失效（实测：点了书架只加载第一页就停住）。
const props = withDefaults(defineProps<{
  loading: boolean
  noMoreData: boolean
  hasData: boolean
  /**
   * 哨兵是否允许自动加载（默认允许）。
   *
   * 关掉它只有一个场景：**列表已到内存上限**。此时再往前翻只会"裁掉头部 +
   * 追加尾部"，页面总高度基本不变，于是哨兵永远停在视口内 → 每 300ms 再触发
   * 一次 → 无限翻页。实测（真实 Firefox）25 秒发了 77 个请求，页面一直在转圈。
   *
   * 关掉自动加载后，按钮仍然可点（用户想继续翻就手动点），只是不再自己跑。
   */
  autoLoad?: boolean
}>(), {
  autoLoad: true,
})

const emit = defineEmits<{ (e: 'load-more'): void }>()

const containerRef = ref<HTMLElement | null>(null)

const { pause, resume, stop } = useIntersectionObserver(
  containerRef,
  (entries) => {
    const target = entries[0]
    if (!target || !target.isIntersecting) return
    if (props.autoLoad === false) return   // 已到上限：不自动翻，等用户手动点
    if (!props.loading && !props.noMoreData && props.hasData) {
      emit('load-more')
    }
  },
  { rootMargin: '100px', threshold: 0.1 },
)

// 旧实现用 unobserve + observe 让仍停在视口里的哨兵重新触发一次；pause/resume
// 是同一件事（resume 会重建 observer，观察时必发一次初始 entry）。
let resumeTimer: ReturnType<typeof setTimeout> | undefined

const checkAndReobserve = () => {
  // 到了上限就别再排「重观察」的定时器——那个定时器存在的意义就是"让仍然在
  // 视口里的哨兵再触发一次"，而上限状态下它必然仍在视口里，于是会无限续期。
  if (props.autoLoad === false) return
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
