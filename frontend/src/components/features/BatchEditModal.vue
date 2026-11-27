<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import EntityLookupField from './EntityLookupField.vue'
import type { EntitySearchOption } from './EntityLookupField.vue'
import { getApiErrorMessage, novelApi } from '../../api'
import type { BatchMetadataRequest, BatchMetadataResult, BatchScope } from '../../types'

const props = defineProps<{
  isOpen: boolean
  scope: BatchScope | null
  scopeLabel: string
}>()

const emit = defineEmits<{
  close: []
  success: [payload: BatchMetadataResult]
  error: [message: string]
}>()

const loading = ref(false)
const authorId = ref('')
const seriesId = ref('')
const unlinkAuthor = ref(false)
const unlinkSeries = ref(false)
const authorIdNumber = ref<number | null | undefined>(null)
const seriesIdNumber = ref<number | null | undefined>(null)
const authorFieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)
const seriesFieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)

async function searchAuthors(q: string, signal?: AbortSignal): Promise<EntitySearchOption[]> {
  const items = (await novelApi.searchAuthors?.(q, { signal })) ?? []
  return items.map((item) => ({
    id: item.author_id,
    name: item.author_name ?? null,
    source: item.source,
    novelCount: item.novel_count,
  }))
}

async function searchSeries(q: string, signal?: AbortSignal): Promise<EntitySearchOption[]> {
  const items = (await novelApi.searchSeries?.(q, { signal })) ?? []
  return items.map((item) => ({
    id: item.series_id,
    name: item.series_name ?? null,
    source: item.source,
    novelCount: item.novel_count,
  }))
}

const preview = ref<BatchMetadataResult | null>(null)
const previewLoading = ref(false)
const previewFailed = ref(false)
let previewSeq = 0

const hasChanges = computed(
  () =>
    unlinkAuthor.value ||
    unlinkSeries.value ||
    authorId.value.trim() !== '' ||
    seriesId.value.trim() !== '',
)

const validationError = computed<string | null>(() => {
  if (!unlinkAuthor.value && authorIdNumber.value === undefined) {
    return '作者 ID 必须是非零整数'
  }
  if (!unlinkSeries.value && seriesIdNumber.value === undefined) {
    return '系列 ID 必须是非零整数'
  }
  return null
})

const confirmDisabled = computed(
  () =>
    !props.scope ||
    !hasChanges.value ||
    !!validationError.value ||
    loading.value ||
    preview.value?.applicable === 0,
)

const previewText = computed(() => {
  if (previewLoading.value) return '统计中…'
  const result = preview.value
  if (!result) return '统计暂不可用；仍可提交，跳过数以服务端结果为准'
  return (
    `已勾选 ${result.matched} 篇：${result.applicable} 篇可编辑书将被修改，` +
    `${result.skipped} 篇只读/不可编辑书跳过`
  )
})

watch(
  () => props.isOpen,
  (open) => {
    if (!open) return
    authorId.value = ''
    seriesId.value = ''
    unlinkAuthor.value = false
    unlinkSeries.value = false
    preview.value = null
    previewFailed.value = false
    authorFieldRef.value?.schedule()
    seriesFieldRef.value?.schedule()
    void runPreview()
  },
  { immediate: true },
)

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
    if (seq !== previewSeq) return
    previewFailed.value = true
  } finally {
    if (seq === previewSeq) previewLoading.value = false
  }
}

function buildPayload(apply: boolean): BatchMetadataRequest | null {
  const scope = props.scope
  if (!scope) return null
  const payload: BatchMetadataRequest = { scope, apply }
  if (unlinkAuthor.value) payload.author_id = null
  else if (authorIdNumber.value != null) payload.author_id = authorIdNumber.value
  if (unlinkSeries.value) payload.series_id = null
  else if (seriesIdNumber.value != null) payload.series_id = seriesIdNumber.value
  return payload
}

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
      <p
        class="text-xs rounded-md px-3 py-2 border"
        :class="
          previewFailed
            ? 'bg-amber-50 border-amber-200 text-amber-800'
            : 'bg-blue-50 border-blue-200 text-blue-700'
        "
        data-testid="batch-edit-preview"
      >
        {{ previewText }}
      </p>

      <div class="grid gap-x-3 gap-y-4 sm:grid-cols-2">
        <EntityLookupField
          ref="authorFieldRef"
          v-model="authorId"
          v-model:unlinked="unlinkAuthor"
          entity="作者"
          label="作者 ID"
          :fetch="novelApi.lookupAuthor"
          :search="searchAuthors"
          placeholder="输入名称或数字 ID"
          empty-note="留空 = 不修改作者"
          unlink-note="勾选 = 解除作者关联；显示名保留（来自文件名解析）"
          :allow-unlink="true"
          unlink-label="解除作者关联"
          input-test-id="batch-edit-author-input"
          hint-test-id="batch-edit-author-hint"
          unlink-test-id="batch-edit-author-unlink"
          @update:id-number="authorIdNumber = $event"
        />

        <EntityLookupField
          ref="seriesFieldRef"
          v-model="seriesId"
          v-model:unlinked="unlinkSeries"
          entity="系列"
          label="系列 ID"
          :fetch="novelApi.lookupSeries"
          :search="searchSeries"
          placeholder="输入名称或数字 ID"
          empty-note="留空 = 不修改系列"
          unlink-note="勾选 = 解除系列关联，并清掉显示名与章节号"
          :allow-unlink="true"
          unlink-label="解除系列关联"
          input-test-id="batch-edit-series-input"
          hint-test-id="batch-edit-series-hint"
          unlink-test-id="batch-edit-series-unlink"
          @update:id-number="seriesIdNumber = $event"
        />
      </div>

      <p
        v-if="validationError"
        class="text-xs text-red-600"
        data-testid="batch-edit-validation-error"
      >
        {{ validationError }}
      </p>
    </div>
  </BaseModal>
</template>
