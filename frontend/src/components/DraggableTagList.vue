<script setup lang="ts">
import { ref, watch } from 'vue';
import draggable from 'vuedraggable';
import { GripVertical, Trash2 } from 'lucide-vue-next';
import { type TagPreferenceResponse } from '../api';

const props = defineProps<{
  tags: TagPreferenceResponse[];
}>();

const emit = defineEmits<{
  (e: 'reorder', tags: TagPreferenceResponse[]): void;
  (e: 'delete', tag: TagPreferenceResponse): void;
}>();

const localTags = ref<TagPreferenceResponse[]>([]);

watch(() => props.tags, (newTags) => {
  localTags.value = [...newTags];
}, { immediate: true, deep: true });

const onDragEnd = () => {
  emit('reorder', localTags.value);
};
</script>

<template>
  <div class="bg-white rounded-lg shadow overflow-hidden divide-y divide-gray-200">
    <draggable
      v-model="localTags"
      tag="ul"
      item-key="id"
      handle=".drag-handle"
      @end="onDragEnd"
    >
      <template #item="{ element: tag }">
        <li class="flex justify-between items-center p-3 select-none">
          <div class="flex items-center">
            <GripVertical class="w-5 h-5 text-gray-400 cursor-move drag-handle mr-2" />
            <span class="text-gray-900">{{ tag.tag }}</span>
          </div>
          <button @click="$emit('delete', tag)" class="text-red-600 hover:text-red-800 font-medium">
            <Trash2 class="w-4 h-4" />
          </button>
        </li>
      </template>
    </draggable>
    <div v-if="localTags.length === 0" class="p-4 text-center text-gray-500">
      暂无标签
    </div>
  </div>
</template>
