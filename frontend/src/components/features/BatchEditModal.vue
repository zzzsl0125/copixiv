<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi, getApiErrorMessage } from '../../api'
import { formatIdLookupHint, useIdLookup } from '../../composables'
import type {
  AuthorLookup, BatchMetadataRequest, BatchMetadataResult, BatchScope, SeriesLookup,
} from '../../types'

/**
 * 批量编辑「作者」/「系列」（批量栏「编辑」）。
 *
 * 与单本 `EditNovelModal.vue` 的分工是**刻意分开**的：那边是"这一本"的编辑器
 * （有初值、"留空 = 解除关联"、字段策略来自 `editable_fields`），这里没有初值
 * 也没有字段策略——批量里这两列的唯一规则是服务端的"只作用于本地书"，
 * 前端只把预演数字摆出来（不自己按 `source` 过滤，那是第二份真相）。
 *
 * 三条拍板决定这里的形态：
 *
 * 1. **留空 = 不改**（与单本弹窗的"留空 = 解除关联"不同）：一批书各自挂着
 *    不同的作者，空输入框不可能是它们的共同意图。要解除关联必须显式勾选
 *    输入框旁边那个 checkbox——勾上即置灰输入框、内容一律忽略，提交体里给
 *    `author_id` / `series_id` 的显式 `null`（键不出现 = 不改，`null` = 解绑）。
 * 2. **预演先行**（`apply: false`，打开弹窗时一次）：报出"选择集里多少篇本地书
 *    会被改、多少篇 pixiv 书跳过"。预演失败**不拦提交**（权威在服务端），
 *    换成一句不误导的降级文案。预演只算范围的人数，与两个字段填了什么无关，
 *    所以打开时算一次就够，改输入不重算。
 * 3. **校验只做即时反馈**：非法 ID 拦住确认按钮（红字文案与单本弹窗 / 服务端
 *    同口径），"哪些书能改"不在前端判断。
 *
 * 两个 ID 字段照旧共用 `useIdLookup`（防抖 / 过期响应丢弃 / 失败不拦提交），
 * 只是 `currentId` 恒 `null`——批量没有"这本书当前的值"，共享的 hint 函数因此
 * 自然走"新 ID"那几档语气。
 */

const props = defineProps<{
  isOpen: boolean
  scope: BatchScope | null
  scopeLabel: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'success', payload: BatchMetadataResult): void
  (e: 'error', message: string): void
}>()

const loading = ref(false)
const authorId = ref('')
const seriesId = ref('')
/** 「解除关联」勾选：勾上 = 提交该字段的显式 `null`，输入框内容一律忽略。 */
const unlinkAuthor = ref(false)
const unlinkSeries = ref(false)

const preview = ref<BatchMetadataResult | null>(null)
const previewLoading = ref(false)
const previewFailed = ref(false)
/** 过期预演响应的丢弃依据（关掉再打开、或换了范围时，慢回来的旧数不该覆盖新的）。 */
let previewSeq = 0

/**
 * 勾选「解除关联」时喂给 `formatIdLookupHint` 的 `currentId` 占位值。
 *
 * 共享函数只在「`currentId` 非空 + 输入为空」那一档返回 `unlinkNote`——它描述的
 * 正是"把一个已有的关联解除掉"。批量没有"这本书当前的值"，而这件事的凭据就是
 * 那个勾，所以勾上时给一个非 null 的占位（正整数，不会与"勾选时按空处理"的输入
 * 相遇，也就不会误落到"当前就是它"那一档）。
 */
const UNLINK_PLACEHOLDER_ID = 1

const authorLookup = useIdLookup<AuthorLookup>({
  input: authorId,
  // 批量没有"这本书当前挂的 id"：恒 null，于是 hint 走"将按此 ID 关联"那几档。
  currentId: () => null,
  fetch: (id) => novelApi.lookupAuthor(id),
  // 勾了解除关联，输入框连同解析一起作废：一个请求都不发。
  enabled: () => !unlinkAuthor.value,
})

const seriesLookup = useIdLookup<SeriesLookup>({
  input: seriesId,
  currentId: () => null,
  fetch: (id) => novelApi.lookupSeries(id),
  enabled: () => !unlinkSeries.value,
})

// 勾选框改变可用性，但输入没变 → useIdLookup 的 watch 不会触发，这里显式重评估
// （勾上 = 清掉预览结果，取消勾选 = 有值就重新查）。
watch(unlinkAuthor, () => authorLookup.schedule())
watch(unlinkSeries, () => seriesLookup.schedule())

/** 一格 hint：填了说什么、留空说什么、勾了解除关联说什么。 */
const authorHint = computed(() => formatIdLookupHint({
  entity: '作者',
  // 勾了勾选框时输入内容作废（置灰）——按"空"喂进去，才落到 unlinkNote 那一档。
  idNumber: unlinkAuthor.value ? null : authorLookup.idNumber.value,
  currentId: unlinkAuthor.value ? UNLINK_PLACEHOLDER_ID : null,
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
  emptyNote: '留空 = 不修改作者',
  // 解除关联的收尾两个字段**不同**（与单本弹窗同一口径，见后端
  // `update_metadata.py::_series_columns`）：作者保留显示名（`author_name` 上传时
  // 来自文件名解析，有独立来源），系列连显示名与章节号一起清（本地书的系列名
  // 只可能由 `series_id` 解析而来，留着就是一串清不掉的孤儿文字）。
  unlinkNote: '勾选 = 解除作者关联；显示名保留（来自文件名解析）',
}))

const seriesHint = computed(() => formatIdLookupHint({
  entity: '系列',
  idNumber: unlinkSeries.value ? null : seriesLookup.idNumber.value,
  currentId: unlinkSeries.value ? UNLINK_PLACEHOLDER_ID : null,
  currentName: '',
  loading: seriesLookup.loading.value,
  failed: seriesLookup.failed.value,
  found: seriesLookup.result.value
    ? {
        name: seriesLookup.result.value.series_name,
        novelCount: seriesLookup.result.value.novel_count,
      }
    : null,
  emptyNote: '留空 = 不修改系列',
  unlinkNote: '勾选 = 解除系列关联，并清掉显示名与章节号',
}))

/** 有东西要写吗（填了 ID 或勾了解除关联）——两个都没动就不发这次请求。 */
const hasChanges = computed(() => (
  unlinkAuthor.value || unlinkSeries.value
  || authorId.value.trim() !== '' || seriesId.value.trim() !== ''
))

/**
 * 本地校验只为即时反馈，权威在服务端：`idNumber === undefined` 即非法
 * （非数字 / `0` / 负数 / 小数 / 全角数字，见 `parseIdInput`）。
 * 勾了解除关联的字段不参与校验（它的输入框已经置灰、内容作废）。
 */
const validationError = computed<string | null>(() => {
  if (!unlinkAuthor.value && authorLookup.idNumber.value === undefined) {
    return '作者 ID 必须是正整数'
  }
  if (!unlinkSeries.value && seriesLookup.idNumber.value === undefined) {
    return '系列 ID 必须是正整数'
  }
  return null
})

const confirmDisabled = computed(() => (
  !props.scope || !hasChanges.value || !!validationError.value || loading.value
))

/**
 * 预演文案：三档——统计中 / 服务端给的数字 / 降级。
 *
 * 降级那句刻意不写"失败"以外的承诺：数字拿不到时用户仍可提交，跳过多少以
 * 服务端结果为准（真执行返回的是同一个形状，toast 也照它报数）。
 */
const previewText = computed(() => {
  if (previewLoading.value) return '统计中…'
  const result = preview.value
  if (!result) return '统计暂不可用；仍可提交，跳过数以服务端结果为准'
  return `已勾选 ${result.matched} 篇：${result.applicable} 篇本地书将被修改，`
    + `${result.skipped} 篇 pixiv 书跳过`
})

/**
 * 打开时：清空表单 → 按 `apply: false` 预演一次。
 *
 * `immediate`：宿主若一开始就带着 `isOpen=true` 挂载（测试、或将来改成条件渲染），
 * 预演同样要发出去，否则预览区永远停在降级文案上。
 */
watch(() => props.isOpen, (open) => {
  if (!open) return
  authorId.value = ''
  seriesId.value = ''
  unlinkAuthor.value = false
  unlinkSeries.value = false
  preview.value = null
  previewFailed.value = false
  // 值没变时 watch 不触发（重新打开同一批），这里显式评估一次。
  authorLookup.schedule()
  seriesLookup.schedule()
  void runPreview()
}, { immediate: true })

async function runPreview() {
  const scope = props.scope
  const seq = ++previewSeq
  preview.value = null
  previewFailed.value = false
  if (!scope) {
    previewLoading.value = false
    return
  }
  previewLoading.value = true
  try {
    const result = await novelApi.batchMetadata({ scope, apply: false })
    if (seq !== previewSeq) return
    preview.value = result
  } catch {
    // 预演失败**不**上抛 error：它是一次锦上添花的读数，不是这次操作的结果，
    // 弹窗里那句降级文案已经是全部要说的话（弹 toast 会让人以为操作失败了）。
    if (seq !== previewSeq) return
    previewFailed.value = true
  } finally {
    if (seq === previewSeq) previewLoading.value = false
  }
}

/**
 * 请求体：**只放真正要改的键**（键不出现 = 不改）——留空什么都不放，
 * 勾了解除关联放显式 `null`，填了合法 ID 放正整数。
 */
function buildPayload(apply: boolean): BatchMetadataRequest | null {
  const scope = props.scope
  if (!scope) return null
  const payload: BatchMetadataRequest = { scope, apply }
  if (unlinkAuthor.value) payload.author_id = null
  else if (authorLookup.idNumber.value != null) payload.author_id = authorLookup.idNumber.value
  if (unlinkSeries.value) payload.series_id = null
  else if (seriesLookup.idNumber.value != null) payload.series_id = seriesLookup.idNumber.value
  return payload
}

/** 确认 = 真执行（`apply: true`）。不做二级确认：弹窗本身已是那一次确认。 */
async function handleConfirm() {
  const payload = buildPayload(true)
  if (!payload || loading.value || confirmDisabled.value) return
  loading.value = true
  try {
    const result = await novelApi.batchMetadata(payload)
    emit('success', result)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '批量编辑失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="✏️ 批量编辑作者与系列"
    :loading="loading"
    confirm-text="确认修改"
    :confirm-disabled="confirmDisabled"
    @close="emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-700 space-y-3" data-testid="batch-edit-modal">
      <p>
        将修改 <b class="text-blue-600">{{ scopeLabel }}</b> 的作者与系列。
      </p>
      <!-- 预演：打开时按 apply=false 报一次数（只算不写）。它只说范围——
           "留空 = 不改"由两个字段各自的 hint 说，"勾选 = 解绑"由勾选框说。 -->
      <p
        class="text-xs rounded-md px-3 py-2 border"
        :class="previewFailed
          ? 'bg-amber-50 border-amber-200 text-amber-800'
          : 'bg-blue-50 border-blue-200 text-blue-700'"
        data-testid="batch-edit-preview"
      >
        {{ previewText }}
      </p>

      <!-- 两个字段并排（窄屏堆叠），与单本弹窗的两行同构：标签 + 输入框 + hint，
           只多一个"解除关联"勾选框——批量里它是解绑的唯一表达（留空 = 不改）。 -->
      <div class="grid gap-x-3 gap-y-4 sm:grid-cols-2">
        <div class="min-w-0">
          <label class="block text-sm font-medium text-gray-700" for="batch-edit-author">作者 ID</label>
          <input
            id="batch-edit-author"
            v-model="authorId"
            type="text"
            inputmode="numeric"
            autocomplete="off"
            placeholder="如 1234567"
            :disabled="unlinkAuthor"
            data-testid="batch-edit-author-input"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
          <label class="mt-1.5 inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
            <input
              v-model="unlinkAuthor"
              type="checkbox"
              data-testid="batch-edit-author-unlink"
              class="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            解除作者关联
          </label>
          <p
            class="mt-0.5 text-xs"
            :class="{
              'text-gray-400': authorHint.tone === 'muted',
              'text-green-600': authorHint.tone === 'ok',
              'text-amber-600': authorHint.tone === 'warn',
              'text-red-600': authorHint.tone === 'error',
            }"
            data-testid="batch-edit-author-hint"
          >
            {{ authorHint.text }}
          </p>
        </div>

        <div class="min-w-0">
          <label class="block text-sm font-medium text-gray-700" for="batch-edit-series">系列 ID</label>
          <input
            id="batch-edit-series"
            v-model="seriesId"
            type="text"
            inputmode="numeric"
            autocomplete="off"
            placeholder="如 123456"
            :disabled="unlinkSeries"
            data-testid="batch-edit-series-input"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
          />
          <label class="mt-1.5 inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
            <input
              v-model="unlinkSeries"
              type="checkbox"
              data-testid="batch-edit-series-unlink"
              class="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            解除系列关联
          </label>
          <p
            class="mt-0.5 text-xs"
            :class="{
              'text-gray-400': seriesHint.tone === 'muted',
              'text-green-600': seriesHint.tone === 'ok',
              'text-amber-600': seriesHint.tone === 'warn',
              'text-red-600': seriesHint.tone === 'error',
            }"
            data-testid="batch-edit-series-hint"
          >
            {{ seriesHint.text }}
          </p>
        </div>
      </div>

      <p v-if="validationError" class="text-xs text-red-600" data-testid="batch-edit-validation-error">
        {{ validationError }}
      </p>
    </div>
  </BaseModal>
</template>
