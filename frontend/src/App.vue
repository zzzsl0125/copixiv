<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import { Toaster } from 'vue-sonner'
import { useNovelSidebarBridge, useTaskStatus } from './composables'

const isSidebarOpen = ref(false)
const route = useRoute()
const isNovelsRoute = computed(() => route.path === '/')

// Global task-status singleton: one fetch on mount picks up rows that were
// already pending/running before the page loaded; it then polls only while
// something is active (no per-view polling loops).
const { startTaskStatusPolling } = useTaskStatus()

const {
  activeSection,
  configLoadedAndApplied,
  isBatchMode,
  isInCollectionView,
  sidebarFilters,
  dispatchSectionSearch,
  dispatchUpdateFilters,
  dispatchResetToDefaults,
  dispatchToggleBatchMode,
} = useNovelSidebarBridge()

onMounted(() => {
  startTaskStatusPolling()
})
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <Sidebar
      :is-open="isSidebarOpen"
      :filters="sidebarFilters"
      :show-filters="isNovelsRoute"
      :active-section="isNovelsRoute ? activeSection : null"
      :config-loaded-and-applied="configLoadedAndApplied"
      :is-batch-mode="isNovelsRoute && isBatchMode"
      :random-disabled="isNovelsRoute && isInCollectionView"
      @close="isSidebarOpen = false"
      @search="dispatchSectionSearch"
      @update:filters="dispatchUpdateFilters"
      @reset-to-defaults="dispatchResetToDefaults"
      @toggle-batch-mode="dispatchToggleBatchMode"
    />

    <div class="flex-1 flex flex-col min-w-0">
      <router-view @toggle-sidebar="isSidebarOpen = !isSidebarOpen" />
    </div>

    <!-- 全局 toast 出口：旧容器不限条数，sonner 默认只叠 3 条；
         取 5 条更接近旧观感，又不会让连发消息堆满屏幕。 -->
    <Toaster
      position="bottom-right"
      :rich-colors="true"
      :close-button="true"
      :visible-toasts="5"
    />
  </div>
</template>
