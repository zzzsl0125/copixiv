<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '../ui/BaseModal.vue'
import FormField from '../ui/FormField.vue'
import EntityLookupField from './EntityLookupField.vue'
import type { EntitySearchOption } from './EntityLookupField.vue'
import TagChipInput from './TagChipInput.vue'
import { getApiErrorMessage, novelApi } from '../../api'
import { useConfirm } from '../../composables'
import { HINT_TONE_CLASSES } from '../../composables/useIdLookup'
import type { Novel, NovelUpdatePayload } from '../../types'

const props = defineProps<{
  isOpen: boolean
  novel: Novel | null
}>()

const emit = defineEmits<{
  close: []
  updated: [novel: Novel]
  deleted: [novelId: number]
  error: [message: string]
}>()

const { confirm } = useConfirm()

const MAX_TITLE = 300
const MAX_TAGS = 50
const MAX_TAG_LENGTH = 100

const loading = ref(false)
const syncing = ref(false)
const deleting = ref(false)
const title = ref('')
const authorId = ref('')
const seriesId = ref('')
const seriesIndex = ref('')
const tags = ref<string[]>([])
const createTime = ref('')

const authorIdNumber = ref<number | null | undefined>(null)
const seriesIdNumber = ref<number | null | undefined>(null)
const authorFieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)
const seriesFieldRef = ref<InstanceType<typeof EntityLookupField> | null>(null)
const tagInputRef = ref<InstanceType<typeof TagChipInput> | null>(null)

const isLocal = computed(() => props.novel?.source === 'local')
const canSync = computed(() => Boolean(props.novel?.can_sync))

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

function canEdit(field: keyof NovelUpdatePayload): boolean {
  return props.novel?.editable_fields?.includes(field) ?? false
}

const indexNumber = computed<number | null | undefined>(() => {
  const raw = seriesIndex.value.trim()
  if (!raw) return null
  if (!/^\d+$/.test(raw)) return undefined
  const parsed = Number(raw)
  return parsed > 0 ? parsed : undefined
})

function loadFrom(novel: Novel | null) {
  title.value = novel?.title ?? ''
  authorId.value = novel?.author_id != null ? String(novel.author_id) : ''
  seriesId.value = novel?.series_id != null ? String(novel.series_id) : ''
  seriesIndex.value = novel?.series_index != null ? String(novel.series_index) : ''
  tags.value = [...(novel?.tags ?? [])]
  createTime.value = (novel?.create_time ?? '').slice(0, 10)
  tagInputRef.value?.reset()
  authorFieldRef.value?.schedule()
  seriesFieldRef.value?.schedule()
}

watch(
  () => props.isOpen,
  (open) => {
    if (open) loadFrom(props.novel)
  },
  { immediate: true },
)

const authorUnlinkNote = computed(
  () => `留空 = 解除作者关联；显示名保留「${props.novel?.author_name || '未知'}」`,
)
const seriesUnlinkNote = computed(
  () =>
    `留空 = 解除系列关联，并清掉显示名「${props.novel?.series_name || '未知'}」与章节号`,
)

const validationError = computed<string | null>(() => {
  if (canEdit('title')) {
    if (!title.value.trim()) return '标题不能为空'
    if (title.value.trim().length > MAX_TITLE) return `标题过长（最多 ${MAX_TITLE} 字）`
  }
  if (canEdit('series_id') && seriesIdNumber.value === undefined) {
    return '系列 ID 必须是非零整数'
  }
  if (canEdit('author_id') && authorIdNumber.value === undefined) {
    return '作者 ID 必须是非零整数'
  }
  if (canEdit('series_index') && indexNumber.value === undefined) {
    return '章节号必须是正整数'
  }
  if (canEdit('tags') && tags.value.length > MAX_TAGS) return `标签过多（最多 ${MAX_TAGS} 个）`
  if (canEdit('tags') && tags.value.some((tag) => tag.length > MAX_TAG_LENGTH)) {
    return `单个标签最多 ${MAX_TAG_LENGTH} 字`
  }
  return null
})

function buildPayload(): NovelUpdatePayload {
  const novel = props.novel
  const payload: NovelUpdatePayload = {}
  if (!novel) return payload

  if (canEdit('title')) {
    const nextTitle = title.value.trim()
    if (nextTitle !== novel.title) payload.title = nextTitle
  }

  if (canEdit('series_id')) {
    const nextSeriesId = seriesIdNumber.value ?? null
    if (nextSeriesId !== (novel.series_id ?? null)) payload.series_id = nextSeriesId
  }

  if (canEdit('author_id')) {
    const nextAuthorId = authorIdNumber.value ?? null
    if (nextAuthorId !== (novel.author_id ?? null)) payload.author_id = nextAuthorId
  }

  if (canEdit('series_index')) {
    const nextIndex = indexNumber.value ?? null
    if (nextIndex !== (novel.series_index ?? null)) payload.series_index = nextIndex
  }

  if (canEdit('tags')) {
    const nextTags = [...tags.value].sort()
    const prevTags = [...(novel.tags ?? [])].sort()
    if (nextTags.join('\u0000') !== prevTags.join('\u0000')) payload.tags = tags.value
  }

  if (canEdit('create_time')) {
    const nextDate = createTime.value.trim()
    if (nextDate !== (novel.create_time ?? '').slice(0, 10)) {
      payload.create_time = nextDate || null
    }
  }

  return payload
}

const hasChanges = computed(
  () => Object.keys(buildPayload()).length > 0 || (tagInputRef.value?.hasDraft ?? false),
)
const confirmDisabled = computed(
  () => !!validationError.value || !hasChanges.value || syncing.value || deleting.value,
)

async function handleConfirm() {
  const novel = props.novel
  if (!novel || loading.value || syncing.value || deleting.value) return
  tagInputRef.value?.flush()
  if (confirmDisabled.value) return

  loading.value = true
  try {
    const updated = await novelApi.updateNovel(novel.id, buildPayload())
    emit('updated', updated)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '保存失败'))
  } finally {
    loading.value = false
  }
}

async function handleSync() {
  const novel = props.novel
  if (!novel || !canSync.value || syncing.value || loading.value || deleting.value) return

  syncing.value = true
  try {
    const updated = await novelApi.syncNovel(novel.id)
    loadFrom(updated)
    emit('updated', updated)
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '同步失败'))
  } finally {
    syncing.value = false
  }
}

async function handleDelete() {
  const novel = props.novel
  if (!novel || deleting.value || loading.value || syncing.value) return

  const confirmed = await confirm({
    title: '删除小说',
    description: `确定要删除小说《${novel.title}》吗？\n删除操作不可撤销：小说记录与本地文件都将被移除。`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return

  deleting.value = true
  try {
    await novelApi.deleteNovel(novel.id)
    emit('deleted', novel.id)
    emit('close')
  } catch (err: unknown) {
    emit('error', getApiErrorMessage(err, '删除失败'))
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    title="编辑书籍信息"
    :loading="loading"
    confirm-text="保存"
    :confirm-disabled="confirmDisabled"
    @close="emit('close')"
    @confirm="handleConfirm"
  >
    <div v-if="novel" class="space-y-4 text-sm text-gray-700">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
        <span
          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold"
          :class="isLocal ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-700'"
          data-testid="edit-source-badge"
        >
          {{ isLocal ? '本地' : novel.source }}
        </span>
        <span>#{{ novel.id }}</span>
        <span title="喜爱">❤️ {{ novel.like }}</span>
        <span title="浏览">👁 {{ novel.view }}</span>
        <span title="字数">📝 {{ novel.text }}</span>
      </div>

      <div class="flex gap-3">
        <FormField label="标题" class="flex-1 min-w-0">
          <template #default="{ id }">
            <input
              :id="id"
              v-model="title"
              type="text"
              :disabled="!canEdit('title')"
              data-testid="edit-title"
              class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
            />
          </template>
        </FormField>

        <EntityLookupField
          ref="authorFieldRef"
          v-model="authorId"
          entity="作者"
          label="作者 ID"
          :fetch="novelApi.lookupAuthor"
          :search="searchAuthors"
          :current-id="novel.author_id ?? null"
          :current-name="novel.author_name || '未知'"
          :disabled="!canEdit('author_id')"
          :show-hint="false"
          placeholder="输入名称或数字 ID"
          empty-note="留空 = 不改作者（名字仍来自文件名解析）"
          :unlink-note="authorUnlinkNote"
          input-test-id="edit-author-id"
          class="w-44 shrink-0"
          @update:id-number="authorIdNumber = $event"
        />
      </div>
      <p
        v-if="canEdit('author_id') && authorFieldRef?.hint?.text"
        class="-mt-2 text-xs"
        :class="HINT_TONE_CLASSES[authorFieldRef.hint.tone]"
        data-testid="edit-author-hint"
      >
        {{ authorFieldRef.hint.text }}
      </p>

      <div class="flex gap-3">
        <EntityLookupField
          ref="seriesFieldRef"
          v-model="seriesId"
          entity="系列"
          label="系列 ID"
          :fetch="novelApi.lookupSeries"
          :search="searchSeries"
          :current-id="novel.series_id ?? null"
          :current-name="novel.series_name || '未知'"
          :disabled="!canEdit('series_id')"
          :show-hint="canEdit('series_id')"
          placeholder="输入名称或数字 ID"
          empty-note="留空 = 不改系列"
          :unlink-note="seriesUnlinkNote"
          input-test-id="edit-series-id"
          hint-test-id="edit-series-hint"
          class="flex-1 min-w-0"
          @update:id-number="seriesIdNumber = $event"
        />

        <FormField label="章节号" class="w-24 shrink-0">
          <template #default="{ id }">
            <input
              :id="id"
              v-model="seriesIndex"
              type="text"
              inputmode="numeric"
              placeholder="—"
              data-testid="edit-series-index"
              class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
            />
          </template>
        </FormField>
      </div>

      <TagChipInput
        ref="tagInputRef"
        v-model="tags"
        label="标签"
        mode="chips"
        placeholder="输入标签后回车添加（可用逗号、空格、/、# 等分隔多个）"
        input-test-id="edit-tag-input"
        tags-test-id="edit-tags"
        tag-remove-test-id-prefix="edit-tag-remove-"
      />

      <FormField label="发布日期">
        <template #default="{ id }">
          <input
            :id="id"
            v-model="createTime"
            type="date"
            data-testid="edit-date"
            class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          />
        </template>
      </FormField>

      <p v-if="validationError" class="text-xs text-red-600" data-testid="edit-validation-error">
        {{ validationError }}
      </p>
    </div>

    <template #extra-buttons>
      <button
        v-if="canSync"
        type="button"
        :disabled="syncing || loading || deleting"
        data-testid="edit-sync"
        class="mt-3 w-full inline-flex justify-center items-center gap-1 rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors cursor-pointer"
        @click="handleSync"
      >
        <span
          v-if="syncing"
          class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600"
          aria-hidden="true"
        ></span>
        {{ syncing ? '同步中…' : '同步' }}
      </button>

      <button
        type="button"
        :disabled="deleting || loading || syncing"
        data-testid="edit-delete"
        class="mt-3 w-full inline-flex justify-center items-center gap-1 rounded-md border border-red-200 shadow-sm px-4 py-2 bg-red-50 text-base font-medium text-red-600 hover:bg-red-100 hover:text-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500 sm:mt-0 sm:w-auto sm:text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors cursor-pointer"
        @click="handleDelete"
      >
        <span
          v-if="deleting"
          class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-red-300 border-t-red-600"
          aria-hidden="true"
        ></span>
        {{ deleting ? '删除中…' : '删除' }}
      </button>
    </template>
  </BaseModal>
</template>
