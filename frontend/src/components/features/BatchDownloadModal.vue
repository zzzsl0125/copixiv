<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useDebounceFn } from '@vueuse/core'
import { RadioGroupIndicator, RadioGroupItem, RadioGroupRoot } from 'reka-ui'
import BaseModal from '../ui/BaseModal.vue'
import { getApiErrorMessage, isAbortError, novelApi } from '../../api'
import type { FormatMode } from '../../types'
import { useSystem } from '../../composables'

const props = defineProps<{
  isOpen: boolean
  novelIds: number[]
}>()

const emit = defineEmits<{
  close: []
  'download-error': [message: string]
  'task-submitted': [payload: { task_id: number; matched: number }]
}>()

const loading = ref(false)
const formatMode = ref<FormatMode>('txt')
const { systemConfig } = useSystem()

const zipName = ref('')
const namingTemplate = ref('')

const previewPath = ref<string | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
let previewSeq = 0
let previewAbort: AbortController | null = null
let suppressPreviewWatch = false

const totalCount = computed(() => props.novelIds.length)

async function fetchPreview() {
  if (props.novelIds.length === 0) {
    previewPath.value = null
    previewError.value = ''
    return
  }
  const seq = ++previewSeq
  previewAbort?.abort()
  const controller = new AbortController()
  previewAbort = controller
  previewLoading.value = true
  previewError.value = ''
  try {
    const result = await novelApi.batchExportPreview(
      {
        novel_ids: props.novelIds,
        format_mode: formatMode.value,
        naming_template: namingTemplate.value || undefined,
      },
      controller.signal,
    )
    if (seq !== previewSeq) return
    previewPath.value = result.path
  } catch (err: unknown) {
    if (seq !== previewSeq) return
    if (isAbortError(err)) return
    previewPath.value = null
    previewError.value = getApiErrorMessage(err, '预览失败')
  } finally {
    if (seq === previewSeq) previewLoading.value = false
  }
}

const debouncedFetchPreview = useDebounceFn(() => {
  if (props.isOpen) void fetchPreview()
}, 300)

watch([namingTemplate, formatMode, () => props.novelIds], () => {
  if (suppressPreviewWatch || !props.isOpen) return
  if (totalCount.value === 0) {
    previewPath.value = null
    previewError.value = ''
    return
  }
  void debouncedFetchPreview()
})

onUnmounted(() => {
  previewAbort?.abort()
})

watch(
  () => props.isOpen,
  async (open) => {
    if (!open) return
    suppressPreviewWatch = true
    formatMode.value = 'txt'
    zipName.value = '批量导出'
    namingTemplate.value = systemConfig.value?.batch_download_naming || ''
    await nextTick()
    suppressPreviewWatch = false
    if (totalCount.value > 0) {
      void fetchPreview()
    } else {
      previewPath.value = null
      previewError.value = ''
    }
  },
  { immediate: true },
)

async function handleConfirm() {
  if (loading.value || totalCount.value === 0) return
  loading.value = true
  try {
    const res = await novelApi.submitBatchExport({
      novel_ids: props.novelIds,
      format_mode: formatMode.value,
      zip_name: zipName.value.trim() || undefined,
      naming_template: namingTemplate.value || undefined,
    })
    emit('task-submitted', res)
    emit('close')
  } catch (err: unknown) {
    emit('download-error', getApiErrorMessage(err, '提交导出任务失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="📦 批量导出"
    :loading="loading"
    confirm-text="确认导出"
    :confirm-disabled="totalCount === 0"
    @close="$emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-600 space-y-1">
      <div class="flex items-center gap-2">
        <span class="font-medium text-gray-700 w-16 shrink-0">范围：</span>
        <span class="text-lg font-bold" :class="totalCount > 0 ? 'text-blue-600' : 'text-gray-400'">
          {{ totalCount > 0 ? `已勾选 ${totalCount} 篇` : '未勾选小说' }}
        </span>
      </div>
    </div>
    <p class="text-xs bg-blue-50 border border-blue-200 text-blue-700 rounded-md px-3 py-2">
      将作为后台任务打包——提交后可关闭页面，进度与下载入口在「任务管理」页。
    </p>
    <div v-if="totalCount > 0" class="space-y-3">
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">文件格式</label>
        <RadioGroupRoot
          :model-value="formatMode"
          class="flex gap-4"
          aria-label="文件格式"
          @update:model-value="formatMode = $event as FormatMode"
        >
          <label class="inline-flex items-center gap-2 cursor-pointer">
            <RadioGroupItem
              value="txt"
              class="h-4 w-4 rounded-full border border-gray-300 bg-white flex items-center justify-center focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 data-[state=checked]:border-blue-600"
            >
              <RadioGroupIndicator class="h-2 w-2 rounded-full bg-blue-600" />
            </RadioGroupItem>
            <span class="text-sm text-gray-700">仅 txt</span>
          </label>
          <label class="inline-flex items-center gap-2 cursor-pointer">
            <RadioGroupItem
              value="prefer_epub"
              class="h-4 w-4 rounded-full border border-gray-300 bg-white flex items-center justify-center focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 data-[state=checked]:border-blue-600"
            >
              <RadioGroupIndicator class="h-2 w-2 rounded-full bg-blue-600" />
            </RadioGroupItem>
            <span class="text-sm text-gray-700">优先 epub</span>
          </label>
        </RadioGroupRoot>
        <p v-if="formatMode === 'prefer_epub'" class="mt-1 text-xs text-gray-500">
          有 epub 的小说使用 .epub，否则回退为 .txt
        </p>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">压缩包名</label>
        <div class="flex items-center gap-1">
          <input
            v-model="zipName"
            type="text"
            class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          />
          <span class="text-sm text-gray-400 shrink-0">.zip</span>
        </div>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">命名规则</label>
        <input
          v-model="namingTemplate"
          type="text"
          class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm font-mono text-xs"
        />
        <div class="mt-1 text-xs">
          <span v-if="previewLoading" class="text-gray-400">正在生成预览…</span>
          <span v-else-if="previewError" class="text-red-500">{{ previewError }}</span>
          <span v-else-if="previewPath" class="text-gray-500">
            首篇预览：<code class="bg-gray-100 px-1 rounded font-mono">{{ previewPath }}</code>
          </span>
          <span v-else class="text-gray-400">无匹配作品，无法预览</span>
        </div>
        <p class="mt-1 text-xs text-gray-400">
          用 <code class="bg-gray-100 px-1 rounded">/</code> 分隔建立子文件夹，用
          <code class="bg-gray-100 px-1 rounded">{}</code> 框起关键词变量。例如
          <code class="bg-gray-100 px-1 rounded">pixiv/{id}-{title}-by-{author_name}</code>
          。为防止文件名重复，命名规则中必须含有
          <code class="bg-gray-100 px-1 rounded">{id}</code>
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
          <li><code class="bg-gray-100 px-1 rounded">{source}</code> 数据源</li>
        </ul>
      </div>
    </div>
  </BaseModal>
</template>
