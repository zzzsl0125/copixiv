<script setup lang="ts">
import { ref, watch } from 'vue';
import draggable from 'vuedraggable';
import { RefreshCw, GripVertical } from 'lucide-vue-next';

export interface TableColumn {
  key: string;
  label: string;
  align?: 'left' | 'center' | 'right';
  width?: string;
  tdClass?: string;
}

const props = defineProps<{
  items: any[];
  columns?: TableColumn[];
  loading?: boolean;
  itemKey?: string;
  emptyText?: string;
  columnsCount?: number;
}>();

const emit = defineEmits<{
  (e: 'reorder', items: any[]): void;
}>();

const localItems = ref<any[]>([]);

watch(() => props.items, (newItems) => {
  localItems.value = [...newItems];
}, { immediate: true, deep: true });

const onDragEnd = () => {
  emit('reorder', localItems.value);
};

const getAlignClass = (align?: 'left' | 'center' | 'right') => {
  if (align === 'right') return 'text-right';
  if (align === 'center') return 'text-center';
  return 'text-left';
};
</script>

<template>
  <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
    <div class="overflow-x-auto">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <slot name="header">
            <tr v-if="columns">
              <th scope="col" class="w-10 px-6 py-3"></th>
              <th 
                v-for="col in columns" 
                :key="col.key"
                scope="col" 
                class="px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider"
                :class="getAlignClass(col.align)"
                :style="{ width: col.width }"
              >
                {{ col.label }}
              </th>
            </tr>
          </slot>
        </thead>
        <draggable
          v-model="localItems"
          tag="tbody"
          :item-key="itemKey || 'id'"
          handle=".drag-handle"
          class="bg-white divide-y divide-gray-200"
          :animation="200"
          ghost-class="bg-blue-50"
          drag-class="opacity-0"
          :force-fallback="true"
          fallback-class="hidden"
          @start="() => console.log('Drag started')"
          @end="(e: any) => { console.log('Drag ended', e); onDragEnd(); }"
        >
          <template #item="{ element, index }">
            <tr class="hover:bg-gray-50 transition-colors group">
              <td class="px-6 py-4 whitespace-nowrap text-gray-400 cursor-move drag-handle select-none">
                <GripVertical class="w-5 h-5 pointer-events-none" />
              </td>
              <td 
                v-for="col in columns" 
                :key="col.key"
                class="px-6 py-4"
                :class="[getAlignClass(col.align), col.tdClass === undefined ? 'whitespace-nowrap' : col.tdClass]"
              >
                <slot :name="col.key" :item="element" :index="index">
                  <span class="text-sm text-gray-900">{{ element[col.key] }}</span>
                </slot>
              </td>
            </tr>
          </template>
          <template #footer>
            <tr v-if="loading && localItems.length === 0">
              <td :colspan="columnsCount || (columns ? columns.length + 1 : 10)" class="px-6 py-12 text-center text-gray-500">
                <slot name="loading">
                  <div class="flex flex-col items-center justify-center">
                    <RefreshCw class="w-6 h-6 animate-spin text-blue-500 mb-2" />
                    <span>加载中...</span>
                  </div>
                </slot>
              </td>
            </tr>
            <tr v-else-if="localItems.length === 0">
              <td :colspan="columnsCount || (columns ? columns.length + 1 : 10)" class="px-6 py-12 text-center text-gray-500">
                <slot name="empty">{{ emptyText || '暂无数据' }}</slot>
              </td>
            </tr>
          </template>
        </draggable>
      </table>
    </div>
  </div>
</template>
