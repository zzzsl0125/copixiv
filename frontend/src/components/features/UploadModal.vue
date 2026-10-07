<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { X, FileUp as FileUpIcon, CheckCircle2, AlertCircle } from '@lucide/vue'
import BaseModal from '../ui/BaseModal.vue'
import { libraryApi, novelApi, getApiErrorMessage } from '../../api'
import { formatIdLookupHint, useIdLookup, useToast } from '../../composables'
import { UPLOAD_STATUS_LABEL, formatFileSize } from '../../types'
import type { AuthorLookup, UploadItemResult } from '../../types'

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

/** 「预设作者」折叠区：默认收起——多数上传按文件名解析作者就够了。 */
const presetOpen = ref(false)
/** 整批共用的作者 ID（输入框原文）；提交后**保留**，同一批书可以分多次传。 */
const presetAuthorId = ref('')

/**
 * 预设作者与编辑 / 批量编辑弹窗共用 `useIdLookup`（防抖 / 过期响应丢弃 /
 * 失败不拦上传）。`currentId` 恒 `null`：上传是**新建**，没有"这本书当前挂的值"，
 * hint 因此自然走"将按此 ID 关联 / 未查到 / ✓ 名字"那几档。
 */
const authorLookup = useIdLookup<AuthorLookup>({
  input: presetAuthorId,
  currentId: () => null,
  fetch: (id) => novelApi.lookupAuthor(id),
})

/** 解析 hint：绿＝确认、灰＝无需动作、黄＝能传但名字待补、红＝拦住上传。 */
const authorHint = computed(() => formatIdLookupHint({
  entity: '作者',
  idNumber: authorLookup.idNumber.value,
  currentId: null,
  // currentId 恒 null 时它只用于"当前就是它"那一档，这里到不了；给空串即可。
  currentName: '',
  loading: authorLookup.loading.value,
  failed: authorLookup.failed.value,
  found: authorLookup.result.value
    ? {
        name: authorLookup.result.value.author_name,
        novelCount: authorLookup.result.value.novel_count,
      }
    : null,
  emptyNote: '留空 = 按文件名解析作者',
  // 「解绑」那一档到不了（currentId 恒 null——上传是新建，没有解绑这回事），
  // 但共享函数要求给一句：这里给与留空同一句承诺，两档在上传里本来就是一件事。
  unlinkNote: '留空 = 按文件名解析作者',
}))

/** 非法 ID（非数字 / `0` / 负数 / 小数）拦上传：服务端 400 / 422，本地先给红字。 */
const presetInvalid = computed(() => authorLookup.idNumber.value === undefined)

/** 交给后端的预设作者：只认合法正整数，其余（含非法）一律当"不预设"。 */
const presetAuthor = computed(() => (
  typeof authorLookup.idNumber.value === 'number' ? authorLookup.idNumber.value : null
))

/** 折叠按钮文案：收起时若已有预设要看得见，否则一个被收起的预设会悄悄生效。 */
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

/** 上传中「已完成/总数」：已回结果的条数 / 本次提交的文件数。 */
const progressLabel = computed(() => `${results.value.length}/${batchTotal.value}`)

const reset = (options: { keepPreset?: boolean } = {}) => {
  files.value = []
  results.value = []
  uploading.value = false
  progress.value = 0
  hasUploaded.value = false
  batchTotal.value = 0
  if (fileInputRef.value) fileInputRef.value.value = ''
  // 预设作者跨「继续上传其他文件」保留（同一批书分多次传，不必重填）；
  // 重新打开弹窗则是新的一批：连同折叠态一起回到默认（要清空也由用户自己动手）。
  if (!options.keepPreset) {
    presetOpen.value = false
    presetAuthorId.value = ''
    authorLookup.schedule()
  }
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
  // 非法预设作者不发请求（服务端 400 / 422）：按钮已禁用，这里是第二道闸。
  if (uploading.value || files.value.length === 0 || presetInvalid.value) return
  const pending = [...files.value]
  uploading.value = true
  progress.value = 0
  batchTotal.value = pending.length
  try {
    const res = await libraryApi.uploadFiles(pending, (percent) => {
      progress.value = percent
    }, presetAuthor.value)
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

/** 「继续上传」：清掉结果与待上传列表，回到选择态（已入库的书已在列表里）。
 *  预设作者**不清**：同一批书分多次传是很常见的走法，不必重填。 */
const uploadMore = () => {
  reset({ keepPreset: true })
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
    :confirm-disabled="files.length === 0 || presetInvalid"
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

      <!-- 预设作者（整批共用，可选）：默认收起——多数上传按文件名解析作者就够了。
           展开后两个字段并排，与批量编辑弹窗同构（label + input + 一行 hint）；
           作者是正整数 pixiv ID，解析 hint 走共享函数（与编辑弹窗同一套语气）。
           系列**先不管**：只留一个置灰占位行，不给任何输入逻辑。 -->
      <div>
        <button
          type="button"
          class="text-xs text-blue-600 hover:text-blue-800"
          :aria-expanded="presetOpen"
          data-testid="upload-preset-toggle"
          @click="presetOpen = !presetOpen"
        >
          {{ presetToggleLabel }}
        </button>

        <div v-if="presetOpen" class="mt-2 grid gap-x-3 gap-y-2 sm:grid-cols-2">
          <div class="min-w-0">
            <label class="block text-sm font-medium text-gray-700" for="upload-preset-author">作者 ID</label>
            <input
              id="upload-preset-author"
              v-model="presetAuthorId"
              type="text"
              inputmode="numeric"
              autocomplete="off"
              placeholder="如 1234567"
              data-testid="upload-preset-author-input"
              class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
            />
            <p
              class="mt-0.5 text-xs"
              :class="{
                'text-gray-400': authorHint.tone === 'muted',
                'text-green-600': authorHint.tone === 'ok',
                'text-amber-600': authorHint.tone === 'warn',
                'text-red-600': authorHint.tone === 'error',
              }"
              data-testid="upload-preset-author-hint"
            >
              {{ authorHint.text }}
            </p>
            <p v-if="presetInvalid" class="mt-0.5 text-xs text-red-600" data-testid="upload-preset-author-error">
              作者 ID 必须是正整数
            </p>
          </div>

          <!-- 系列：后续版本支持（占位；无 v-model、无校验，就是不接逻辑） -->
          <div class="min-w-0" data-testid="upload-preset-series-disabled">
            <label class="block text-sm font-medium text-gray-400" for="upload-preset-series">系列 ID</label>
            <input
              id="upload-preset-series"
              type="text"
              disabled
              placeholder="—"
              class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
            />
            <p class="mt-0.5 text-xs text-gray-400">系列：后续版本支持</p>
          </div>
        </div>
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
            <!-- 重复：指出已有那本（标题 + id），便于过去查看；若这次预设了作者，
                 响应会把写进去的作者名回显出来（重复件也被应用了作者）。 -->
            <p v-else-if="item.status === 'duplicate'" class="mt-0.5 text-xs text-gray-500">
              已在库中：《{{ item.title || '未知标题' }}》
              <template v-if="typeof item.novel_id === 'number'"> · id {{ item.novel_id }}</template>
              <template v-if="item.author_name"> · 已挂作者：{{ item.author_name }}</template>
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
