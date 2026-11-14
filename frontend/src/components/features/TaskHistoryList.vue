<script setup lang="ts">
import { computed, ref } from 'vue'
import { RefreshCw, Clock, FileText, CheckCircle, XCircle, Download } from '@lucide/vue'
import LoadMore from './LoadMore.vue'
import LogViewer from './LogViewer.vue'
import { getApiErrorMessage, novelApi, taskApi } from '../../api'
import { downloadUrl } from '../../lib/utils'
import type { TaskHistory } from '../../types'

const props = defineProps<{
  history: TaskHistory[]
  loading: boolean
  hasMore: boolean
}>()

const emit = defineEmits<{ (e: 'loadMore'): void }>()

interface ParsedTaskResult {
  log: string
  summary: string
  new_novels_count: number | null | undefined
  new_novel_titles: string[]
}

interface RowStaticCache {
  result: TaskHistory['result']
  arguments: TaskHistory['arguments']
  startTime: TaskHistory['start_time']
  parsedResult: ParsedTaskResult
  argsText: string
  batchLabel: string
  startTimeText: string
}

interface TaskHistoryRow {
  item: TaskHistory
  isBatch: boolean
  argsText: string
  batchLabel: string
  progressText: string
  startTimeText: string
  newNovelsCount: number | null | undefined
  newNovelTitles: string[]
}

const logModalOpen = ref(false)
const currentLog = ref('')
const titlesModalOpen = ref(false)
const currentTitles = ref('')

const parseResult = (resultStr?: string | Record<string, any> | null): ParsedTaskResult => {
  if (!resultStr) return { log: '', summary: '', new_novels_count: null, new_novel_titles: [] }
  // ``result`` no longer carries ``log`` (the list endpoint strips it); the
  // log is fetched on demand. Everything else stays inline.
  // Handle the case where the API already returns a parsed object (not a JSON string)
  if (typeof resultStr === 'object') {
    return {
      log: typeof resultStr.log === 'string' ? resultStr.log : '',
      summary: typeof resultStr.summary === 'string' ? resultStr.summary : '',
      new_novels_count: resultStr.new_novels_count ?? null,
      new_novel_titles: resultStr.new_novel_titles || [],
    }
  }
  try {
    const parsed = JSON.parse(resultStr)
    if (parsed && typeof parsed === 'object') {
      return {
        log: typeof parsed.log === 'string' ? parsed.log : '',
        summary: typeof parsed.summary === 'string' ? parsed.summary : '',
        new_novels_count: parsed.new_novels_count,
        new_novel_titles: parsed.new_novel_titles || [],
      }
    }
  } catch { /* legacy plain text log */ }
  return { log: resultStr, summary: '', new_novels_count: null, new_novel_titles: [] }
}

/** Compact label for batch_* rows (args contain a huge ID list). */
const batchArgsLabel = (item: TaskHistory) => {
  let args: Record<string, unknown> | null = null
  try {
    args = typeof item.arguments === 'object'
      ? (item.arguments as Record<string, unknown>)
      : JSON.parse(item.arguments || 'null')
  } catch { /* ignore */ }
  if (!args) return ''
  if (item.name === 'batch_export') {
    const ids = Array.isArray(args.novel_ids) ? args.novel_ids.length : 0
    const fmt = args.format_mode === 'prefer_epub' ? '优先 epub' : 'txt'
    return `批量导出 · ${ids} 篇 · ${fmt}`
  }
  const labels: Record<string, string> = {
    delete: '批量删除',
    add_tags: '批量添加标签',
    remove_tags: '批量移除标签',
  }
  const op = labels[String(args.operation)] ?? String(args.operation)
  const ids = Array.isArray(args.novel_ids) ? args.novel_ids.length : 0
  const tags = Array.isArray(args.tags) && args.tags.length
    ? ` · 标签：${args.tags.join('、')}`
    : ''
  return `${op} · ${ids} 篇${tags}`
}

const isBatchTask = (item: TaskHistory) =>
  item.name === 'batch_operation' || item.name === 'batch_export'

const formatDate = (dateStr?: string | null) => {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString()
}

// Per-row cache keyed by the reactive item proxy so polling one active row
// does not re-run JSON.parse / JSON.stringify across unchanged history rows.
const rowCache = new WeakMap<TaskHistory, RowStaticCache>()

const getStaticFields = (item: TaskHistory, isBatch: boolean): RowStaticCache => {
  const cached = rowCache.get(item)
  if (
    cached &&
    cached.result === item.result &&
    cached.arguments === item.arguments &&
    cached.startTime === item.start_time
  ) {
    return cached
  }
  const next: RowStaticCache = {
    result: item.result,
    arguments: item.arguments,
    startTime: item.start_time,
    parsedResult: parseResult(item.result),
    argsText: isBatch ? '' : (item.arguments ? JSON.stringify(item.arguments) : 'None'),
    batchLabel: isBatch ? batchArgsLabel(item) : '',
    startTimeText: formatDate(item.start_time),
  }
  rowCache.set(item, next)
  return next
}

const rows = computed<TaskHistoryRow[]>(() =>
  props.history.map((item) => {
    const isBatch = isBatchTask(item)
    const fields = getStaticFields(item, isBatch)
    const progressText =
      typeof item.progress === 'string' && item.progress
        ? item.progress
        : fields.parsedResult.summary || ''
    return {
      item,
      isBatch,
      argsText: fields.argsText,
      batchLabel: fields.batchLabel,
      progressText,
      startTimeText: fields.startTimeText,
      newNovelsCount: fields.parsedResult.new_novels_count,
      newNovelTitles: fields.parsedResult.new_novel_titles,
    }
  }),
)

const handleDownloadExport = (item: TaskHistory) => {
  // Navigate straight to the attachment URL (works in in-app browsers /
  // WebViews that don't support blob: downloads).
  downloadUrl(novelApi.exportDownloadUrl(item.id))
}

const loadingLog = ref(false)

/**
 * Fetch and show one row's log.
 *
 * The list response carries only ``has_log`` now, so this is the single place
 * that loads log text — on demand, never during polling.
 */
const showLog = async (item: TaskHistory) => {
  logModalOpen.value = true
  currentLog.value = ''
  if (!item.has_log) {
    currentLog.value = '无输出日志'
    return
  }
  loadingLog.value = true
  try {
    const data = await taskApi.getTaskLog(item.id)
    currentLog.value = data.log || '无输出日志'
  } catch (err) {
    currentLog.value = `日志获取失败: ${getApiErrorMessage(err, '未知错误')}`
  } finally {
    loadingLog.value = false
  }
}

const showTitles = (titles: string[]) => {
  currentTitles.value = titles.join('\n')
  titlesModalOpen.value = true
}

/** Backend stores status values lowercase ("success"/"failed"/...). */
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
                <div v-else class="text-gray-400"><Clock class="w-8 h-8" /></div>
              </div>
              <div class="min-w-0 flex-1 ml-4 grid grid-cols-1 sm:grid-cols-3 gap-2 sm:gap-4 items-center">
                <div class="col-span-1 min-w-0">
                  <p class="text-sm font-medium text-gray-900 truncate">{{ row.item.name }}</p>
                  <template v-if="row.isBatch">
                    <p class="text-xs text-gray-500 mt-1 truncate">{{ row.batchLabel }}</p>
                    <!-- 进度优先读 progress 列；为空时回退到 result summary（兼容旧数据/终态） -->
                    <p v-if="statusIs(row.item.status, 'running', 'pending') && row.progressText" class="text-xs text-blue-600 mt-0.5 truncate" :title="row.progressText">
                      {{ row.progressText }}
                    </p>
                  </template>
                  <p v-else class="text-xs text-gray-500 mt-1 font-mono truncate max-w-lg" :title="row.argsText">args: {{ row.argsText }}</p>
                </div>
                <div class="col-span-1 text-xs text-gray-500 flex flex-col justify-center">
                  <span>开始: {{ row.startTimeText }}</span>
                  <span v-if="row.newNovelsCount !== null && row.newNovelsCount !== undefined" class="text-green-600 font-medium mt-0.5">
                    新增小说: {{ row.newNovelsCount }}
                  </span>
                  <span v-if="typeof row.item.duration === 'number'" class="mt-0.5">耗时: {{ row.item.duration.toFixed(2) }}s</span>
                </div>
                <div class="col-span-1 flex items-center justify-start sm:justify-end">
                  <div class="w-auto sm:w-48 flex flex-wrap gap-2 sm:flex-nowrap justify-start sm:justify-end">
                    <button
                      v-if="row.item.name === 'batch_export' && statusIs(row.item.status, 'success')"
                      @click="handleDownloadExport(row.item)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-blue-300 shadow-sm text-xs font-medium rounded text-blue-700 bg-blue-50 hover:bg-blue-100 focus:outline-none transition-colors"
                    >
                      <Download class="w-3.5 h-3.5 mr-1" />
                      下载
                    </button>
                    <button
                      v-if="row.newNovelTitles.length > 0"
                      @click="showTitles(row.newNovelTitles)"
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" /> 清单
                    </button>
                    <button
                      v-if="row.item.has_log || statusIs(row.item.status, 'failed')"
                      @click="showLog(row.item)"
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
        <div class="absolute inset-0 bg-gray-500/75 transition-opacity" @click="logModalOpen = false" aria-hidden="true"></div>
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
        <div class="absolute inset-0 bg-gray-500/75 transition-opacity" @click="titlesModalOpen = false" aria-hidden="true"></div>
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
