<script setup lang="ts">
import { computed, ref } from 'vue'
import { useClipboard, useVirtualList, watchDebounced } from '@vueuse/core'
import { ArrowDown, ArrowUp, Check, Copy, Download, Maximize2, Minimize2, Search } from '@lucide/vue'

const props = defineProps<{
  log: string
  title?: string
  maxHeight?: string
}>()

const searchTerm = ref('')
const showSearch = ref(false)
const isFullscreen = ref(false)
const currentMatchIndex = ref(-1)

const processedLines = computed(() => {
  if (!props.log) return []
  return props.log.split('\n').map((line, index) => ({
    number: index + 1,
    content: line,
    id: `L${index + 1}`,
  }))
})

const { list: virtualLines, containerProps, wrapperProps, scrollTo } = useVirtualList(
  processedLines,
  {
    itemHeight: 22,
    overscan: 20,
  },
)

const matches = computed(() => {
  const needle = searchTerm.value.trim().toLowerCase()
  if (!needle) return []
  const results: { lineIndex: number }[] = []
  processedLines.value.forEach((line, idx) => {
    if (line.content.toLowerCase().includes(needle)) {
      results.push({ lineIndex: idx })
    }
  })
  return results
})

const scrollToMatch = () => {
  const match = matches.value[currentMatchIndex.value]
  if (!match) return
  scrollTo(match.lineIndex, { block: 'center' })
}

watchDebounced(
  searchTerm,
  () => {
    currentMatchIndex.value = matches.value.length > 0 ? 0 : -1
    scrollToMatch()
  },
  { debounce: 200 },
)

const nextMatch = () => {
  if (matches.value.length === 0) return
  currentMatchIndex.value = (currentMatchIndex.value + 1) % matches.value.length
  scrollToMatch()
}

const prevMatch = () => {
  if (matches.value.length === 0) return
  currentMatchIndex.value =
    (currentMatchIndex.value - 1 + matches.value.length) % matches.value.length
  scrollToMatch()
}

const { copy, copied } = useClipboard({ source: () => props.log })

const downloadLog = () => {
  const blob = new Blob([props.log], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `log-${new Date().toISOString()}.txt`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

const toggleFullscreen = () => {
  isFullscreen.value = !isFullscreen.value
}
</script>

<template>
  <div
    class="flex flex-col bg-[#1e1e1e] text-[#d4d4d4] rounded-lg overflow-hidden border border-[#333] shadow-xl transition-all duration-300"
    :class="[
      isFullscreen ? 'fixed inset-0 z-50 h-screen w-screen rounded-none' : 'w-full',
      !isFullscreen && maxHeight ? '' : 'h-full',
    ]"
    :style="!isFullscreen && maxHeight ? { height: maxHeight } : {}"
  >
    <div class="flex items-center justify-between px-4 py-2 bg-[#252526] border-b border-[#333] select-none shrink-0">
      <div class="flex items-center space-x-3 overflow-hidden">
        <span class="text-sm font-medium text-gray-300 truncate">{{ title || 'Execution Log' }}</span>
        <span class="text-xs text-gray-500 font-mono">{{ processedLines.length }} lines</span>
      </div>
      <div class="flex items-center space-x-2">
        <button
          @click="showSearch = !showSearch"
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors cursor-pointer"
          :title="showSearch ? '关闭搜索' : '搜索'"
          :aria-label="showSearch ? '关闭搜索' : '搜索日志'"
        >
          <Search class="w-4 h-4" />
        </button>
        <button
          @click="copy()"
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors cursor-pointer"
          :title="copied ? '已复制' : '复制全部'"
          aria-label="复制全部日志"
        >
          <Check v-if="copied" class="w-4 h-4 text-green-400" />
          <Copy v-else class="w-4 h-4" />
        </button>
        <button
          @click="downloadLog"
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors cursor-pointer"
          title="下载"
          aria-label="下载日志"
        >
          <Download class="w-4 h-4" />
        </button>
        <button
          @click="toggleFullscreen"
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors cursor-pointer"
          :title="isFullscreen ? '退出全屏' : '全屏'"
          :aria-label="isFullscreen ? '退出全屏' : '全屏'"
        >
          <Minimize2 v-if="isFullscreen" class="w-4 h-4" />
          <Maximize2 v-else class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div v-if="showSearch" class="flex items-center px-4 py-2 bg-[#2d2d2d] border-b border-[#333] shrink-0">
      <div class="relative flex-grow">
        <input
          v-model="searchTerm"
          type="text"
          placeholder="搜索日志内容…"
          aria-label="搜索日志内容"
          class="w-full bg-[#1e1e1e] text-white text-base sm:text-xs rounded px-3 py-1.5 border border-[#3e3e42] focus:outline-none focus:border-blue-500"
          @keydown.enter="nextMatch"
        />
        <span v-if="matches.length > 0" class="absolute right-2 top-1.5 text-xs text-gray-500">
          {{ currentMatchIndex + 1 }}/{{ matches.length }}
        </span>
      </div>
      <div class="flex ml-2 space-x-1">
        <button @click="prevMatch" class="p-1 hover:bg-[#3c3c3c] rounded text-gray-400 cursor-pointer" aria-label="上一个匹配">
          <ArrowUp class="w-4 h-4" />
        </button>
        <button @click="nextMatch" class="p-1 hover:bg-[#3c3c3c] rounded text-gray-400 cursor-pointer" aria-label="下一个匹配">
          <ArrowDown class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div
      v-if="processedLines.length === 0"
      class="flex-1 flex items-center justify-center text-gray-500 italic text-xs sm:text-sm"
    >
      No logs available
    </div>
    <div
      v-else
      :ref="(el) => { containerProps.ref.value = el as HTMLElement | null }"
      :style="containerProps.style"
      class="flex-1 overflow-auto font-mono text-xs sm:text-sm px-4 py-2 scrollbar-thin scrollbar-thumb-[#424242] scrollbar-track-transparent"
      data-testid="log-viewer-scroll"
      @scroll="containerProps.onScroll"
    >
      <div v-bind="wrapperProps" class="min-w-full">
        <div
          v-for="{ data: line, index } in virtualLines"
          :key="line.id"
          class="flex h-[22px] leading-[22px] items-center group hover:bg-[#2a2d2e] whitespace-pre"
          :class="{ 'bg-[#37373d]': matches.length > 0 && matches[currentMatchIndex]?.lineIndex === index }"
        >
          <span class="flex-shrink-0 w-10 sm:w-12 text-right text-gray-600 select-none mr-3 sm:mr-4 border-r border-[#333] pr-3 sm:pr-4 group-hover:text-gray-500">
            {{ line.number }}
          </span>
          <span class="text-[#d4d4d4]">{{ line.content }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrollbar-thin::-webkit-scrollbar { width: 10px; height: 10px; }
.scrollbar-thin::-webkit-scrollbar-track { background: transparent; }
.scrollbar-thin::-webkit-scrollbar-thumb { background: #424242; border-radius: 5px; border: 2px solid #1e1e1e; }
.scrollbar-thin::-webkit-scrollbar-thumb:hover { background: #4f4f4f; }
</style>
