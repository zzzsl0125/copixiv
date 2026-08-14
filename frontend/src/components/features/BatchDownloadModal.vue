<script setup lang="ts">
import { ref, watch, computed, onUnmounted } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi } from '../../api'
import { buildQueries } from '../../lib/utils'
import { useSystem } from '../../composables'

const props = defineProps<{
  isOpen: boolean
  keyword: string
  order_by: string
  order_direction: string
  min_like?: number
  min_text?: number
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'download-success', filename: string): void
  (e: 'download-error', message: string): void
}>()

const loading = ref(false)
const countLoading = ref(false)
const totalCount = ref(0)
const downloadLimit = ref(50)
const formatMode = ref<'txt' | 'prefer_epub'>('txt')
const { systemConfig } = useSystem()

const zipName = ref('')
const namingTemplate = ref('')

const previewPath = ref<string | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
let previewTimer: ReturnType<typeof setTimeout> | undefined
let previewSeq = 0

async function fetchPreview() {
  const seq = ++previewSeq
  const queries = buildQueries(props.keyword)
  previewLoading.value = true
  previewError.value = ''
  try {
    const result = await novelApi.batchDownloadPreview({
      queries: Object.keys(queries).length > 0 ? JSON.stringify(queries) : undefined,
      order_by: props.order_by,
      order_direction: props.order_direction,
      min_like: props.min_like,
      min_text: props.min_text,
      format_mode: formatMode.value,
      naming_template: namingTemplate.value || undefined,
    })
    if (seq !== previewSeq) return
    previewPath.value = result.path
  } catch (err: unknown) {
    if (seq !== previewSeq) return
    const msg = err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      : err instanceof Error ? err.message : '预览失败'
    previewPath.value = null
    previewError.value = msg || '预览失败'
  } finally {
    if (seq === previewSeq) previewLoading.value = false
  }
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(fetchPreview, 300)
}

watch(
  [
    namingTemplate,
    formatMode,
    () => props.keyword,
    () => props.order_by,
    () => props.order_direction,
    () => props.min_like,
    () => props.min_text,
  ],
  schedulePreview,
)

onUnmounted(() => {
  if (previewTimer) clearTimeout(previewTimer)
})

const keywordDisplay = computed(() => {
  if (!props.keyword.trim()) return '（无）'
  const parts = props.keyword.split(/[;；]/).filter(Boolean)
  const keywords = parts
    .map(p => {
      const idx = p.indexOf(':')
      if (idx > 0) return { type: p.substring(0, idx).trim(), value: p.substring(idx + 1).trim() }
      return { type: 'keyword', value: p.trim() }
    })
    .filter(k => k.type === 'keyword')
    .map(k => k.value)
  return keywords.length > 0 ? keywords.join(', ') : props.keyword
})

const searchValues = computed(() => {
  if (!props.keyword.trim()) return [] as string[]
  const parts = props.keyword.split(/[;；]/).filter(Boolean)
  return parts
    .map(p => {
      const idx = p.indexOf(':')
      if (idx > 0) return p.substring(idx + 1).trim()
      return p.trim()
    })
    .filter(Boolean)
})

function defaultZipName(): string {
  if (searchValues.value.length > 0) {
    return searchValues.value.slice(0, 3).join('_')
  }
  return `批量下载`
}

const orderByDisplay = computed(() => {
  const map: Record<string, string> = {
    id: '默认排序', like: '按赞数', view: '按浏览数', text: '按字数', random: '随机', create_time: '按创建时间',
  }
  const dir = props.order_direction === 'ASC' ? '↑' : '↓'
  return `${map[props.order_by] || props.order_by} ${dir}`
})

watch(() => props.isOpen, async (open) => {
  console.log('[BatchDownloadModal] isOpen changed:', open, 'keyword:', props.keyword)
  if (!open) return
  totalCount.value = 0
  downloadLimit.value = 50
  formatMode.value = 'txt'
  zipName.value = defaultZipName()
  namingTemplate.value = systemConfig.value?.batch_download_naming || ''
  countLoading.value = true
  try {
    const queries = buildQueries(props.keyword)
    console.log('[BatchDownloadModal] built queries:', queries)
    const result = await novelApi.countNovels({
      queries: Object.keys(queries).length > 0 ? queries : undefined,
      min_like: props.min_like,
      min_text: props.min_text,
    })
    console.log('[BatchDownloadModal] count result:', result)
    totalCount.value = result.total
    if (downloadLimit.value > result.total) downloadLimit.value = result.total || 1
    zipName.value = defaultZipName()
    schedulePreview()
  } catch (err) {
    console.error('[BatchDownloadModal] count failed:', err)
    totalCount.value = 0
  } finally {
    countLoading.value = false
  }
})

async function handleConfirm() {
  if (totalCount.value === 0) return
  loading.value = true
  try {
    const queries = buildQueries(props.keyword)
    const queryJson = Object.keys(queries).length > 0 ? JSON.stringify(queries) : undefined
    const response = await novelApi.batchDownload({
      queries: queryJson,
      order_by: props.order_by,
      order_direction: props.order_direction,
      min_like: props.min_like,
      min_text: props.min_text,
      limit: downloadLimit.value,
      format_mode: formatMode.value,
      zip_name: zipName.value || undefined,
      naming_template: namingTemplate.value || undefined,
    })

    const blob = response.data as Blob
    const disposition = response.headers['content-disposition'] || ''
    const match = disposition.match(/filename\*=UTF-8''(.+)/)
    let filename = 'batch_download.zip'
    if (match) filename = decodeURIComponent(match[1])

    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)

    emit('download-success', filename)
    emit('close')
  } catch (err: unknown) {
    // responseType is 'blob', so error bodies arrive as Blobs — read the
    // JSON out of them instead of showing a generic message.
    let msg = '下载失败'
    if (err && typeof err === 'object' && 'response' in err) {
      const resp = (err as { response?: { data?: unknown; status?: number } }).response
      const data = resp?.data
      if (data instanceof Blob) {
        try {
          const text = await data.text()
          const parsed = JSON.parse(text) as { detail?: string }
          if (parsed.detail) msg = parsed.detail
        } catch { /* non-JSON error body — keep generic message */ }
      } else if (data && typeof data === 'object' && 'detail' in data) {
        msg = (data as { detail?: string }).detail || msg
      }
    } else if (err instanceof Error) {
      msg = err.message
    }
    emit('download-error', msg)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal :is-open="isOpen" title="📦 打包下载" :loading="loading" confirm-text="确认下载" @close="$emit('close')" @confirm="handleConfirm">
    <div class="text-sm text-gray-600 space-y-1">
      <div class="flex items-center gap-2"><span class="font-medium text-gray-700 w-16 shrink-0">关键词：</span><span class="truncate text-gray-900">{{ keywordDisplay }}</span></div>
      <div class="flex items-center gap-2"><span class="font-medium text-gray-700 w-16 shrink-0">排序：</span><span class="text-gray-900">{{ orderByDisplay }}</span></div>
      <div v-if="min_like || min_text" class="flex items-center gap-2">
        <span class="font-medium text-gray-700 w-16 shrink-0">筛选：</span>
        <span class="text-gray-900">
          <template v-if="min_like">≥ {{ min_like }} 赞</template>
          <template v-if="min_like && min_text"> · </template>
          <template v-if="min_text">≥ {{ min_text }} 字</template>
        </span>
      </div>
    </div>
    <hr class="border-gray-200" />
    <div class="flex items-center gap-2 text-sm">
      <span class="font-medium text-gray-700">本地匹配：</span>
      <span v-if="countLoading" class="text-gray-400">正在统计…</span>
      <span v-else class="text-lg font-bold" :class="totalCount > 0 ? 'text-blue-600' : 'text-gray-400'">{{ totalCount > 0 ? `${totalCount} 篇` : '无匹配结果' }}</span>
    </div>
    <div v-if="totalCount > 0" class="space-y-3">
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">下载数量</label>
        <div class="flex items-center gap-2">
          <input v-model.number="downloadLimit" type="number" min="1" :max="totalCount" class="block w-24 px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
          <span class="text-sm text-gray-500">/ 共 {{ totalCount }} 篇</span>
          <button v-if="totalCount > 50" type="button" class="text-sm text-blue-600 hover:text-blue-800" @click="downloadLimit = totalCount">下载全部</button>
        </div>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">文件格式</label>
        <div class="flex gap-4">
          <label class="inline-flex items-center"><input v-model="formatMode" type="radio" value="txt" class="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300" /><span class="ml-2 text-sm text-gray-700">仅 txt</span></label>
          <label class="inline-flex items-center"><input v-model="formatMode" type="radio" value="prefer_epub" class="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300" /><span class="ml-2 text-sm text-gray-700">优先 epub</span></label>
        </div>
        <p v-if="formatMode === 'prefer_epub'" class="mt-1 text-xs text-gray-500">有 epub 的小说使用 .epub，否则回退为 .txt</p>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">压缩包名</label>
        <div class="flex items-center gap-1">
          <input v-model="zipName" type="text" class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
          <span class="text-sm text-gray-400 shrink-0">.zip</span>
        </div>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">命名规则</label>
        <input v-model="namingTemplate" type="text" class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm font-mono text-xs" />
        <div class="mt-1 text-xs">
          <span v-if="previewLoading" class="text-gray-400">正在生成预览…</span>
          <span v-else-if="previewError" class="text-red-500">{{ previewError }}</span>
          <span v-else-if="previewPath" class="text-gray-500">
            首篇预览：<code class="bg-gray-100 px-1 rounded font-mono">{{ previewPath }}</code>
          </span>
          <span v-else class="text-gray-400">无匹配作品，无法预览</span>
        </div>
        <p class="mt-1 text-xs text-gray-400">
          用 <code class="bg-gray-100 px-1 rounded">/</code> 分隔建立子文件夹，用 <code class="bg-gray-100 px-1 rounded">{}</code> 框起关键词变量。例如
          <code class="bg-gray-100 px-1 rounded">pixiv/{id}-{title}-by-{author_name}</code>。为防止文件名重复，命名规则中必须含有 <code class="bg-gray-100 px-1 rounded">{id}</code>
        </p>
        <p class="mt-1 text-xs text-gray-400">可用关键词：</p>
        <ul class="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-gray-400">
          <li><code class="bg-gray-100 px-1 rounded">{id}</code> 作品 ID</li>
          <li><code class="bg-gray-100 px-1 rounded">{title}</code> 标题</li>
          <li><code class="bg-gray-100 px-1 rounded">{author_name}</code> 作者名</li>
          <li><code class="bg-gray-100 px-1 rounded">{author_id}</code> 作者 ID</li>
          <li><code class="bg-gray-100 px-1 rounded">{like}</code> 赞数</li>
          <li><code class="bg-gray-100 px-1 rounded">{view}</code> 浏览数</li>
          <li><code class="bg-gray-100 px-1 rounded">{text}</code> 字数</li>
          <li><code class="bg-gray-100 px-1 rounded">{date}</code> 创建日期</li>
          <li><code class="bg-gray-100 px-1 rounded">{series_name}</code> 系列名</li>
          <li><code class="bg-gray-100 px-1 rounded">{series_index}</code> 系列序号</li>
        </ul>
      </div>
    </div>
  </BaseModal>
</template>
