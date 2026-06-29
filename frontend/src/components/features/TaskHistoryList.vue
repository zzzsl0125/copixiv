<script setup lang="ts">
import { ref } from 'vue'
import { RefreshCw, Clock, FileText, CheckCircle, XCircle } from '@lucide/vue'
import LoadMore from './LoadMore.vue'
import StatusBadge from '../ui/StatusBadge.vue'
import LogViewer from './LogViewer.vue'
import type { TaskHistory } from '../../types'

defineProps<{
  history: TaskHistory[]
  loading: boolean
  hasMore: boolean
}>()

const emit = defineEmits<{ (e: 'loadMore'): void }>()

const logModalOpen = ref(false)
const currentLog = ref('')
const titlesModalOpen = ref(false)
const currentTitles = ref('')

const parseResult = (resultStr?: string | null) => {
  if (!resultStr) return { log: '', new_novels_count: null, new_novel_titles: [] as string[] }
  try {
    const parsed = JSON.parse(resultStr)
    if (parsed && typeof parsed === 'object') {
      return {
        log: parsed.log || '',
        new_novels_count: parsed.new_novels_count,
        new_novel_titles: parsed.new_novel_titles || [],
      }
    }
  } catch { /* legacy plain text log */ }
  return { log: resultStr, new_novels_count: null, new_novel_titles: [] as string[] }
}

const showLog = (result: string) => {
  const parsed = parseResult(result)
  currentLog.value = parsed.log || '无输出日志'
  logModalOpen.value = true
}

const showTitles = (result: string) => {
  const parsed = parseResult(result)
  currentTitles.value = parsed.new_novel_titles.join('\n')
  titlesModalOpen.value = true
}

const formatDate = (dateStr?: string | null) => {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString()
}
</script>

<template>
  <div>
    <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
      <ul class="divide-y divide-gray-200">
        <li v-if="loading && history.length === 0" class="px-6 py-12 text-center text-gray-500">
          <RefreshCw class="w-6 h-6 animate-spin mx-auto text-blue-500 mb-2" /> 加载中...
        </li>
        <li v-else-if="history.length === 0" class="px-6 py-12 text-center text-gray-500">暂无历史记录</li>
        <li v-for="item in history" :key="item.id" class="px-6 py-4 hover:bg-gray-50 transition-colors">
          <div class="flex items-center justify-between w-full">
            <div class="flex items-center min-w-0 flex-1">
              <div class="flex-shrink-0 mr-4">
                <div v-if="item.status === 'SUCCESS'" class="text-green-500"><CheckCircle class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'FAILED'" class="text-red-500"><XCircle class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'RUNNING'" class="text-blue-500 animate-spin"><RefreshCw class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'PENDING'" class="text-yellow-500"><Clock class="w-8 h-8" /></div>
                <div v-else class="text-gray-400"><Clock class="w-8 h-8" /></div>
              </div>
              <div class="min-w-0 flex-1 ml-4 grid grid-cols-3 gap-4 items-center">
                <div class="col-span-1">
                  <p class="text-sm font-medium text-gray-900 truncate">{{ item.name }}</p>
                  <p class="text-xs text-gray-500 mt-1 font-mono truncate max-w-lg" :title="item.arguments">args: {{ item.arguments || 'None' }}</p>
                </div>
                <div class="col-span-1 text-xs text-gray-500 flex flex-col justify-center">
                  <span>开始: {{ formatDate(item.start_time) }}</span>
                  <span v-if="item.end_time">结束: {{ formatDate(item.end_time) }}</span>
                  <span v-if="parseResult(item.result).new_novels_count !== null && parseResult(item.result).new_novels_count !== undefined" class="text-green-600 font-medium mt-0.5">
                    新增小说: {{ parseResult(item.result).new_novels_count }}
                  </span>
                </div>
                <div class="col-span-1 flex items-center justify-end space-x-4">
                  <span v-if="typeof item.duration === 'number'" class="text-xs text-gray-500 whitespace-nowrap">耗时: {{ item.duration.toFixed(2) }}s</span>
                  <StatusBadge :status="item.status" />
                  <div class="w-48 flex justify-end space-x-2">
                    <button
                      v-if="parseResult(item.result).new_novel_titles && parseResult(item.result).new_novel_titles.length > 0"
                      @click="showTitles(item.result || '')"
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" /> 清单
                    </button>
                    <button
                      v-if="item.result || item.status === 'FAILED'"
                      @click="showLog(item.result || '')"
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" /> 日志
                    </button>
                    <span v-else class="w-full"></span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </li>
      </ul>
    </div>

    <LoadMore :loading="loading" :no-more-data="!hasMore" :has-data="history.length > 0" @load-more="emit('loadMore')" />

    <!-- Log Modal -->
    <div v-if="logModalOpen" class="fixed inset-0 z-50 overflow-hidden" role="dialog" aria-modal="true">
      <div class="absolute inset-0 overflow-hidden">
        <div class="absolute inset-0 bg-gray-500 bg-opacity-75 transition-opacity" @click="logModalOpen = false" aria-hidden="true"></div>
        <div class="pointer-events-none fixed inset-y-0 right-0 flex max-w-full pl-0 sm:pl-10">
          <div class="pointer-events-auto w-screen max-w-4xl transform transition duration-500 ease-in-out sm:duration-700">
            <div class="flex h-full flex-col bg-white shadow-xl">
              <div class="px-4 py-3 sm:px-6 bg-gray-50 border-b border-gray-200 flex justify-between items-center">
                <h3 class="text-lg font-medium text-gray-900">执行日志</h3>
                <button @click="logModalOpen = false" class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <span class="sr-only">Close panel</span>
                  <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
              <div class="relative flex-1 overflow-hidden bg-[#1e1e1e]">
                <LogViewer :log="currentLog" title="Log Output" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Titles Modal -->
    <div v-if="titlesModalOpen" class="fixed inset-0 z-50 overflow-hidden" role="dialog" aria-modal="true">
      <div class="absolute inset-0 overflow-hidden">
        <div class="absolute inset-0 bg-gray-500 bg-opacity-75 transition-opacity" @click="titlesModalOpen = false" aria-hidden="true"></div>
        <div class="pointer-events-none fixed inset-y-0 right-0 flex max-w-full pl-0 sm:pl-10">
          <div class="pointer-events-auto w-screen max-w-4xl transform transition duration-500 ease-in-out sm:duration-700">
            <div class="flex h-full flex-col bg-white shadow-xl">
              <div class="px-4 py-3 sm:px-6 bg-gray-50 border-b border-gray-200 flex justify-between items-center">
                <h3 class="text-lg font-medium text-gray-900">新增小说清单</h3>
                <button @click="titlesModalOpen = false" class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <span class="sr-only">Close panel</span>
                  <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
              <div class="relative flex-1 overflow-hidden bg-[#1e1e1e]">
                <LogViewer :log="currentTitles" title="Novel Titles" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
