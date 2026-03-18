<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { tagPreferenceApi, type TagPreferenceResponse } from '../api';
import PageHeader from '../components/PageHeader.vue';
import SectionHeader from '../components/SectionHeader.vue';
import DraggableTable, { type TableColumn } from '../components/DraggableTable.vue';
import BaseModal from '../components/BaseModal.vue';
import AppInput from '../components/AppInput.vue';

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const allTags = ref<TagPreferenceResponse[]>([]);
const loading = ref(true);

const activeTab = ref<'favourite' | 'blocked'>('favourite');

const showModal = ref(false);
const saving = ref(false);
const currentTag = ref('');

const filteredTags = computed(() => {
  return allTags.value
    .filter(t => t.preference === activeTab.value)
    .sort((a, b) => a.sort_index - b.sort_index);
});

async function fetchTagPreferences() {
  loading.value = true;
  try {
    allTags.value = await tagPreferenceApi.getTagPreferences();
  } catch (error) {
    console.error('Failed to fetch tags:', error);
  } finally {
    loading.value = false;
  }
}

async function saveTag() {
  if (!currentTag.value.trim()) return;
  saving.value = true;
  try {
    await tagPreferenceApi.setTagPreference(currentTag.value.trim(), activeTab.value);
    await fetchTagPreferences();
    closeModal();
  } catch (error) {
    console.error(`Failed to add ${activeTab.value} tag:`, error);
    alert('添加失败');
  } finally {
    saving.value = false;
  }
}

async function removeTag(tag: TagPreferenceResponse) {
  if (!confirm(`确定要删除标签 "${tag.tag}" 吗？`)) return;
  try {
    await tagPreferenceApi.deleteTagPreference(tag.tag);
    await fetchTagPreferences();
  } catch (error) {
    console.error('Failed to remove tag:', error);
    alert('删除失败');
  }
}

async function onDragEnd(updatedTags: TagPreferenceResponse[]) {
  const preference = activeTab.value;
  const otherTags = allTags.value.filter(t => t.preference !== preference).sort((a, b) => a.sort_index - b.sort_index);
  
  // Combine updated tags with the other preference tags
  // The backend might depend on the overall order of all tags, but the API accepts all ids.
  const newOrderedTags = preference === 'favourite' ? [...updatedTags, ...otherTags] : [...otherTags, ...updatedTags];

  try {
    const tagIds = newOrderedTags.map(t => t.id);
    await tagPreferenceApi.reorderTagPreferences(tagIds);
    // Locally update without full fetch to keep it snappy if possible, but fetch is safer.
    await fetchTagPreferences();
  } catch (error) {
    console.error('Failed to reorder tags:', error);
    alert('排序失败');
    await fetchTagPreferences();
  }
}

const openModal = () => {
  currentTag.value = '';
  showModal.value = true;
};

const closeModal = () => {
  showModal.value = false;
  currentTag.value = '';
};

onMounted(() => {
  fetchTagPreferences();
});

const columns: TableColumn[] = [
  { key: 'tag', label: '标签名称' },
  { key: 'actions', label: '操作', align: 'right' }
];
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="标签管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader 
        :tabs="[
          { name: 'favourite', label: '喜爱的标签' },
          { name: 'blocked', label: '厌恶的标签' }
        ]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'favourite' | 'blocked'"
        :add-button-text="activeTab === 'favourite' ? '添加喜爱标签' : '添加厌恶标签'"
        :show-refresh="true"
        :loading="loading"
        @add="openModal()"
        @refresh="fetchTagPreferences()"
      >
      </SectionHeader>

      <DraggableTable 
        :items="filteredTags" 
        :columns="columns"
        :loading="loading"
        empty-text="暂无标签"
        @reorder="onDragEnd"
      >
        <template #tag="{ item: tag }">
          <span class="text-sm font-medium text-gray-900">{{ tag.tag }}</span>
        </template>
        
        <template #actions="{ item: tag }">
          <button @click="removeTag(tag)" class="text-red-600 hover:text-red-900 text-sm font-medium">删除</button>
        </template>
      </DraggableTable>
    </main>

    <!-- Add Modal -->
    <BaseModal
      :is-open="showModal"
      :title="activeTab === 'favourite' ? '添加喜爱标签' : '添加厌恶标签'"
      :loading="saving"
      @close="closeModal"
      @confirm="saveTag"
    >
      <AppInput 
        :model-value="currentTag"
        @update:model-value="currentTag = $event"
        label="标签名称" 
        required 
        @keyup.enter="saveTag"
      />
    </BaseModal>
  </div>
</template>
