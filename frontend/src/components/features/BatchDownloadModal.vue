<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi } from '../../api'
import { buildQueries } from '../../lib'

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
    const msg = err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      : err instanceof Error ? err.message : '下载失败'
    emit('download-error', msg || '下载失败')
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
    </div>
  </BaseModal>
</template>
