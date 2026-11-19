<script setup lang="ts">
import BatchDeleteModal from './BatchDeleteModal.vue'
import BatchTagModal from './BatchTagModal.vue'
import BatchEditModal from './BatchEditModal.vue'
import BatchDownloadModal from './BatchDownloadModal.vue'
import EditNovelModal from './EditNovelModal.vue'
import UploadModal from './UploadModal.vue'
import type {
  BatchMetadataResult,
  BatchOperationResult,
  BatchScope,
  Novel,
  NovelFilters,
} from '../../types'

defineProps<{
  deleteOpen: boolean
  tagModalOpen: boolean
  tagOperation: 'add_tags' | 'remove_tags'
  batchEditOpen: boolean
  exportOpen: boolean
  editingNovel: Novel | null
  uploadOpen: boolean
  batchScope: BatchScope | null
  scopeLabel: string
  filters: NovelFilters
}>()

const emit = defineEmits<{
  'close-delete': []
  'close-tag': []
  'close-batch-edit': []
  'close-export': []
  'close-edit': []
  'close-upload': []
  'batch-success': [payload: { operation: string; result: BatchOperationResult | BatchMetadataResult }]
  'batch-task-submitted': [payload: { operation: string; task_id: number; matched: number }]
  'download-success': [filename: string]
  'novel-updated': [novel: Novel]
  'novel-deleted': [novelId: number]
  uploaded: [summary: { created: number; duplicate: number; failed: number }]
  error: [message: string]
}>()
</script>

<template>
  <BatchDeleteModal
    :is-open="deleteOpen"
    :scope="batchScope"
    :scope-label="scopeLabel"
    @close="emit('close-delete')"
    @success="(payload) => emit('batch-success', { operation: 'delete', result: payload })"
    @task-submitted="(payload) => emit('batch-task-submitted', { operation: 'delete', ...payload })"
    @error="emit('error', $event)"
  />
  <BatchTagModal
    :is-open="tagModalOpen"
    :operation="tagOperation"
    :scope="batchScope"
    :scope-label="scopeLabel"
    @close="emit('close-tag')"
    @success="(payload) => emit('batch-success', { operation: tagOperation, result: payload })"
    @task-submitted="(payload) => emit('batch-task-submitted', { operation: tagOperation, ...payload })"
    @error="emit('error', $event)"
  />
  <BatchEditModal
    :is-open="batchEditOpen"
    :scope="batchScope"
    :scope-label="scopeLabel"
    @close="emit('close-batch-edit')"
    @success="(payload) => emit('batch-success', { operation: 'update_metadata', result: payload })"
    @error="emit('error', $event)"
  />
  <BatchDownloadModal
    :is-open="exportOpen"
    :keyword="filters.keyword"
    :order_by="filters.order_by"
    :order_direction="filters.order_direction"
    :min_like="filters.min_like"
    :min_text="filters.min_text"
    :novel-ids="batchScope?.novel_ids"
    @close="emit('close-export')"
    @download-success="emit('download-success', $event)"
    @download-error="emit('error', $event)"
    @task-submitted="(payload) => emit('batch-task-submitted', { operation: 'export', ...payload })"
  />
  <EditNovelModal
    :is-open="editingNovel !== null"
    :novel="editingNovel"
    @close="emit('close-edit')"
    @updated="emit('novel-updated', $event)"
    @deleted="emit('novel-deleted', $event)"
    @error="emit('error', $event)"
  />
  <UploadModal
    :is-open="uploadOpen"
    @close="emit('close-upload')"
    @uploaded="emit('uploaded', $event)"
    @error="emit('error', $event)"
  />
</template>
