<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { Search, Menu } from '@lucide/vue'
import AppLogo from '../ui/AppLogo.vue'
import SearchHistoryPopup from './SearchHistoryPopup.vue'
import { searchHistoryApi } from '../../api'
import { getApiErrorMessage } from '../../api/errors'
import { useToast } from '../../composables'
import type { SearchHistory, NovelFilters } from '../../types'

const props = defineProps<{
  filters: Pick<NovelFilters, 'keyword' | 'order_by' | 'order_direction'>
}>()

const emit = defineEmits<{
  (e: 'search', keyword?: string): void
  (e: 'update:filters', filters: Partial<NovelFilters>): void
  (e: 'toggle-sidebar'): void
  (e: 'logo-click'): void
}>()

const toast = useToast()
const localKeyword = ref(props.filters.keyword)

watch(() => props.filters.keyword, (newKeyword) => {
  localKeyword.value = newKeyword
})

const performSearch = () => {
  showHistory.value = false
  emit('search', localKeyword.value)
}

const history = ref<SearchHistory[]>([])
const showHistory = ref(false)
const searchContainer = ref<HTMLElement | null>(null)

const fetchHistory = async () => {
  try {
    history.value = await searchHistoryApi.getSearchHistory()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '搜索历史加载失败'))
    history.value = []
  }
}

const handleFocus = () => {
  fetchHistory()
  showHistory.value = true
}

const handleSelectItem = (item: SearchHistory) => {
  if (item.type === 'keyword') {
    localKeyword.value = item.value
  } else {
    localKeyword.value = `${item.type}:${item.value};`
  }
  performSearch()
  showHistory.value = false
}

const handleDeleteItem = async (id: number) => {
  try {
    await searchHistoryApi.deleteSearchHistoryItem(id)
    fetchHistory()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除搜索历史失败'))
  }
}

const handleClearAll = async () => {
  try {
    await searchHistoryApi.clearSearchHistory()
    history.value = []
    showHistory.value = false
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '清空搜索历史失败'))
  }
}

const handleClickOutside = (event: MouseEvent) => {
  if (searchContainer.value && !searchContainer.value.contains(event.target as Node)) {
    showHistory.value = false
  }
}

onMounted(() => {
  document.addEventListener('mousedown', handleClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', handleClickOutside)
})
</script>

<template>
  <header class="w-full bg-white shadow-sm sticky top-0 z-10">
    <h1 class="sr-only">小说列表</h1>
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
      <div class="flex flex-row gap-4 items-center justify-between">
        <div class="flex items-center gap-3">
          <button @click="$emit('toggle-sidebar')" class="md:hidden p-1 -ml-1 text-gray-500 hover:text-gray-700 focus:outline-none flex-shrink-0" aria-label="打开菜单">
            <Menu class="h-6 w-6" aria-hidden="true" />
          </button>
          <AppLogo className="flex-shrink-0" @click="$emit('logo-click')" />
        </div>

        <div class="flex flex-row gap-4 items-center justify-end flex-grow min-w-0">
          <div ref="searchContainer" class="relative w-full max-w-md min-w-0">
            <div class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none" aria-hidden="true">
              <Search class="h-5 w-5 text-gray-400" />
            </div>
            <input
              v-model="localKeyword"
              @keyup.enter="performSearch"
              @focus="handleFocus"
              type="text"
              autocomplete="off"
              aria-label="搜索小说"
              placeholder="搜索标题 / 作者 / 系列 / 标签 / ID…"
              class="block w-full pl-10 pr-3 py-2 border border-gray-300 rounded-md leading-5 bg-white placeholder-gray-500 focus:outline-none focus:placeholder-gray-400 focus:ring-1 focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
            />
            <SearchHistoryPopup
              :history="history"
              :show="showHistory"
              @select-item="handleSelectItem"
              @delete-item="handleDeleteItem"
              @clear-all="handleClearAll"
            />
          </div>
        </div>
      </div>
    </div>
  </header>
</template>
