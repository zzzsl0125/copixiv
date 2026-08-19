<script setup lang="ts">
import { X, History } from '@lucide/vue'
import type { SearchHistory } from '../../types'

defineProps<{
  history: SearchHistory[]
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'select-item', item: SearchHistory): void
  (e: 'delete-item', id: number): void
  (e: 'clear-all'): void
}>()
</script>

<template>
  <div v-if="show && history.length > 0" class="absolute top-full mt-2 w-full max-w-md bg-white border border-gray-200 rounded-md shadow-lg z-20">
    <div class="px-4 py-2 flex justify-between items-center border-b border-gray-200">
      <h3 class="text-sm font-semibold text-gray-700">搜索历史</h3>
      <button type="button" @click.stop="emit('clear-all')" class="text-xs text-blue-500 hover:text-blue-700">
        全部清除
      </button>
    </div>
    <ul class="py-1 max-h-60 overflow-y-auto">
      <li
        v-for="item in history"
        :key="item.id"
        role="button"
        tabindex="0"
        @click="emit('select-item', item)"
        @keydown.enter.prevent="emit('select-item', item)"
        @keydown.space.prevent="emit('select-item', item)"
        class="px-4 py-2 text-sm text-gray-800 hover:bg-gray-100 cursor-pointer flex justify-between items-center group focus:bg-gray-100 focus:outline-none"
      >
        <div class="flex items-center">
          <History class="w-4 h-4 mr-2 text-gray-400" aria-hidden="true" />
          <span>{{ item.display_value || item.value }}</span>
          <span v-if="item.type !== 'keyword'" class="ml-2 text-xs text-gray-500 bg-gray-200 px-1.5 py-0.5 rounded">
            {{ item.type }}
          </span>
        </div>
        <button type="button" @click.stop="emit('delete-item', item.id)" class="text-gray-400 hover:text-gray-600 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100 transition-opacity" :aria-label="`删除搜索历史：${item.display_value || item.value}`">
          <X class="w-4 h-4" />
        </button>
      </li>
    </ul>
  </div>
</template>
