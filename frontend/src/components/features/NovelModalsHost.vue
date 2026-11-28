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
  expandSeries?: boolean
  mergeSeries?: boolean
}>()

const emit = defineEmits<{
  'close-delete': []
  'close-tag': []
  'close-batch-edit': []
  'close-export': []
  'close-edit': []
  'close-upload': []
  'batch-success': [payload: { operation: string; result: BatchOperationResult | BatchMetadataResult }]
  'export-task-submitted': [payload: { task_id: number; matched: number }]
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
    :expand-series="expandSeries"
    @close="emit('close-delete')"
    @success="(payload) => emit('batch-success', { operation: 'delete', result: payload })"
    @error="emit('error', $event)"
  />
  <BatchTagModal
    :is-open="tagModalOpen"
    :operation="tagOperation"
    :scope="batchScope"
    :scope-label="scopeLabel"
    :expand-series="expandSeries"
    @close="emit('close-tag')"
    @success="(payload) => emit('batch-success', { operation: tagOperation, result: payload })"
    @error="emit('error', $event)"
  />
  <BatchEditModal
    :is-open="batchEditOpen"
    :scope="batchScope"
    :scope-label="scopeLabel"
    :expand-series="expandSeries"
    @close="emit('close-batch-edit')"
    @success="(payload) => emit('batch-success', { operation: 'update_metadata', result: payload })"
    @error="emit('error', $event)"
  />
  <BatchDownloadModal
    v-if="exportOpen"
    :is-open="exportOpen"
    :novel-ids="batchScope?.novel_ids ?? []"
    :merge-series="mergeSeries"
    @close="emit('close-export')"
    @download-error="emit('error', $event)"
    @task-submitted="emit('export-task-submitted', $event)"
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
