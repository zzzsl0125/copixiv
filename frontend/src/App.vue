<script setup lang="ts">
import { ref } from 'vue'
import { Toaster } from 'vue-sonner'
import Sidebar from './components/Sidebar.vue'
import ConfirmDialog from './components/ui/ConfirmDialog.vue'
import { useProvideNovelShellState, useTaskStatus } from './composables'

const isSidebarOpen = ref(false)

// Cross-shell UI state (batch mode toggle, persistent selectedIds, random refresh nonce)
useProvideNovelShellState()

// Global busy-task polling singleton backed by Vue Query: automatically fetches on
// mount and polls every 5s only while busyFuncs is non-empty.
useTaskStatus()
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <Sidebar :is-open="isSidebarOpen" @close="isSidebarOpen = false" />

    <div class="flex-1 flex flex-col min-w-0">
      <router-view @toggle-sidebar="isSidebarOpen = !isSidebarOpen" />
    </div>

    <ConfirmDialog />

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
