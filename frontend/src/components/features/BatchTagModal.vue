<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi, BATCH_MAX_NOVELS } from '../../api'
import { getApiErrorMessage } from '../../api/errors'
import type { BatchOperation, BatchScope } from '../../types'

const props = defineProps<{
  isOpen: boolean
  operation: Exclude<BatchOperation, 'delete'>
  scope: BatchScope | null
  scopeLabel: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'success', payload: { matched: number; affected: number }): void
  (e: 'task-submitted', payload: { task_id: number; matched: number }): void
  (e: 'error', message: string): void
}>()

const loading = ref(false)
const input = ref('')

const isAdd = computed(() => props.operation === 'add_tags')

/** Selections beyond the sync cap run as a background task (chunked). */
const isTaskMode = computed(
  () => (props.scope?.novel_ids.length ?? 0) > BATCH_MAX_NOVELS,
)

// Parse tags from comma / full-width comma / space / newline separated input.
const parsedTags = computed(() => {
  return Array.from(
    new Set(
      input.value
        .split(/[,，\s]+/)
        .map(t => t.trim())
        .filter(Boolean),
    ),
  )
})

const title = computed(() => (isAdd.value ? '🏷️ 批量添加标签' : '🏷️ 批量移除标签'))
const confirmText = computed(() => (isAdd.value ? '确认添加' : '确认移除'))

watch(() => props.isOpen, (open) => {
  if (open) input.value = ''
})

async function handleConfirm() {
  if (loading.value || !props.scope || parsedTags.value.length === 0) return
  loading.value = true
  try {
    const payload = {
      operation: props.operation,
      scope: props.scope,
      tags: parsedTags.value,
    }
    if (isTaskMode.value) {
      const res = await novelApi.submitBatchTask(payload)
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
    :confirm-disabled="parsedTags.length === 0"
    @close="$emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p>
        将<span v-if="isAdd">为</span><span v-else>从</span>
        <b class="text-blue-600">{{ scopeLabel }}</b>
        {{ isAdd ? '添加以下标签' : '移除以下标签' }}。
      </p>
      <p v-if="isTaskMode" class="text-xs bg-blue-50 border border-blue-200 text-blue-700 rounded-md px-3 py-2">
        选择超过 {{ BATCH_MAX_NOVELS }} 篇，将作为后台任务执行——提交后可关闭页面，进度可在「任务管理」页查看。
      </p>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">标签</label>
        <input
          v-model="input"
          type="text"
          autocomplete="off"
          :placeholder="isAdd ? '输入要添加的标签，用逗号或空格分隔' : '输入要移除的标签，用逗号或空格分隔'"
          class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          data-testid="batch-tag-input"
        />
        <p v-if="parsedTags.length > 0" class="mt-2 flex flex-wrap gap-1">
          <span
            v-for="tag in parsedTags"
            :key="tag"
            class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700"
          >
            {{ tag }}
          </span>
          <span class="text-xs text-gray-400 self-center">共 {{ parsedTags.length }} 个</span>
        </p>
        <p v-else class="mt-1 text-xs text-gray-400">支持逗号、空格、换行分隔多个标签。</p>
      </div>
    </div>
  </BaseModal>
</template>
