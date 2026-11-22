<script setup lang="ts">
import { computed, ref } from 'vue'
import { Ban, CheckCircle, Clock, Download, FileText, RefreshCw, XCircle } from '@lucide/vue'
import LoadMore from './LoadMore.vue'
import LogViewer from './LogViewer.vue'
import SlideOverDrawer from '../ui/SlideOverDrawer.vue'
import { getApiErrorMessage, novelApi, taskApi } from '../../api'
import { downloadUrl } from '../../lib/utils'
import type { TaskHistory } from '../../types'

const props = defineProps<{
  history: TaskHistory[]
  loading: boolean
  hasMore: boolean
  cancellingIds?: ReadonlySet<number>
}>()

const emit = defineEmits<{
  loadMore: []
  cancel: [taskId: number]
}>()

interface TaskHistoryRow {
  item: TaskHistory
  isExport: boolean
  argsText: string
  exportLabel: string
  progressText: string
  startTimeText: string
  newNovelsCount: number | null
  newNovelTitles: string[]
}

const drawerOpen = ref(false)
const drawerMode = ref<'log' | 'titles'>('log')
const drawerContent = ref('')
const drawerTitle = computed(() =>
  drawerMode.value === 'log' ? '执行日志' : '新增小说清单',
)
const drawerLogTitle = computed(() =>
  drawerMode.value === 'log' ? 'Log Output' : 'Novel Titles',
)

const isExportTask = (item: TaskHistory): boolean =>
  item.task_func === 'batch_export' || item.name === 'batch_export'

const exportLabel = (item: TaskHistory): string => {
  const args = item.arguments
  if (!args) return ''
  const ids = Array.isArray(args.novel_ids) ? args.novel_ids.length : 0
  const fmt = args.format_mode === 'prefer_epub' ? '优先 epub' : 'txt'
  return `批量导出 · ${ids} 篇 · ${fmt}`
}

const progressText = (item: TaskHistory): string => {
  if (typeof item.progress === 'string' && item.progress) return item.progress
  return typeof item.result?.summary === 'string' ? item.result.summary : ''
}

const newNovelsCount = (item: TaskHistory): number | null =>
  typeof item.result?.new_novels_count === 'number' ? item.result.new_novels_count : null

const newNovelTitles = (item: TaskHistory): string[] =>
  Array.isArray(item.result?.new_novel_titles)
    ? (item.result.new_novel_titles as string[])
    : []

const argsText = (item: TaskHistory): string =>
  item.arguments ? JSON.stringify(item.arguments) : 'None'

const formatDate = (dateStr?: string | null): string =>
  dateStr ? new Date(dateStr).toLocaleString() : '-'

const rows = computed<TaskHistoryRow[]>(() =>
  props.history.map((item) => {
    const isExport = isExportTask(item)
    return {
      item,
      isExport,
      argsText: isExport ? '' : argsText(item),
      exportLabel: isExport ? exportLabel(item) : '',
      progressText: progressText(item),
      startTimeText: formatDate(item.start_time),
      newNovelsCount: newNovelsCount(item),
      newNovelTitles: newNovelTitles(item),
    }
  }),
)

const handleDownloadExport = (item: TaskHistory) => {
  downloadUrl(novelApi.exportDownloadUrl(item.id))
}

const showLog = async (item: TaskHistory) => {
  drawerMode.value = 'log'
  drawerOpen.value = true
  drawerContent.value = ''
  if (!item.has_log) {
    drawerContent.value = '无输出日志'
    return
  }
  try {
    const data = await taskApi.getTaskLog(item.id)
    drawerContent.value = data.log || '无输出日志'
  } catch (err) {
    drawerContent.value = `日志获取失败: ${getApiErrorMessage(err, '未知错误')}`
  }
}

const showTitles = (titles: string[]) => {
  drawerMode.value = 'titles'
  drawerContent.value = titles.join('\n')
  drawerOpen.value = true
}

const statusIs = (status: string, ...names: string[]) =>
  names.includes(status.toLowerCase())
</script>

<template>
  <div>
    <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
      <ul class="divide-y divide-gray-200">
        <li v-if="loading && history.length === 0" class="px-6 py-12 text-center text-gray-500">
          <RefreshCw class="w-6 h-6 animate-spin mx-auto text-blue-500 mb-2" /> 加载中...
        </li>
        <li v-else-if="history.length === 0" class="px-6 py-12 text-center text-gray-500">暂无历史记录</li>
        <li v-for="row in rows" :key="row.item.id" class="px-6 py-4 hover:bg-gray-50">
          <div class="flex items-center justify-between w-full">
            <div class="flex items-center min-w-0 flex-1">
              <div class="shrink-0 mr-4">
                <div v-if="statusIs(row.item.status, 'success')" class="text-green-500"><CheckCircle class="w-8 h-8" /></div>
                <div v-else-if="statusIs(row.item.status, 'failed')" class="text-red-500"><XCircle class="w-8 h-8" /></div>
                <div v-else-if="statusIs(row.item.status, 'running')" class="text-blue-500 animate-spin"><RefreshCw class="w-8 h-8" /></div>
                <div v-else-if="statusIs(row.item.status, 'pending')" class="text-yellow-500"><Clock class="w-8 h-8" /></div>
                <div v-else-if="statusIs(row.item.status, 'cancelled')" class="text-gray-400"><Ban class="w-8 h-8" /></div>
                <div v-else class="text-gray-400"><Clock class="w-8 h-8" /></div>
              </div>
              <div class="min-w-0 flex-1 ml-4 grid grid-cols-1 sm:grid-cols-3 gap-2 sm:gap-4 items-center">
                <div class="col-span-1 min-w-0">
                  <p class="text-sm font-medium text-gray-900 truncate">{{ row.item.name }}</p>
                  <template v-if="row.isExport">
                    <p class="text-xs text-gray-500 mt-1 truncate">{{ row.exportLabel }}</p>
                    <p
                      v-if="statusIs(row.item.status, 'running', 'pending') && row.progressText"
                      class="text-xs text-blue-600 mt-0.5 truncate"
                      :title="row.progressText"
                    >
                      {{ row.progressText }}
                    </p>
                  </template>
                  <p v-else class="text-xs text-gray-500 mt-1 font-mono truncate max-w-lg" :title="row.argsText">
                    args: {{ row.argsText }}
                  </p>
                </div>
                <div class="col-span-1 text-xs text-gray-500 flex flex-col justify-center">
                  <span>开始: {{ row.startTimeText }}</span>
                  <span v-if="row.newNovelsCount !== null" class="text-green-600 font-medium mt-0.5">
                    新增小说: {{ row.newNovelsCount }}
                  </span>
                  <span v-if="typeof row.item.duration === 'number'" class="mt-0.5">耗时: {{ row.item.duration.toFixed(2) }}s</span>
                </div>
                <div class="col-span-1 flex items-center justify-start sm:justify-end">
                  <div class="w-auto sm:w-56 flex flex-wrap gap-2 sm:flex-nowrap justify-start sm:justify-end">
                    <button
                      v-if="statusIs(row.item.status, 'pending', 'running')"
                      :disabled="cancellingIds?.has(row.item.id)"
                      @click="emit('cancel', row.item.id)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-red-300 shadow-sm text-xs font-medium rounded text-red-700 bg-red-50 hover:bg-red-100 focus:outline-none transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <Ban class="w-3.5 h-3.5 mr-1" />
                      {{ cancellingIds?.has(row.item.id) ? '取消中…' : '取消' }}
                    </button>
                    <button
                      v-if="row.isExport && statusIs(row.item.status, 'success')"
                      @click="handleDownloadExport(row.item)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-blue-300 shadow-sm text-xs font-medium rounded text-blue-700 bg-blue-50 hover:bg-blue-100 focus:outline-none transition-colors cursor-pointer"
                    >
                      <Download class="w-3.5 h-3.5 mr-1" />
                      下载
                    </button>
                    <button
                      v-if="row.newNovelTitles.length > 0"
                      @click="showTitles(row.newNovelTitles)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors cursor-pointer"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" /> 清单
                    </button>
                    <button
                      v-if="row.item.has_log || statusIs(row.item.status, 'failed')"
                      @click="showLog(row.item)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors cursor-pointer"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" /> 日志
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </li>
      </ul>
    </div>

    <LoadMore :loading="loading" :no-more-data="!hasMore" :has-data="history.length > 0" @load-more="emit('loadMore')" />

    <SlideOverDrawer :open="drawerOpen" :title="drawerTitle" @close="drawerOpen = false">
      <LogViewer :log="drawerContent" :title="drawerLogTitle" />
    </SlideOverDrawer>
  </div>
</template>
