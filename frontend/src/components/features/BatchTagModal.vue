<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import TagChipInput from './TagChipInput.vue'
import { getApiErrorMessage, novelApi } from '../../api'
import type { BatchOperation, BatchScope } from '../../types'

const props = defineProps<{
  isOpen: boolean
  operation: Exclude<BatchOperation, 'delete'>
  scope: BatchScope | null
  scopeLabel: string
  expandSeries?: boolean
}>()

const emit = defineEmits<{
  close: []
  success: [payload: { matched: number; affected: number }]
  error: [message: string]
}>()

const BATCH_MAX_TAGS = 20

const loading = ref(false)
const parsedTags = ref<string[]>([])
const tagInputRef = ref<InstanceType<typeof TagChipInput> | null>(null)

const isAdd = computed(() => props.operation === 'add_tags')
const title = computed(() => (isAdd.value ? '🏷️ 批量添加标签' : '🏷️ 批量移除标签'))
const confirmText = computed(() => (isAdd.value ? '确认添加' : '确认移除'))
const validationError = computed<string | null>(() =>
  parsedTags.value.length > BATCH_MAX_TAGS
    ? `一次最多操作 ${BATCH_MAX_TAGS} 个标签（当前 ${parsedTags.value.length} 个）`
    : null,
)

watch(
  () => props.isOpen,
  (open) => {
    if (open) {
      parsedTags.value = []
      tagInputRef.value?.reset()
    }
  },
)

async function handleConfirm() {
  if (
    loading.value ||
    !props.scope ||
    parsedTags.value.length === 0 ||
    parsedTags.value.length > BATCH_MAX_TAGS
  ) {
    return
  }
  loading.value = true
  try {
    const result = await novelApi.batchOperation({
      operation: props.operation,
      scope: props.scope,
      tags: parsedTags.value,
      ...(props.expandSeries !== undefined ? { expand_series: props.expandSeries } : {}),
    })
    emit('success', result)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '批量标签操作失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    :title="title"
    :loading="loading"
    :confirm-text="confirmText"
    :confirm-disabled="parsedTags.length === 0 || parsedTags.length > BATCH_MAX_TAGS"
    @close="$emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p>
        将<span v-if="isAdd">为</span><span v-else>从</span>
        <b class="text-blue-600">{{ scopeLabel }}</b>
        {{ isAdd ? '添加以下标签' : '移除以下标签' }}。
      </p>
      <TagChipInput
        ref="tagInputRef"
        v-model="parsedTags"
        label="标签"
        mode="live"
        :placeholder="isAdd ? '输入要添加的标签，可用逗号、空格、/、# 分隔' : '输入要移除的标签，可用逗号、空格、/、# 分隔'"
        :helper-text="`支持逗号、空格、/、#、换行等分隔多个标签，单次最多 ${BATCH_MAX_TAGS} 个。`"
        input-test-id="batch-tag-input"
      />
      <p v-if="validationError" class="text-xs text-red-600" data-testid="batch-tag-error">
        {{ validationError }}
      </p>
    </div>
  </BaseModal>
</template>
