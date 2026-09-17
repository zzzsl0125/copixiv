<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { X, FileUp as FileUpIcon, CheckCircle2, AlertCircle } from '@lucide/vue'
import BaseModal from '../ui/BaseModal.vue'
import { libraryApi, getApiErrorMessage } from '../../api'
import { useToast } from '../../composables'
import { UPLOAD_STATUS_LABEL, formatFileSize } from '../../types'
import type { UploadItemResult } from '../../types'

const props = defineProps<{
  isOpen: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /** 上传成功（至少一条 created）——调用方刷新列表 / 重新加载当前 section。 */
  (e: 'uploaded', summary: { created: number; duplicate: number; failed: number }): void
  (e: 'error', message: string): void
}>()

const toast = useToast()

const fileInputRef = ref<HTMLInputElement | null>(null)
/** 待上传文件（跨多次选择累积；同名同大小的重复选择被忽略）。 */
const files = ref<File[]>([])
/** 后端逐条结果，按 filename 归并（重名文件合并为一条，与后端行为一致）。 */
const results = ref<UploadItemResult[]>([])
const uploading = ref(false)
/** 上传进度：0-100，取浏览器 onUploadProgress 的字节比例。 */
const progress = ref(0)
const hasUploaded = ref(false)
/** 本次提交的文件数（进度分母；提交后 files 会被结果批清空，故单独记）。 */
const batchTotal = ref(0)

const summary = computed(() => ({
  created: results.value.filter((r) => r.status === 'created').length,
  duplicate: results.value.filter((r) => r.status === 'duplicate').length,
  failed: results.value.filter((r) => r.status === 'failed').length,
}))

/** 上传中「已完成/总数」：已回结果的条数 / 本次提交的文件数。 */
const progressLabel = computed(() => `${results.value.length}/${batchTotal.value}`)

const reset = () => {
  files.value = []
  results.value = []
  uploading.value = false
  progress.value = 0
  hasUploaded.value = false
  batchTotal.value = 0
  if (fileInputRef.value) fileInputRef.value.value = ''
}

watch(() => props.isOpen, (open) => {
  if (open) reset()
})

const openPicker = () => fileInputRef.value?.click()

const handleFilePick = (event: Event) => {
  const input = event.target as HTMLInputElement
  const picked = Array.from(input.files || [])
  // 同一次选择里也可能带重复（大小相同即视为同一份），按 (name, size) 去重。
  const known = new Set(files.value.map((f) => `${f.name}:${f.size}`))
  for (const file of picked) {
    const key = `${file.name}:${file.size}`
    if (known.has(key)) continue
    known.add(key)
    files.value.push(file)
  }
  // 清空 input.value：否则再次选择同一文件不会触发 change。
  input.value = ''
}

const removeFile = (index: number) => {
  if (uploading.value) return
  files.value.splice(index, 1)
}

const handleUpload = async () => {
  if (uploading.value || files.value.length === 0) return
  const pending = [...files.value]
  uploading.value = true
  progress.value = 0
  batchTotal.value = pending.length
  try {
    const res = await libraryApi.uploadFiles(pending, (percent) => {
      progress.value = percent
    })
    const batch = res.results || []
    results.value = [...results.value, ...batch]
    // 后端按 filename 归并；重名文件不重复排队，未回结果的继续留在待上传列表。
    const answered = new Set(batch.map((r) => r.filename))
    files.value = pending.filter((f) => !answered.has(f.name))
    hasUploaded.value = true

    const counts = {
      created: batch.filter((r) => r.status === 'created').length,
      duplicate: batch.filter((r) => r.status === 'duplicate').length,
      failed: batch.filter((r) => r.status === 'failed').length,
    }
    if (counts.created > 0) {
      toast.success(`已入库 ${counts.created} 本`)
      emit('uploaded', counts)
    }
    if (counts.duplicate > 0) toast.info(`${counts.duplicate} 个文件已在库中（同一份正文）`)
    if (counts.failed > 0) toast.warning(`${counts.failed} 个文件上传失败，详见结果列表`)
    // 部分成功也要能刷新列表：只要有一本入库就足够。
    if (counts.created === 0 && counts.duplicate === 0 && counts.failed === 0) {
      toast.warning('后端没有返回任何上传结果')
    }
  } catch (err: unknown) {
    // 网络/整包错误（无文件 400、后端异常）：逐条结果不可用，整体报错。
    emit('error', getApiErrorMessage(err, '上传失败'))
  } finally {
    uploading.value = false
  }
}

/** 「继续上传」：清掉结果与待上传列表，回到选择态（已入库的书已在列表里）。 */
const uploadMore = () => {
  reset()
  openPicker()
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="📚 上传本地 TXT"
    confirm-text="上传"
    :cancel-text="hasUploaded ? '完成' : '取消'"
    :loading="uploading"
    :confirm-disabled="files.length === 0"
    @close="$emit('close')"
    @confirm="handleUpload"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p class="text-xs text-gray-500">
        支持多选，仅收 <b>.txt</b>（编码自动识别：UTF-8 / GB18030 / Big5 / UTF-16）。
        文件会复制入库，正文相同的书不会重复收录。
      </p>

      <input
        ref="fileInputRef"
        type="file"
        accept=".txt"
        multiple
        class="hidden"
        data-testid="upload-file-input"
        @change="handleFilePick"
      />

      <div class="flex gap-2">
        <button
          type="button"
          class="inline-flex items-center px-3 py-2 text-sm font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded-md transition-colors"
          :disabled="uploading"
          data-testid="upload-pick-button"
          @click="openPicker"
        >
          <FileUpIcon class="h-4 w-4 mr-1.5" /> 选择文件
        </button>
        <span v-if="!hasUploaded" class="self-center text-xs text-gray-500">
          已选 {{ files.length }} 个文件
        </span>
      </div>

      <!-- 上传中：进度 + 待上传列表 -->
      <div v-if="uploading" data-testid="upload-progress">
        <div class="flex justify-between text-xs text-gray-600 mb-1">
          <span>上传中…</span>
          <span>{{ progressLabel }} · {{ progress }}%</span>
        </div>
        <div class="w-full h-1.5 bg-gray-100 rounded">
          <div class="h-1.5 bg-blue-500 rounded transition-all" :style="{ width: `${progress}%` }"></div>
        </div>
      </div>

      <!-- 待上传列表（文件名 + 大小，可移除） -->
      <ul v-if="files.length > 0" class="divide-y divide-gray-100 border border-gray-200 rounded-md" data-testid="upload-file-list">
        <li v-for="(file, index) in files" :key="`${file.name}:${file.size}`" class="flex items-center justify-between px-3 py-2">
          <span class="truncate mr-2" :title="file.name">{{ file.name }}</span>
          <span class="flex items-center gap-2 shrink-0">
            <span class="text-xs text-gray-400">{{ formatFileSize(file.size) }}</span>
            <button
              type="button"
              class="text-gray-400 hover:text-red-500 disabled:opacity-40 disabled:cursor-not-allowed"
              :disabled="uploading"
              :aria-label="`移除 ${file.name}`"
              data-testid="upload-remove-file"
              @click="removeFile(index)"
            >
              <X class="h-4 w-4" />
            </button>
          </span>
        </li>
      </ul>

      <!-- 逐条结果 -->
      <div v-if="results.length > 0" data-testid="upload-result-list">
        <div class="flex items-center justify-between mb-1">
          <span class="font-medium text-gray-700">上传结果</span>
          <span class="text-xs" data-testid="upload-summary">
            成功 {{ summary.created }} / 重复 {{ summary.duplicate }} / 失败 {{ summary.failed }}
          </span>
        </div>
        <ul class="divide-y divide-gray-100 border border-gray-200 rounded-md max-h-56 overflow-y-auto">
          <li v-for="(item, index) in results" :key="`${item.filename}:${index}`" class="px-3 py-2">
            <div class="flex items-start justify-between gap-2">
              <span class="truncate" :title="item.filename">{{ item.filename }}</span>
              <span
                class="shrink-0 inline-flex items-center gap-1 text-xs font-medium"
                :class="item.status === 'created' ? 'text-green-600' : item.status === 'duplicate' ? 'text-amber-600' : 'text-red-600'"
              >
                <CheckCircle2 v-if="item.status === 'created'" class="h-3.5 w-3.5" />
                <AlertCircle v-else class="h-3.5 w-3.5" />
                {{ UPLOAD_STATUS_LABEL[item.status] }}
              </span>
            </div>
            <p v-if="item.status === 'created'" class="mt-0.5 text-xs text-gray-500">
              《{{ item.title || item.filename }}》
              <template v-if="item.author_name"> · {{ item.author_name }}</template>
              <template v-if="typeof item.text === 'number'"> · {{ item.text }} 字</template>
              <template v-if="typeof item.novel_id === 'number'"> · id {{ item.novel_id }}</template>
            </p>
            <!-- 重复：指出已有那本（标题 + id），便于过去查看 -->
            <p v-else-if="item.status === 'duplicate'" class="mt-0.5 text-xs text-gray-500">
              已在库中：《{{ item.title || '未知标题' }}》
              <template v-if="typeof item.novel_id === 'number'"> · id {{ item.novel_id }}</template>
            </p>
            <p v-else class="mt-0.5 text-xs text-red-600">{{ item.error || '未知错误' }}</p>
          </li>
        </ul>
        <button
          type="button"
          class="mt-2 text-xs text-blue-600 hover:text-blue-800"
          data-testid="upload-more"
          @click="uploadMore"
        >
          继续上传其他文件
        </button>
      </div>
    </div>
  </BaseModal>
</template>
