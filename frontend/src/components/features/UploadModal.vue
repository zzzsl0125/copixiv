<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CollapsibleContent, CollapsibleRoot, CollapsibleTrigger } from 'reka-ui'
import { AlertCircle, CheckCircle2, FileUp as FileUpIcon, X } from '@lucide/vue'
import BaseModal from '../ui/BaseModal.vue'
import EntityLookupField from './EntityLookupField.vue'
import type { EntitySearchOption } from './EntityLookupField.vue'
import { getApiErrorMessage, libraryApi, novelApi } from '../../api'
import { parseIdInput, useToast } from '../../composables'
import { UPLOAD_STATUS_LABEL, formatFileSize } from '../../types'
import type { UploadItemResult } from '../../types'

const props = defineProps<{
  isOpen: boolean
}>()

const emit = defineEmits<{
  close: []
  uploaded: [summary: { created: number; duplicate: number; failed: number }]
  error: [message: string]
}>()

const toast = useToast()

const fileInputRef = ref<HTMLInputElement | null>(null)
const files = ref<File[]>([])
const results = ref<UploadItemResult[]>([])
const uploading = ref(false)
const progress = ref(0)
const hasUploaded = ref(false)
const batchTotal = ref(0)

const presetOpen = ref(false)
const presetAuthorId = ref('')
const presetFieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)

async function searchAuthors(q: string, signal?: AbortSignal): Promise<EntitySearchOption[]> {
  const items = (await novelApi.searchAuthors?.(q, { signal })) ?? []
  return items.map((item) => ({
    id: item.author_id,
    name: item.author_name ?? null,
    source: item.source,
    novelCount: item.novel_count,
  }))
}

const presetIdNumber = computed(() => parseIdInput(presetAuthorId.value))
const presetInvalid = computed(() => presetIdNumber.value === undefined)
const presetAuthor = computed(() =>
  typeof presetIdNumber.value === 'number' ? presetIdNumber.value : null,
)

const presetToggleLabel = computed(() => {
  if (presetOpen.value) return '− 预设作者'
  const preset = presetAuthorId.value.trim()
  return preset ? `预设作者：${preset}` : '+ 预设作者'
})

const summary = computed(() => ({
  created: results.value.filter((r) => r.status === 'created').length,
  duplicate: results.value.filter((r) => r.status === 'duplicate').length,
  failed: results.value.filter((r) => r.status === 'failed').length,
}))

const progressLabel = computed(() => `${batchTotal.value} 个文件`)

const reset = (options: { keepPreset?: boolean } = {}) => {
  files.value = []
  results.value = []
  uploading.value = false
  progress.value = 0
  hasUploaded.value = false
  batchTotal.value = 0
  if (fileInputRef.value) fileInputRef.value.value = ''
  if (!options.keepPreset) {
    presetOpen.value = false
    presetAuthorId.value = ''
    presetFieldRef.value?.schedule()
  }
}

watch(
  () => props.isOpen,
  (open) => {
    if (open) reset()
  },
)

const openPicker = () => fileInputRef.value?.click()

const handleFilePick = (event: Event) => {
  const input = event.target as HTMLInputElement
  const picked = Array.from(input.files || [])
  const known = new Set(files.value.map((f) => `${f.name}:${f.size}`))
  for (const file of picked) {
    const key = `${file.name}:${file.size}`
    if (known.has(key)) continue
    known.add(key)
    files.value.push(file)
  }
  input.value = ''
}

const removeFile = (index: number) => {
  if (uploading.value) return
  files.value.splice(index, 1)
}

const handleUpload = async () => {
  if (uploading.value || files.value.length === 0 || presetInvalid.value) return
  const pending = [...files.value]
  uploading.value = true
  progress.value = 0
  batchTotal.value = pending.length
  try {
    const res = await libraryApi.uploadFiles(
      pending,
      (percent) => {
        progress.value = percent
      },
      presetAuthor.value,
    )
    const batch = res.results || []
    results.value = [...results.value, ...batch]
    const answered = new Set(batch.map((r) => r.filename))
    files.value = pending.filter((f) => !answered.has(f.name))
    hasUploaded.value = true

    const counts = {
      created: res.created,
      duplicate: res.duplicate,
      failed: res.failed,
    }
    const attached = batch.filter((r) => r.epub_attached).length
    if (counts.created > 0) {
      toast.success(`已入库 ${counts.created} 本`)
    }
    if (counts.duplicate > 0) {
      toast.info(
        attached > 0
          ? `${counts.duplicate} 个文件已在库中，其中 ${attached} 个补挂了 EPUB`
          : `${counts.duplicate} 个文件已在库中（同一份正文）`,
      )
    }
    if (counts.failed > 0) toast.warning(`${counts.failed} 个文件上传失败，详见结果列表`)
    if (counts.created > 0 || attached > 0 || (presetAuthor.value !== null && counts.duplicate > 0)) {
      emit('uploaded', counts)
    }
    if (counts.created === 0 && counts.duplicate === 0 && counts.failed === 0) {
      toast.warning('后端没有返回任何上传结果')
    }
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '上传失败'))
  } finally {
    uploading.value = false
  }
}

const uploadMore = () => {
  reset({ keepPreset: true })
  openPicker()
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="📚 上传本地书籍"
    confirm-text="上传"
    :cancel-text="hasUploaded ? '完成' : '取消'"
    :loading="uploading"
    :confirm-disabled="files.length === 0 || presetInvalid"
    @close="$emit('close')"
    @confirm="handleUpload"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p class="text-xs text-gray-500">
        支持多选，收 <b>.txt</b> 与 <b>.epub</b>。TXT 编码自动识别
        （UTF-8 / GB18030 / Big5 / UTF-16）；EPUB 自动提取正文与标题 / 作者，
        <b>原件一并保留</b>。文件会复制入库，正文相同的书不会重复收录。
      </p>

      <input
        ref="fileInputRef"
        type="file"
        accept=".txt,.epub"
        multiple
        class="hidden"
        data-testid="upload-file-input"
        @change="handleFilePick"
      />

      <div class="flex gap-2">
        <button
          type="button"
          class="inline-flex items-center px-3 py-2 text-sm font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded-md transition-colors cursor-pointer"
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

      <CollapsibleRoot v-model:open="presetOpen" :unmount-on-hide="true">
        <CollapsibleTrigger
          type="button"
          class="text-xs text-blue-600 hover:text-blue-800 cursor-pointer"
          data-testid="upload-preset-toggle"
        >
          {{ presetToggleLabel }}
        </CollapsibleTrigger>

        <CollapsibleContent class="mt-2">
          <EntityLookupField
            ref="presetFieldRef"
            v-model="presetAuthorId"
            entity="作者"
            label="作者 ID"
            :fetch="novelApi.lookupAuthor"
            :search="searchAuthors"
            placeholder="输入名称或数字 ID"
            empty-note="留空 = 按文件名解析作者"
            unlink-note="留空 = 按文件名解析作者"
            input-test-id="upload-preset-author-input"
            hint-test-id="upload-preset-author-hint"
          />
        </CollapsibleContent>
      </CollapsibleRoot>

      <div v-if="uploading" data-testid="upload-progress">
        <div class="flex justify-between text-xs text-gray-600 mb-1">
          <span>上传中…</span>
          <span>{{ progressLabel }} · {{ progress }}%</span>
        </div>
        <div class="w-full h-1.5 bg-gray-100 rounded">
          <div class="h-1.5 bg-blue-500 rounded transition-all" :style="{ width: `${progress}%` }"></div>
        </div>
      </div>

      <ul
        v-if="files.length > 0"
        class="divide-y divide-gray-100 border border-gray-200 rounded-md"
        data-testid="upload-file-list"
      >
        <li
          v-for="(file, index) in files"
          :key="`${file.name}:${file.size}`"
          class="flex items-center justify-between px-3 py-2"
        >
          <span class="truncate mr-2" :title="file.name">{{ file.name }}</span>
          <span class="flex items-center gap-2 shrink-0">
            <span class="text-xs text-gray-400">{{ formatFileSize(file.size) }}</span>
            <button
              type="button"
              class="text-gray-400 hover:text-red-500 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
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

      <div v-if="results.length > 0" data-testid="upload-result-list">
        <div class="flex items-center justify-between mb-1">
          <span class="font-medium text-gray-700">上传结果</span>
          <span class="text-xs" data-testid="upload-summary">
            成功 {{ summary.created }} / 重复 {{ summary.duplicate }} / 失败 {{ summary.failed }}
          </span>
        </div>
        <ul class="divide-y divide-gray-100 border border-gray-200 rounded-md max-h-56 overflow-y-auto">
          <li
            v-for="(item, index) in results"
            :key="`${item.filename}:${index}`"
            class="px-3 py-2"
          >
            <div class="flex items-start justify-between gap-2">
              <span class="truncate" :title="item.filename">{{ item.filename }}</span>
              <span
                class="shrink-0 inline-flex items-center gap-1 text-xs font-medium"
                :class="
                  item.status === 'created'
                    ? 'text-green-600'
                    : item.status === 'duplicate'
                      ? 'text-amber-600'
                      : 'text-red-600'
                "
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
            <p v-else-if="item.status === 'duplicate'" class="mt-0.5 text-xs text-gray-500">
              已在库中：《{{ item.title || '未知标题' }}》
              <template v-if="typeof item.novel_id === 'number'"> · id {{ item.novel_id }}</template>
              <template v-if="item.author_name"> · 已挂作者：{{ item.author_name }}</template>
              <template v-if="item.epub_attached"> · 已补挂 EPUB</template>
            </p>
            <p v-else class="mt-0.5 text-xs text-red-600">{{ item.error || '未知错误' }}</p>
          </li>
        </ul>
        <button
          type="button"
          class="mt-2 text-xs text-blue-600 hover:text-blue-800 cursor-pointer"
          data-testid="upload-more"
          @click="uploadMore"
        >
          继续上传其他文件
        </button>
      </div>
    </div>
  </BaseModal>
</template>
