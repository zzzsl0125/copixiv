<script setup lang="ts">
import { Search, Menu } from 'lucide-vue-next';
import AppLogo from './AppLogo.vue';

defineProps<{
  filters: {
    keyword: string;
    order_by: string;
    order_direction: string;
  };
}>();

defineEmits<{
  (e: 'search'): void;
  (e: 'update:filters', filters: any): void;
  (e: 'toggle-sidebar'): void;
}>();
</script>

<template>
  <header class="w-full bg-white shadow-sm sticky top-0 z-10">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
      <div class="flex flex-row gap-4 items-center justify-between">
        <div class="flex items-center gap-3">
          <button @click="$emit('toggle-sidebar')" class="md:hidden p-1 -ml-1 text-gray-500 hover:text-gray-700 focus:outline-none flex-shrink-0">
            <Menu class="h-6 w-6" />
          </button>
          <AppLogo 
            className="flex-shrink-0"
            @click="() => {
              filters.keyword = '';
              filters.order_by = 'random';
              filters.order_direction = 'DESC';
              $emit('search');
            }"
          />
        </div>
        
        <!-- Search Controls -->
        <div class="flex flex-row gap-4 items-center justify-end flex-grow min-w-0">
          <!-- Search Bar -->
          <div class="relative w-full max-w-md min-w-0">
            <div class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
              <Search class="h-5 w-5 text-gray-400" />
            </div>
            <input 
              v-model="filters.keyword" 
              @keyup.enter="$emit('search')"
              type="text" 
              class="block w-full pl-10 pr-3 py-2 border border-gray-300 rounded-md leading-5 bg-white placeholder-gray-500 focus:outline-none focus:placeholder-gray-400 focus:ring-1 focus:ring-blue-500 focus:border-blue-500 sm:text-sm" 
            />
          </div>
        </div>
      </div>
    </div>
  </header>
</template>
