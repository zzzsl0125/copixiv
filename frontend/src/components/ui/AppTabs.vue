<script setup lang="ts">
import { TabsList, TabsRoot, TabsTrigger } from 'reka-ui'

export interface TabItem {
  name: string
  label: string
}

defineProps<{
  tabs: TabItem[]
  modelValue: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()
</script>

<template>
  <TabsRoot
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', String($event))"
  >
    <TabsList class="-mb-[1px] flex space-x-8" aria-label="Tabs">
      <TabsTrigger
        v-for="tab in tabs"
        :key="tab.name"
        :value="tab.name"
        class="whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors focus:outline-none focus-visible:text-blue-600 data-[state=active]:border-blue-500 data-[state=active]:text-blue-600 border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
        :class="tabs.length === 1 ? 'cursor-default' : 'cursor-pointer'"
      >
        {{ tab.label }}
      </TabsTrigger>
    </TabsList>
  </TabsRoot>
</template>
