<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { Sparkles } from '@lucide/vue'
import { getApiErrorMessage, queryKeys, tagAliasApi, tagPreferenceApi } from '../api'
import { useConfirm, useSystem, useToast } from '../composables'
import type { TagAlias, TagPreference } from '../types'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import DraggableTable, { type TableColumn } from '../components/features/DraggableTable.vue'
import BaseModal from '../components/ui/BaseModal.vue'
import AppInput from '../components/ui/AppInput.vue'
import AppSwitch from '../components/ui/AppSwitch.vue'
import AliasSuggestModal from '../components/features/AliasSuggestModal.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const toast = useToast()
const { confirm } = useConfirm()
const queryClient = useQueryClient()
const { systemConfig, updateConfig, isUpdatingConfig } = useSystem()

const excludeBlocked = computed(() => systemConfig.value?.exclude_blocked_tag_novels ?? true)

async function toggleExclude(nextValue: boolean) {
  if (isUpdatingConfig.value) return
  try {
    const updated = await updateConfig({
      exclude_blocked_tag_novels: nextValue,
    })
    toast.success(
      updated.exclude_blocked_tag_novels
        ? '已开启：带厌恶标签的小说将从列表中排除'
        : '已关闭：带厌恶标签的小说将正常显示',
    )
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '设置保存失败'))
  }
}

const activeTab = ref<'favourite' | 'blocked' | 'alias'>('favourite')
const showModal = ref(false)
const showSuggestModal = ref(false)
const saving = ref(false)
const currentTag = ref('')
const aliasSource = ref('')
const aliasTarget = ref('')
const isEditingAlias = ref(false)
const editingAliasOriginalTarget = ref('')

const tagPrefsQuery = useQuery({
  queryKey: queryKeys.tagPreferences(),
  queryFn: ({ signal }) => tagPreferenceApi.getTagPreferences(signal),
})

const tagAliasesQuery = useQuery({
  queryKey: queryKeys.tagAliases(),
  queryFn: ({ signal }) => tagAliasApi.getTagAliases(signal),
  enabled: computed(() => activeTab.value === 'alias'),
})

watch(
  [() => tagPrefsQuery.error.value, () => tagAliasesQuery.error.value],
  ([prefErr, aliasErr]) => {
    const err = activeTab.value === 'alias' ? aliasErr : prefErr
    if (err) toast.error(getApiErrorMessage(err, '标签/别名加载失败'))
  },
)

const allTags = computed<TagPreference[]>(() => tagPrefsQuery.data.value ?? [])
const allAliases = computed<TagAlias[]>(() => tagAliasesQuery.data.value ?? [])
const loading = computed(() =>
  activeTab.value === 'alias'
    ? tagAliasesQuery.isPending.value
    : tagPrefsQuery.isPending.value,
)
const refreshing = computed(() =>
  activeTab.value === 'alias'
    ? tagAliasesQuery.isFetching.value
    : tagPrefsQuery.isFetching.value,
)

interface GroupedAlias {
  target: string
  sources: string[]
  aliases: TagAlias[]
}

const groupedAliases = computed<GroupedAlias[]>(() => {
  const map = new Map<string, TagAlias[]>()
  for (const alias of allAliases.value) {
    if (!map.has(alias.target)) map.set(alias.target, [])
    map.get(alias.target)!.push(alias)
  }
  return Array.from(map.entries()).map(([target, aliases]) => ({
    target,
    sources: aliases.map((a) => a.source),
    aliases,
  }))
})

const filteredTags = computed(() =>
  allTags.value
    .filter((t) => t.preference === activeTab.value)
    .sort((a, b) => a.sort_index - b.sort_index),
)

async function fetchData() {
  if (activeTab.value === 'alias') {
    await tagAliasesQuery.refetch()
  } else {
    await tagPrefsQuery.refetch()
  }
}

async function invalidateAfterTagChange({
  prefs = false,
  aliases = false,
  novels = false,
}: {
  prefs?: boolean
  aliases?: boolean
  novels?: boolean
} = {}) {
  const tasks: Promise<void>[] = []
  if (prefs) tasks.push(queryClient.invalidateQueries({ queryKey: queryKeys.tagPreferences() }))
  if (aliases) tasks.push(queryClient.invalidateQueries({ queryKey: queryKeys.tagAliases() }))
  if (novels) tasks.push(queryClient.invalidateQueries({ queryKey: queryKeys.novelsAll() }))
  await Promise.all(tasks)
}

async function saveTag() {
  if (saving.value) return
  if (activeTab.value === 'alias') {
    if (!aliasSource.value.trim() || !aliasTarget.value.trim()) return
    saving.value = true
    try {
      const newSources = aliasSource.value
        .split(/[,，\n]+/)
        .map((s) => s.trim())
        .filter((s) => s)
      const target = aliasTarget.value.trim()
      if (isEditingAlias.value) {
        const existingGroup = groupedAliases.value.find(
          (g) => g.target === editingAliasOriginalTarget.value,
        )
        if (existingGroup) {
          await tagAliasApi.replaceTagAliasGroup(
            editingAliasOriginalTarget.value,
            target,
            newSources,
          )
        }
      } else {
        await tagAliasApi.createTagAliasesBatch(newSources, target)
      }
      await invalidateAfterTagChange({ prefs: true, aliases: true, novels: true })
      closeModal()
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '保存失败'))
    } finally {
      saving.value = false
    }
  } else {
    if (!currentTag.value.trim()) return
    saving.value = true
    const pref = activeTab.value as 'favourite' | 'blocked'
    try {
      await tagPreferenceApi.setTagPreference(currentTag.value.trim(), pref)
      await invalidateAfterTagChange({ prefs: true, novels: pref === 'blocked' })
      closeModal()
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '添加失败'))
    } finally {
      saving.value = false
    }
  }
}

async function removeTag(tag: TagPreference) {
  const confirmed = await confirm({
    title: '删除标签',
    description: `确定要删除标签 "${tag.tag}" 吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await tagPreferenceApi.deleteTagPreference(tag.id)
    await invalidateAfterTagChange({ prefs: true, novels: tag.preference === 'blocked' })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除失败'))
  }
}

async function removeAliasGroup(group: GroupedAlias) {
  const confirmed = await confirm({
    title: '删除别名映射',
    description: `确定要删除聚合标签 "${group.target}" 下的所有别名映射吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await tagAliasApi.deleteTagAliasGroup(group.target)
    await invalidateAfterTagChange({ aliases: true })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除失败'))
  }
}

async function onDragEnd(updatedPrefTags: TagPreference[]) {
  if (activeTab.value === 'alias') return
  const preference = activeTab.value as 'favourite' | 'blocked'
  const otherTags = allTags.value
    .filter((t) => t.preference !== preference)
    .sort((a, b) => a.sort_index - b.sort_index)
  const newOrderedTags = (
    preference === 'favourite'
      ? [...updatedPrefTags, ...otherTags]
      : [...otherTags, ...updatedPrefTags]
  ).map((t, idx) => ({ ...t, sort_index: idx }))
  queryClient.setQueryData(queryKeys.tagPreferences(), newOrderedTags)
  try {
    await tagPreferenceApi.reorderTagPreferences(newOrderedTags.map((t) => t.id))
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '排序失败'))
  } finally {
    await queryClient.invalidateQueries({ queryKey: queryKeys.tagPreferences() })
  }
}

const openModal = () => {
  currentTag.value = ''
  aliasSource.value = ''
  aliasTarget.value = ''
  isEditingAlias.value = false
  editingAliasOriginalTarget.value = ''
  showModal.value = true
}

const openEditModal = (group: GroupedAlias) => {
  isEditingAlias.value = true
  editingAliasOriginalTarget.value = group.target
  aliasTarget.value = group.target
  aliasSource.value = group.sources.join(', ')
  showModal.value = true
}

const closeModal = () => {
  showModal.value = false
  currentTag.value = ''
  aliasSource.value = ''
  aliasTarget.value = ''
  isEditingAlias.value = false
  editingAliasOriginalTarget.value = ''
}

const tagColumns: TableColumn[] = [
  { key: 'tag', label: '标签名称' },
  { key: 'actions', label: '操作', align: 'right' },
]
const aliasColumns: TableColumn[] = [
  { key: 'target', label: '聚合标签' },
  { key: 'sources', label: '原标签' },
  { key: 'actions', label: '操作', align: 'right' },
]
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 bg-gray-50">
    <PageHeader title="标签管理" @toggle-sidebar="$emit('toggle-sidebar')" />
    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow">
      <SectionHeader
        :tabs="[{ name: 'favourite', label: '喜爱的标签' }, { name: 'blocked', label: '厌恶的标签' }, { name: 'alias', label: '标签别名映射' }]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'favourite' | 'blocked' | 'alias'"
        :add-button-text="activeTab === 'favourite' ? '添加喜爱标签' : activeTab === 'blocked' ? '添加厌恶标签' : '添加别名映射'"
        :show-refresh="true"
        :loading="refreshing"
        @add="openModal()"
        @refresh="void fetchData()"
      >
        <template #actions v-if="activeTab === 'alias'">
          <button
            @click="showSuggestModal = true"
            class="flex items-center px-4 py-2 bg-indigo-600 text-white rounded-md hover:bg-indigo-700 transition-colors shadow-sm text-sm font-medium whitespace-nowrap cursor-pointer"
          >
            <Sparkles class="w-4 h-4 mr-2" /> 别名建议
          </button>
        </template>
      </SectionHeader>

      <!-- 厌恶标签的全局排除开关 -->
      <div
        v-if="activeTab === 'blocked'"
        class="mb-4 bg-white border border-gray-200 rounded-lg px-4 py-3 flex items-center justify-between gap-4"
      >
        <div class="min-w-0">
          <p class="text-sm font-medium text-gray-900">在浏览列表中排除厌恶标签小说</p>
          <p class="text-xs text-gray-500 mt-0.5">
            带厌恶标签的小说将从列表、搜索、计数与全选中隐藏（抓取入库不受影响）
          </p>
        </div>
        <AppSwitch
          :model-value="excludeBlocked"
          :disabled="isUpdatingConfig"
          aria-label="在浏览列表中排除厌恶标签小说"
          @update:model-value="toggleExclude"
        />
      </div>

      <DraggableTable
        v-if="activeTab !== 'alias'"
        :items="filteredTags"
        :columns="tagColumns"
        :loading="loading"
        item-key="id"
        empty-text="暂无标签"
        @reorder="onDragEnd"
      >
        <template #tag="{ item: tag }">
          <span class="text-sm font-medium text-gray-900">{{ tag.tag }}</span>
        </template>
        <template #actions="{ item: tag }">
          <button
            @click="removeTag(tag)"
            class="text-red-600 hover:text-red-900 text-sm font-medium cursor-pointer"
          >
            删除
          </button>
        </template>
      </DraggableTable>

      <DraggableTable
        v-else
        :items="groupedAliases"
        :columns="aliasColumns"
        :loading="loading"
        item-key="target"
        :disabled="true"
        empty-text="暂无别名映射"
      >
        <template #target="{ item: group }">
          <span class="text-sm font-medium text-gray-900">{{ group.target }}</span>
        </template>
        <template #sources="{ item: group }">
          <div
            class="truncate max-w-md text-sm text-gray-900"
            :title="group.sources.join(', ')"
          >
            {{ group.sources.join(', ') }}
          </div>
        </template>
        <template #actions="{ item: group }">
          <div class="flex items-center justify-end space-x-4">
            <button
              @click="openEditModal(group)"
              class="text-indigo-600 hover:text-indigo-900 text-sm font-medium cursor-pointer"
            >
              编辑
            </button>
            <button
              @click="removeAliasGroup(group)"
              class="text-red-600 hover:text-red-900 text-sm font-medium cursor-pointer"
            >
              删除
            </button>
          </div>
        </template>
      </DraggableTable>
    </main>

    <BaseModal
      :is-open="showModal"
      :title="activeTab === 'favourite' ? '添加喜爱标签' : activeTab === 'blocked' ? '添加厌恶标签' : (isEditingAlias ? '编辑别名映射' : '添加别名映射')"
      :loading="saving"
      @close="closeModal"
      @confirm="saveTag"
    >
      <template v-if="activeTab !== 'alias'">
        <AppInput
          :model-value="currentTag"
          @update:model-value="currentTag = $event"
          label="标签名称"
          required
        />
      </template>
      <template v-else>
        <div class="space-y-4">
          <AppInput
            :model-value="aliasTarget"
            @update:model-value="aliasTarget = $event"
            label="聚合标签(目标)"
            placeholder="例如: NTR"
            required
          />
          <AppInput
            :model-value="aliasSource"
            @update:model-value="aliasSource = $event"
            label="被聚合标签(原标签)"
            placeholder="例如: ntr, 绿帽 (使用逗号分隔多个)"
            required
          />
        </div>
      </template>
    </BaseModal>

    <AliasSuggestModal
      :is-open="showSuggestModal"
      @close="showSuggestModal = false"
      @updated="invalidateAfterTagChange({ prefs: true, aliases: true, novels: true })"
    />
  </div>
</template>
