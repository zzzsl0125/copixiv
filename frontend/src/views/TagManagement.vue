<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { tagPreferenceApi, tagAliasApi, type TagPreferenceResponse, type TagAlias } from '../api';
import PageHeader from '../components/PageHeader.vue';
import SectionHeader from '../components/SectionHeader.vue';
import DraggableTable, { type TableColumn } from '../components/DraggableTable.vue';
import BaseModal from '../components/BaseModal.vue';
import AppInput from '../components/AppInput.vue';
import AliasSuggestModal from '../components/AliasSuggestModal.vue';
import { Sparkles } from 'lucide-vue-next';

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const allTags = ref<TagPreferenceResponse[]>([]);
const allAliases = ref<TagAlias[]>([]);
const loading = ref(true);

const activeTab = ref<'favourite' | 'blocked' | 'alias'>('favourite');

const showModal = ref(false);
const showSuggestModal = ref(false);
const suggestTarget = ref<string | undefined>(undefined);
const saving = ref(false);
const currentTag = ref('');
const aliasSource = ref('');
const aliasTarget = ref('');
const isEditingAlias = ref(false);
const editingAliasOriginalTarget = ref('');

interface GroupedAlias {
  target: string;
  sources: string[];
  aliases: TagAlias[];
}

const groupedAliases = computed<GroupedAlias[]>(() => {
  const map = new Map<string, TagAlias[]>();
  for (const alias of allAliases.value) {
    if (!map.has(alias.target)) {
      map.set(alias.target, []);
    }
    map.get(alias.target)!.push(alias);
  }
  return Array.from(map.entries()).map(([target, aliases]) => ({
    target,
    sources: aliases.map(a => a.source),
    aliases,
  }));
});

const filteredTags = computed(() => {
  return allTags.value
    .filter(t => t.preference === activeTab.value)
    .sort((a, b) => a.sort_index - b.sort_index);
});

async function fetchData() {
  loading.value = true;
  try {
    if (activeTab.value === 'alias') {
      allAliases.value = await tagAliasApi.getTagAliases();
    } else {
      allTags.value = await tagPreferenceApi.getTagPreferences();
    }
  } catch (error) {
    console.error('Failed to fetch tags/aliases:', error);
  } finally {
    loading.value = false;
  }
}

watch(activeTab, () => {
  fetchData();
});

async function saveTag() {
  if (activeTab.value === 'alias') {
    if (!aliasSource.value.trim() || !aliasTarget.value.trim()) return;
    saving.value = true;
    try {
      const newSources = aliasSource.value.split(/[,，\n]+/).map(s => s.trim()).filter(s => s);
      const target = aliasTarget.value.trim();

      if (isEditingAlias.value) {
        // Editing an existing group
        const existingGroup = groupedAliases.value.find(g => g.target === editingAliasOriginalTarget.value);
        if (existingGroup) {
          const oldSources = existingGroup.sources;
          const toAdd = newSources.filter(s => !oldSources.includes(s));
          const toRemove = existingGroup.aliases.filter(a => !newSources.includes(a.source));

          // Delete removed ones
          for (const a of toRemove) {
            await tagAliasApi.deleteTagAlias(a.id);
          }
          // Add new ones
          for (const s of toAdd) {
            await tagAliasApi.createTagAlias(s, target);
          }
          // If the target name itself changed, we need to update all remaining sources
          if (target !== editingAliasOriginalTarget.value) {
             const toKeepAndRename = existingGroup.aliases.filter(a => newSources.includes(a.source));
             for (const a of toKeepAndRename) {
               await tagAliasApi.deleteTagAlias(a.id);
               await tagAliasApi.createTagAlias(a.source, target);
             }
          }
        }
      } else {
        // Creating new
        for (const s of newSources) {
          await tagAliasApi.createTagAlias(s, target);
        }
      }

      await fetchData();
      closeModal();
    } catch (error) {
      console.error('Failed to save tag aliases:', error);
      alert('保存失败');
    } finally {
      saving.value = false;
    }
  } else {
    if (!currentTag.value.trim()) return;
    saving.value = true;
    try {
      await tagPreferenceApi.setTagPreference(currentTag.value.trim(), activeTab.value as 'favourite' | 'blocked');
      await fetchData();
      closeModal();
    } catch (error) {
      console.error(`Failed to add ${activeTab.value} tag:`, error);
      alert('添加失败');
    } finally {
      saving.value = false;
    }
  }
}

async function removeTag(tag: TagPreferenceResponse) {
  if (!confirm(`确定要删除标签 "${tag.tag}" 吗？`)) return;
  try {
    await tagPreferenceApi.deleteTagPreference(tag.tag);
    await fetchData();
  } catch (error) {
    console.error('Failed to remove tag:', error);
    alert('删除失败');
  }
}

async function removeAliasGroup(group: GroupedAlias) {
  if (!confirm(`确定要删除聚合标签 "${group.target}" 下的所有别名映射吗？`)) return;
  try {
    for (const alias of group.aliases) {
      await tagAliasApi.deleteTagAlias(alias.id);
    }
    await fetchData();
  } catch (error) {
    console.error('Failed to remove alias group:', error);
    alert('删除失败');
  }
}

async function onDragEnd(updatedTags: any[]) {
  if (activeTab.value === 'alias') return; // Aliases are not reorderable
  const updatedPrefTags = updatedTags as TagPreferenceResponse[];
  const preference = activeTab.value as 'favourite' | 'blocked';
  const otherTags = allTags.value.filter(t => t.preference !== preference).sort((a, b) => a.sort_index - b.sort_index);
  
  // Combine updated tags with the other preference tags
  // The backend might depend on the overall order of all tags, but the API accepts all ids.
  const newOrderedTags = preference === 'favourite' ? [...updatedPrefTags, ...otherTags] : [...otherTags, ...updatedPrefTags];

  try {
    const tagIds = newOrderedTags.map(t => t.id);
    await tagPreferenceApi.reorderTagPreferences(tagIds);
    // Locally update without full fetch to keep it snappy if possible, but fetch is safer.
    await fetchData();
  } catch (error) {
    console.error('Failed to reorder tags:', error);
    alert('排序失败');
    await fetchData();
  }
}

const openModal = () => {
  currentTag.value = '';
  aliasSource.value = '';
  aliasTarget.value = '';
  isEditingAlias.value = false;
  editingAliasOriginalTarget.value = '';
  showModal.value = true;
};

const openEditModal = (group: GroupedAlias) => {
  suggestTarget.value = group.target;
  showSuggestModal.value = true;
};

const closeModal = () => {
  showModal.value = false;
  currentTag.value = '';
  aliasSource.value = '';
  aliasTarget.value = '';
  isEditingAlias.value = false;
  editingAliasOriginalTarget.value = '';
};

onMounted(() => {
  fetchData();
});

const tagColumns: TableColumn[] = [
  { key: 'tag', label: '标签名称' },
  { key: 'actions', label: '操作', align: 'right' }
];

const aliasColumns: TableColumn[] = [
  { key: 'target', label: '聚合标签' },
  { key: 'sources', label: '原标签' },
  { key: 'actions', label: '操作', align: 'right' }
];

const currentColumns = computed(() => activeTab.value === 'alias' ? aliasColumns : tagColumns);

</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="标签管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader 
        :tabs="[
          { name: 'favourite', label: '喜爱的标签' },
          { name: 'blocked', label: '厌恶的标签' },
          { name: 'alias', label: '标签别名映射' }
        ]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'favourite' | 'blocked' | 'alias'"
        :add-button-text="activeTab === 'favourite' ? '添加喜爱标签' : activeTab === 'blocked' ? '添加厌恶标签' : '添加别名映射'"
        :show-refresh="true"
        :loading="loading"
        @add="openModal()"
        @refresh="fetchData()"
      >
        <template #actions v-if="activeTab === 'alias'">
          <button 
            @click="suggestTarget = undefined; showSuggestModal = true" 
            class="flex items-center px-4 py-2 bg-indigo-600 text-white rounded-md hover:bg-indigo-700 transition-colors shadow-sm text-sm font-medium whitespace-nowrap"
          >
            <Sparkles class="w-4 h-4 mr-2" />
            别名建议
          </button>
        </template>
      </SectionHeader>

      <DraggableTable 
        :items="activeTab === 'alias' ? groupedAliases : filteredTags" 
        :columns="currentColumns"
        :loading="loading"
        :empty-text="activeTab === 'alias' ? '暂无别名映射' : '暂无标签'"
        @reorder="onDragEnd"
      >
        <!-- Tag Slots -->
        <template #tag="{ item: tag }" v-if="activeTab !== 'alias'">
          <span class="text-sm font-medium text-gray-900">{{ tag.tag }}</span>
        </template>
        
        <template #actions="{ item }" v-if="activeTab !== 'alias'">
          <button @click="removeTag(item)" class="text-red-600 hover:text-red-900 text-sm font-medium">删除</button>
        </template>

        <!-- Alias Slots -->
        <template #target="{ item: group }" v-if="activeTab === 'alias'">
          <span class="text-sm font-medium text-gray-900">{{ group.target }}</span>
        </template>

        <template #sources="{ item: group }" v-if="activeTab === 'alias'">
          <div class="truncate max-w-md text-sm text-gray-900" :title="group.sources.join(', ')">
            {{ group.sources.join(', ') }}
          </div>
        </template>

        <template #actions="{ item: group }" v-if="activeTab === 'alias'">
          <div class="flex items-center justify-end space-x-4">
            <button @click="openEditModal(group)" class="text-indigo-600 hover:text-indigo-900 text-sm font-medium">编辑</button>
            <button @click="removeAliasGroup(group)" class="text-red-600 hover:text-red-900 text-sm font-medium">删除</button>
          </div>
        </template>
      </DraggableTable>
    </main>

    <!-- Add Modal -->
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
          @keyup.enter="saveTag"
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
            @keyup.enter="saveTag"
          />
        </div>
      </template>
    </BaseModal>

    <!-- Suggest Modal -->
    <AliasSuggestModal
      :is-open="showSuggestModal"
      :target-tag="suggestTarget"
      @close="showSuggestModal = false"
      @updated="fetchData"
    />
  </div>
</template>
