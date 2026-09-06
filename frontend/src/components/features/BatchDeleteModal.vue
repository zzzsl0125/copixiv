<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import { novelApi, BATCH_MAX_NOVELS, getApiErrorMessage } from '../../api'
import type { BatchScope } from '../../types'

const CONFIRM_WORD = 'DELETE'

const props = defineProps<{
  isOpen: boolean
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
const confirmInput = ref('')

const confirmReady = computed(() => confirmInput.value === CONFIRM_WORD)

/** Selections beyond the sync cap run as a background task (chunked). */
const isTaskMode = computed(
  () => (props.scope?.novel_ids.length ?? 0) > BATCH_MAX_NOVELS,
)

watch(() => props.isOpen, (open) => {
  if (open) confirmInput.value = ''
})

async function handleConfirm() {
  if (loading.value || !props.scope || !confirmReady.value) return
  loading.value = true
  try {
    if (isTaskMode.value) {
      const res = await novelApi.submitBatchTask({
        operation: 'delete',
        scope: props.scope,
      })
      emit('task-submitted', res)
    } else {
      const result = await novelApi.batchOperation({
        operation: 'delete',
        scope: props.scope,
      })
      emit('success', result)
    }
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '批量删除失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="🗑️ 批量删除"
    :loading="loading"
    confirm-text="确认删除"
    :confirm-disabled="!confirmReady"
    @close="$emit('close')"
    @confirm="handleConfirm"
  >
    <div class="text-sm text-gray-700 space-y-3">
      <p>
        将删除 <b class="text-red-600">{{ scopeLabel }}</b>，共
        <b class="text-red-600">影响小说及其本地文件</b>。
      </p>
      <p class="text-xs text-gray-500">删除操作不可撤销：小说记录、txt/epub 文件都会被移除。</p>
      <p v-if="isTaskMode" class="text-xs bg-blue-50 border border-blue-200 text-blue-700 rounded-md px-3 py-2">
        选择超过 {{ BATCH_MAX_NOVELS }} 篇，将作为后台任务执行——提交后可关闭页面，进度可在「任务管理」页查看。
      </p>
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">
          输入 <code class="bg-gray-100 px-1.5 py-0.5 rounded font-mono">{{ CONFIRM_WORD }}</code> 以确认删除
        </label>
        <input
          v-model="confirmInput"
          type="text"
          autocomplete="off"
          :placeholder="`请输入 ${CONFIRM_WORD}`"
          class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-red-500 focus:border-red-500 sm:text-sm font-mono"
          data-testid="delete-confirm-input"
        />
      </div>
    </div>
  </BaseModal>
</template>
