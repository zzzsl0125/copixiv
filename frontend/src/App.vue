<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import Sidebar from './components/Sidebar.vue';
import { useNovels } from './composables/useNovels';

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  loadNovels,
  handleSearch,
  handleLoadMore,
  handleCardSearch
} = useNovels();

const isSidebarOpen = ref(false);
const route = useRoute();

const isNovelsRoute = computed(() => route.path === '/');

onMounted(() => {
  loadNovels();
});
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <Sidebar 
      :is-open="isSidebarOpen" 
      :filters="filters" 
      :show-filters="isNovelsRoute"
      @close="isSidebarOpen = false" 
      @search="(keyword?: string) => handleSearch(keyword)" 
      @update:filters="Object.assign(filters, $event)" 
    />

    <div class="flex-1 flex flex-col min-w-0">
      <router-view
        :is-sidebar-open="isSidebarOpen"
        :filters="filters"
        :novels="novels"
        :loading="loading"
        :error="error"
        :no-more-data="noMoreData"
        @load-more="handleLoadMore"
        @search="handleSearch"
        @card-search="handleCardSearch"
        @update:filters="Object.assign(filters, $event)"
        @toggle-sidebar="isSidebarOpen = !isSidebarOpen"
      />
    </div>
  </div>
</template>
