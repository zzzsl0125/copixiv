<script setup lang="ts">
import { ref, toRef } from 'vue';
import NovelCard from '../components/NovelCard.vue';
import NovelHeader from '../components/NovelHeader.vue';
import LoadMore from '../components/LoadMore.vue';
import EmptyState from '../components/EmptyState.vue';
import { useMasonryLayout } from '../composables/useMasonryLayout';
import type { Novel } from '../api';

const props = defineProps<{
  isSidebarOpen: boolean;
  filters: any;
  novels: Novel[];
  loading: boolean;
  error: string | null;
  noMoreData: boolean;
}>();

const emit = defineEmits<{
  (e: 'toggle-sidebar'): void;
  (e: 'search', keyword?: string): void;
  (e: 'card-search', type: string, value: string | number): void;
  (e: 'update:filters', filters: any): void;
  (e: 'load-more'): void;
}>();

const columnRefs = ref<HTMLElement[]>([]);

const { columns } = useMasonryLayout(
  toRef(props, 'novels'), 
  columnRefs
);
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full">
    <NovelHeader 
      :filters="props.filters" 
      @search="() => emit('search')" 
      @update:filters="emit('update:filters', $event)" 
      @toggle-sidebar="emit('toggle-sidebar')"
    />

    <!-- Main Content -->
    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow">
    
    <!-- Error State -->
    <div v-if="props.error" class="bg-red-50 border-l-4 border-red-400 p-4 mb-6 rounded-md">
      <div class="flex">
        <div class="flex-shrink-0">
          <svg class="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
            <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
          </svg>
        </div>
        <div class="ml-3">
          <p class="text-sm text-red-700">{{ props.error }}</p>
        </div>
      </div>
    </div>

    <!-- Novel Grid -->
    <div v-if="props.novels.length > 0" class="flex gap-6 items-start">
      <div v-for="(col, colIndex) in columns" :key="colIndex" :ref="el => { if (el) columnRefs[colIndex] = el as HTMLElement }" class="flex-1 flex flex-col gap-6 w-full min-w-0">
        <NovelCard v-for="novel in col" :key="novel.id" :novel="novel" @search="(type, value) => emit('card-search', type, value)" />
      </div>
    </div>
    
    <EmptyState v-else-if="!props.loading" :loading="props.loading" />

    <LoadMore 
      :loading="props.loading" 
      :no-more-data="props.noMoreData" 
      :has-data="props.novels.length > 0"
      @load-more="emit('load-more')" 
    />

    </main>
  </div>
</template>
