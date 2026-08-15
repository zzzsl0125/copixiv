<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import ToastContainer from './components/ui/ToastContainer.vue'
import { useNovels, useSystem, useToast } from './composables'
import type { NovelFilters } from './types'

const {
  novels,
  loading,
  error,
  noMoreData,
  filters,
  loadNovels,
  handleSearch,
  handleLoadMore,
  handleCardSearch,
} = useNovels()

const { systemConfig, fetchConfig } = useSystem()
const { toasts } = useToast()
const configLoadedAndApplied = ref(false)

const syncActiveSection = () => {
  if (filters.keyword === 'is_favourite:true;') {
    activeSection.value = 'favourites'
  } else if (filters.keyword === 'is_special_follow:true;') {
    activeSection.value = 'special_follow'
  } else if (filters.keyword) {
    activeSection.value = null
  } else {
    activeSection.value = 'novels'
  }
}

const applyConfigAndLoad = () => {
  if (configLoadedAndApplied.value) return

  const config = systemConfig.value
  if (config) {
    const urlParams = new URLSearchParams(window.location.search)
    if (!urlParams.has('min_like')) {
      filters.min_like = config.default_min_like
    }
    if (!urlParams.has('min_text')) {
      filters.min_text = config.default_min_text
    }
  }

  configLoadedAndApplied.value = true
  syncActiveSection()
  loadNovels()
}

watch(() => systemConfig.value, (newConfig) => {
  if (newConfig && !configLoadedAndApplied.value) {
    applyConfigAndLoad()
  }
})

const isSidebarOpen = ref(false)
const route = useRoute()
const router = useRouter()
const activeSection = ref<'novels' | 'favourites' | 'special_follow' | null>('novels')

const isNovelsRoute = computed(() => route.path === '/')

watch(route, (to) => {
  if (to.path !== '/') {
    activeSection.value = null
  } else {
    syncActiveSection()
  }
})

const handleNovelStateChanged = (payload: { id: number; field: 'is_favourite' | 'is_special_follow'; value: number }) => {
  const novel = novels.value.find(n => n.id === payload.id)
  if (novel) novel[payload.field] = payload.value
}

const handleSectionSearch = (keyword: string | undefined, section: 'novels' | 'favourites' | 'special_follow') => {
  activeSection.value = section
  handleSearch(keyword, { setOrdering: true })
}

const handleResetToDefaults = () => {
  filters.keyword = ''
  filters.order_by = 'random'
  filters.order_direction = 'DESC'
  filters.min_like = systemConfig.value?.default_min_like
  filters.min_text = systemConfig.value?.default_min_text

  activeSection.value = 'novels'
  handleSearch(undefined, { setOrdering: false })
}

const handleLogoClick = () => {
  if (route.path !== '/') router.push('/')
  handleResetToDefaults()
}

onMounted(() => {
  if (route.path !== '/') {
    activeSection.value = null
  }
  // Load novels once the config attempt settles — success applies the
  // defaults, failure still lets the home page browse with URL/current
  // filters instead of waiting forever for a systemConfig that never comes.
  fetchConfig().then(() => {
    if (!configLoadedAndApplied.value) applyConfigAndLoad()
  })
})
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <Sidebar
      :is-open="isSidebarOpen"
      :filters="filters"
      :show-filters="isNovelsRoute"
      :active-section="activeSection"
      :config-loaded-and-applied="configLoadedAndApplied"
      @close="isSidebarOpen = false"
      @search="handleSectionSearch"
      @update:filters="($event: NovelFilters) => { Object.assign(filters, $event); handleSearch(); }"
      @reset-to-defaults="handleResetToDefaults"
    />

    <div class="flex-1 flex flex-col min-w-0">
      <router-view
        :is-sidebar-open="isSidebarOpen"
        :filters="filters"
        :novels="novels"
        :loading="loading"
        :error="error"
        :no-more-data="noMoreData"
        @logo-click="handleLogoClick"
        @load-more="handleLoadMore"
        @search="(keyword?: string) => handleSearch(keyword, { setOrdering: true })"
        @card-search="handleCardSearch"
        @novel-state-changed="handleNovelStateChanged"
        @update:filters="($event: NovelFilters) => { Object.assign(filters, $event); handleSearch(); }"
        @toggle-sidebar="isSidebarOpen = !isSidebarOpen"
      />
    </div>

    <ToastContainer :toasts="toasts" />
  </div>
</template>
