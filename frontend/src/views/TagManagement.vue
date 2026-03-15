<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { tagPreferenceApi, type TagPreferenceResponse } from '../api';
import DraggableTagList from '../components/DraggableTagList.vue';
import PageHeader from '../components/PageHeader.vue';

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const allTags = ref<TagPreferenceResponse[]>([]);
const newFavouriteTag = ref('');
const newBlockedTag = ref('');

const favouriteTags = computed(() => allTags.value.filter(t => t.preference === 'favourite'));
const blockedTags = computed(() => allTags.value.filter(t => t.preference === 'blocked'));

async function fetchTagPreferences() {
  allTags.value = await tagPreferenceApi.getTagPreferences();
  console.log('Fetched tags:', allTags.value);
}

async function addTag(tag: string, preference: 'favourite' | 'blocked') {
  if (!tag) return;
  try {
    await tagPreferenceApi.setTagPreference(tag, preference);
    fetchTagPreferences();
    if (preference === 'favourite') {
      newFavouriteTag.value = '';
    } else {
      newBlockedTag.value = '';
    }
  } catch (error) {
    console.error(`Failed to add ${preference} tag:`, error);
    alert('添加失败');
  }
}

async function removeTag(tag: TagPreferenceResponse) {
  if (!confirm(`确定要删除标签 "${tag.tag}" 吗？`)) return;
  try {
    await tagPreferenceApi.deleteTagPreference(tag.tag);
    fetchTagPreferences();
  } catch (error) {
    console.error('Failed to remove tag:', error);
    alert('删除失败');
  }
}

async function reorderTags(updatedTags: TagPreferenceResponse[], preference: 'favourite' | 'blocked') {
  const otherTags = allTags.value.filter(t => t.preference !== preference).sort((a, b) => a.sort_index - b.sort_index);
  const newOrderedTags = preference === 'favourite' ? [...updatedTags, ...otherTags] : [...otherTags, ...updatedTags];

  try {
    const tagIds = newOrderedTags.map(t => t.id);
    await tagPreferenceApi.reorderTagPreferences(tagIds);
    await fetchTagPreferences();
  } catch (error) {
    console.error('Failed to reorder tags:', error);
    alert('排序失败');
    await fetchTagPreferences();
  }
}

onMounted(fetchTagPreferences);
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="标签管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
        <div>
          <h2 class="text-xl font-semibold mb-4 text-gray-800">喜爱的标签</h2>
          <div class="flex mb-4">
            <input v-model="newFavouriteTag" @keyup.enter="addTag(newFavouriteTag, 'favourite')" class="border-gray-300 shadow-sm p-2 flex-grow rounded-l-md focus:ring-blue-500 focus:border-blue-500" type="text" placeholder="添加喜爱的标签">
            <button @click="addTag(newFavouriteTag, 'favourite')" class="bg-blue-600 text-white p-2 rounded-r-md hover:bg-blue-700 font-semibold">添加</button>
          </div>
          <DraggableTagList :tags="favouriteTags" @reorder="(tags) => reorderTags(tags, 'favourite')" @delete="removeTag" />
        </div>
        <div>
          <h2 class="text-xl font-semibold mb-4 text-gray-800">厌恶的标签</h2>
          <div class="flex mb-4">
            <input v-model="newBlockedTag" @keyup.enter="addTag(newBlockedTag, 'blocked')" class="border-gray-300 shadow-sm p-2 flex-grow rounded-l-md focus:ring-blue-500 focus:border-blue-500" type="text" placeholder="添加厌恶的标签">
            <button @click="addTag(newBlockedTag, 'blocked')" class="bg-blue-600 text-white p-2 rounded-r-md hover:bg-blue-700 font-semibold">添加</button>
          </div>
          <DraggableTagList :tags="blockedTags" @reorder="(tags) => reorderTags(tags, 'blocked')" @delete="removeTag" />
        </div>
      </div>
    </main>
  </div>
</template>
