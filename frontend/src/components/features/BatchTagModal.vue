<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import TagChipInput from './TagChipInput.vue'
import { BATCH_MAX_NOVELS, getApiErrorMessage, novelApi } from '../../api'
import { useTaskStatus } from '../../composables'
import type { BatchOperation, BatchScope } from '../../types'

const props = defineProps<{
  isOpen: boolean
  operation: Exclude<BatchOperation, 'delete'>
  scope: BatchScope | null
  scopeLabel: string
}>()

const emit = defineEmits<{
  close: []
  success: [payload: { matched: number; affected: number }]
  'task-submitted': [payload: { task_id: number; matched: number }]
  error: [message: string]
}>()

const loading = ref(false)
const parsedTags = ref<string[]>([])
const tagInputRef = ref<InstanceType<typeof TagChipInput> | null>(null)
const { isBusy, notifyTaskEnqueued } = useTaskStatus()

const isAdd = computed(() => props.operation === 'add_tags')
const isTaskMode = computed(() => (props.scope?.novel_ids.length ?? 0) > BATCH_MAX_NOVELS)
const title = computed(() => (isAdd.value ? '🏷️ 批量添加标签' : '🏷️ 批量移除标签'))
const confirmText = computed(() => (isAdd.value ? '确认添加' : '确认移除'))

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
  if (loading.value || isBusy('batch_operation') || !props.scope || parsedTags.value.length === 0) {
    return
  }
  loading.value = true
  try {
    const payload = {
      operation: props.operation,
      scope: props.scope,
      tags: parsedTags.value,
    }
    if (isTaskMode.value) {
      const res = await novelApi.submitBatchTask(payload)
      notifyTaskEnqueued('batch_operation')
      emit('task-submitted', res)
    } else {
      const result = await novelApi.batchOperation(payload)
      emit('success', result)
    }
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
    :confirm-disabled="parsedTags.length === 0 || isBusy('batch_operation')"
    @close="$emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p>
        将<span v-if="isAdd">为</span><span v-else>从</span>
        <b class="text-blue-600">{{ scopeLabel }}</b>
        {{ isAdd ? '添加以下标签' : '移除以下标签' }}。
      </p>
      <p
        v-if="isTaskMode"
        class="text-xs bg-blue-50 border border-blue-200 text-blue-700 rounded-md px-3 py-2"
      >
        选择超过 {{ BATCH_MAX_NOVELS }} 篇，将作为后台任务执行——提交后可关闭页面，进度可在「任务管理」页查看。
      </p>
      <TagChipInput
        ref="tagInputRef"
        v-model="parsedTags"
        label="标签"
        mode="live"
        :placeholder="isAdd ? '输入要添加的标签，用逗号或空格分隔' : '输入要移除的标签，用逗号或空格分隔'"
        helper-text="支持逗号、空格、换行分隔多个标签。"
        input-test-id="batch-tag-input"
      />
    </div>
  </BaseModal>
</template>
