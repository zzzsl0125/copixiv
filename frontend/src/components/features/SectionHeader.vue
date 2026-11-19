<script setup lang="ts">
import { Plus, RefreshCw } from '@lucide/vue'
import AppTabs, { type TabItem } from '../ui/AppTabs.vue'

export type Tab = TabItem

defineProps<{
  title?: string
  tabs?: Tab[]
  activeTab?: string
  addButtonText?: string
  showRefresh?: boolean
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'add'): void
  (e: 'refresh'): void
  (e: 'update:activeTab', value: string): void
}>()
</script>

<template>
  <div class="mb-6 border-b border-gray-200 flex justify-between min-h-[58px]">
    <div class="flex-1 flex items-end">
      <h1 v-if="title" class="text-2xl font-bold text-gray-900 py-4">{{ title }}</h1>
      <AppTabs
        v-else-if="tabs && tabs.length > 0 && activeTab !== undefined"
        :tabs="tabs"
        :model-value="activeTab"
        @update:model-value="emit('update:activeTab', $event)"
      />
      <slot name="left"></slot>
    </div>

    <div class="flex items-center space-x-4 py-3">
      <slot name="actions"></slot>

      <button
        v-if="addButtonText"
        @click="$emit('add')"
        class="flex items-center px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors shadow-sm text-sm font-medium whitespace-nowrap cursor-pointer"
      >
        <Plus class="w-4 h-4 mr-2" />
        {{ addButtonText }}
      </button>

      <div v-if="showRefresh" class="flex space-x-2">
        <button
          @click="$emit('refresh')"
          class="p-2 text-gray-500 hover:text-blue-600 rounded-full hover:bg-gray-100 transition-colors cursor-pointer"
          title="刷新"
          aria-label="刷新"
        >
          <RefreshCw class="w-5 h-5" :class="{ 'animate-spin': loading }" />
        </button>
      </div>
    </div>
  </div>
</template>
