<script setup lang="ts">
import type { SearchHistory } from '../api';
import { X, History } from 'lucide-vue-next';

defineProps<{
  history: SearchHistory[];
  show: boolean;
}>();

const emit = defineEmits<{
  (e: 'select-item', item: SearchHistory): void;
  (e: 'delete-item', id: number): void;
  (e: 'clear-all'): void;
}>();

const handleItemClick = (item: SearchHistory) => {
  emit('select-item', item);
};

const handleDeleteClick = (id: number) => {
  emit('delete-item', id);
};

const handleClearAllClick = () => {
  emit('clear-all');
};
</script>

<template>
  <div v-if="show && history.length > 0" class="absolute top-full mt-2 w-full max-w-md bg-white border border-gray-200 rounded-md shadow-lg z-20">
    <div class="px-4 py-2 flex justify-between items-center border-b border-gray-200">
      <h3 class="text-sm font-semibold text-gray-700">搜索历史</h3>
      <button @click.stop="handleClearAllClick" class="text-xs text-blue-500 hover:text-blue-700">
        全部清除
      </button>
    </div>
    <ul class="py-1 max-h-60 overflow-y-auto">
      <li
        v-for="item in history"
        :key="item.id"
        @click="handleItemClick(item)"
        class="px-4 py-2 text-sm text-gray-800 hover:bg-gray-100 cursor-pointer flex justify-between items-center group"
      >
        <div class="flex items-center">
          <History class="w-4 h-4 mr-2 text-gray-400" />
          <span>{{ item.display_value || item.value }}</span>
          <span v-if="item.type !== 'keyword'" class="ml-2 text-xs text-gray-500 bg-gray-200 px-1.5 py-0.5 rounded">
            {{ item.type }}
          </span>
        </div>
        <button @click.stop="handleDeleteClick(item.id)" class="text-gray-400 hover:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity">
          <X class="w-4 h-4" />
        </button>
      </li>
    </ul>
  </div>
</template>
