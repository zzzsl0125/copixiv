<script setup lang="ts" generic="T extends object">
import { ref, watch, type Ref } from 'vue'
import { VueDraggable } from 'vue-draggable-plus'
import { GripVertical, RefreshCw } from '@lucide/vue'

export interface TableColumn {
  key: string
  label: string
  align?: 'left' | 'center' | 'right'
  width?: string
  tdClass?: string
}

const props = defineProps<{
  items: T[]
  columns?: TableColumn[]
  loading?: boolean
  itemKey?: keyof T & string
  emptyText?: string
  columnsCount?: number
  disabled?: boolean
}>()

const emit = defineEmits<{
  reorder: [items: T[]]
}>()

const localItems = ref<T[]>([]) as Ref<T[]>

watch(
  () => props.items,
  (newItems) => {
    localItems.value = [...newItems]
  },
  { immediate: true },
)

const resolvedKey = (item: T, index: number): string | number => {
  const field = props.itemKey ?? ('id' as keyof T & string)
  const val = item[field]
  return typeof val === 'string' || typeof val === 'number' ? val : index
}

const readCell = (item: T, key: string): unknown =>
  (item as Record<string, unknown>)[key]

const onDragEnd = () => {
  emit('reorder', [...localItems.value])
}

const moveItem = (index: number, delta: number) => {
  const nextIndex = index + delta
  if (nextIndex < 0 || nextIndex >= localItems.value.length) return
  const next = [...localItems.value]
  const [item] = next.splice(index, 1)
  if (item === undefined) return
  next.splice(nextIndex, 0, item)
  localItems.value = next
  emit('reorder', next)
}

const getAlignClass = (align?: 'left' | 'center' | 'right') => {
  if (align === 'right') return 'text-right'
  if (align === 'center') return 'text-center'
  return 'text-left'
}
</script>

<template>
  <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
    <div class="overflow-x-auto">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <slot name="header">
            <tr v-if="columns">
              <th v-if="!disabled" scope="col" class="w-10 px-6 py-3"></th>
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

        <VueDraggable
          v-model="localItems"
          tag="tbody"
          :disabled="disabled"
          handle=".drag-handle"
          class="bg-white divide-y divide-gray-200"
          :animation="200"
          ghost-class="bg-blue-50"
          @end="onDragEnd"
        >
          <tr
            v-for="(element, index) in localItems"
            :key="resolvedKey(element, index)"
            class="hover:bg-gray-50 transition-colors group"
          >
            <td
              v-if="!disabled"
              class="px-6 py-4 whitespace-nowrap text-gray-400 cursor-move drag-handle select-none"
            >
              <button
                type="button"
                class="inline-flex focus:outline-none focus-visible:text-blue-600"
                :aria-label="`第 ${index + 1} 行，方向键上/下可调整顺序`"
                @click.stop
                @keydown.arrow-up.prevent="moveItem(index, -1)"
                @keydown.arrow-down.prevent="moveItem(index, 1)"
              >
                <GripVertical class="w-5 h-5 pointer-events-none" aria-hidden="true" />
              </button>
            </td>
            <td
              v-for="col in columns"
              :key="col.key"
              class="px-6 py-4"
              :class="[
                getAlignClass(col.align),
                col.tdClass === undefined ? 'whitespace-nowrap' : col.tdClass,
              ]"
            >
              <slot :name="col.key" :item="element" :index="index">
                <span class="text-sm text-gray-900">{{ readCell(element, col.key) }}</span>
              </slot>
            </td>
          </tr>
        </VueDraggable>

        <tbody v-if="(loading && localItems.length === 0) || (!loading && localItems.length === 0)" class="bg-white">
          <tr v-if="loading && localItems.length === 0">
            <td
              :colspan="columnsCount || (columns ? columns.length + (disabled ? 0 : 1) : 10)"
              class="px-6 py-12 text-center text-gray-500"
            >
              <slot name="loading">
                <div class="flex flex-col items-center justify-center">
                  <RefreshCw class="w-6 h-6 animate-spin text-blue-500 mb-2" />
                  <span>加载中...</span>
                </div>
              </slot>
            </td>
          </tr>
          <tr v-else>
            <td
              :colspan="columnsCount || (columns ? columns.length + (disabled ? 0 : 1) : 10)"
              class="px-6 py-12 text-center text-gray-500"
            >
              <slot name="empty">{{ emptyText || '暂无数据' }}</slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <slot name="footer"></slot>
  </div>
</template>
