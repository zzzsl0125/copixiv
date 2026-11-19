<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, Settings, ListChecks, AlertTriangle, Upload } from '@lucide/vue'
import type { SectionId } from '../config'
import type { NovelFilters } from '../types'
import { useNovelQueryState, useNovelShellState } from '../composables'

defineProps<{
  isOpen: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const route = useRoute()
const router = useRouter()
const { filters, activeSection, navigateToSection, updateFilters } = useNovelQueryState()
const { isBatchMode, isInCollectionView, enterBatchMode, toggleBatchMode } = useNovelShellState()

const isNovelsRoute = computed(() => route.path === '/')

const handleSectionClick = (section: SectionId) => {
  void navigateToSection(section)
  emit('close')
}

const updateFilter = <K extends keyof NovelFilters>(key: K, value: NovelFilters[K]) => {
  void updateFilters({ [key]: value })
}

/**
 * 批量操作：与「阅览」一栏按钮一致——不在小说页时先跳回 '/'。
 * 离页点击只进不退，留在小说页时切换开关。
 */
const handleBatchClick = () => {
  if (route.path !== '/') {
    void router.push('/')
    if (!isBatchMode.value) enterBatchMode()
  } else {
    toggleBatchMode()
  }
  emit('close')
}

const btnClass = (isActive: boolean, pxClass = 'px-3') => [
  `py-2 ${pxClass} text-sm font-medium rounded-md transition-colors`,
  isActive ? 'bg-blue-500 text-white' : 'bg-gray-50 text-gray-700 hover:bg-gray-100',
]

const navItemClass = (isActive: boolean) => [
  'group flex items-center justify-center px-3 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 hover:bg-gray-50 rounded-md',
  isActive ? 'bg-gray-100' : '',
]
const navIconClass = 'mr-3 h-5 w-5 text-gray-400 group-hover:text-gray-500'
</script>

<template>
  <div
    v-if="isOpen"
    class="fixed inset-0 bg-gray-900/30 z-20 md:hidden transition-opacity"
    @click="$emit('close')"
  ></div>

  <div
    :class="[
      isOpen ? 'translate-x-0' : '-translate-x-full',
      'fixed inset-y-0 left-0 z-30 w-72 bg-white shadow-xl transform transition-transform duration-300 ease-in-out md:translate-x-0 md:sticky md:top-0 md:h-screen md:shrink-0 flex flex-col',
    ]"
  >
    <div class="flex-1 overflow-y-auto px-8 py-6">
      <nav class="space-y-2 mb-8">
        <!-- 阅览 -->
        <h3 class="pt-1 text-sm font-semibold text-gray-500 uppercase tracking-wider mb-1">阅览</h3>
        <button
          type="button"
          :class="[navItemClass(isNovelsRoute && activeSection === 'novels'), 'w-full']"
          data-testid="nav-novels"
          @click="handleSectionClick('novels')"
        >
          <Search :class="navIconClass" /> 小说列表
        </button>
        <button
          type="button"
          :class="[navItemClass(isNovelsRoute && activeSection === 'favourites'), 'w-full']"
          data-testid="nav-favourites"
          @click="handleSectionClick('favourites')"
        >
          <Search :class="navIconClass" /> 我的收藏
        </button>
        <button
          type="button"
          :class="[navItemClass(isNovelsRoute && activeSection === 'special_follow'), 'w-full']"
          data-testid="nav-special-follow"
          @click="handleSectionClick('special_follow')"
        >
          <Search :class="navIconClass" /> 特别关注
        </button>
        <button
          type="button"
          :class="[navItemClass(isNovelsRoute && activeSection === 'uploads'), 'w-full']"
          data-testid="nav-uploads"
          @click="handleSectionClick('uploads')"
        >
          <Upload :class="navIconClass" /> 我的上传
        </button>

        <!-- 配置 -->
        <h3 class="pt-4 text-sm font-semibold text-gray-500 uppercase tracking-wider mb-1">配置</h3>
        <router-link to="/tasks" :class="[navItemClass(false), route.path === '/tasks' ? 'bg-gray-100' : '']" @click="$emit('close')">
          <Settings :class="navIconClass" /> 任务管理
        </router-link>
        <router-link to="/tag-management" :class="[navItemClass(false), route.path === '/tag-management' ? 'bg-gray-100' : '']" @click="$emit('close')">
          <Settings :class="navIconClass" /> 标签管理
        </router-link>
        <router-link to="/tokens" :class="[navItemClass(false), route.path === '/tokens' ? 'bg-gray-100' : '']" @click="$emit('close')">
          <Settings :class="navIconClass" /> 账号管理
        </router-link>

        <!-- 其他 -->
        <h3 class="pt-4 text-sm font-semibold text-gray-500 uppercase tracking-wider mb-1">其他</h3>
        <router-link
          to="/failed-novels"
          :class="navItemClass(route.path === '/failed-novels')"
          @click="$emit('close')"
        >
          <AlertTriangle class="mr-3 h-5 w-5 text-gray-400 group-hover:text-gray-500" />
          失败记录
        </router-link>
        <button
          @click="handleBatchClick"
          data-testid="nav-batch-mode"
          class="group w-full flex items-center justify-center px-3 py-2 text-sm font-medium rounded-md transition-colors"
          :class="isNovelsRoute && isBatchMode ? 'bg-blue-500 text-white' : 'text-gray-700 hover:text-gray-900 hover:bg-gray-50'"
        >
          <ListChecks class="mr-3 h-5 w-5" :class="isNovelsRoute && isBatchMode ? 'text-white' : 'text-gray-400 group-hover:text-gray-500'" />
          批量操作
        </button>
      </nav>

      <!-- Filters -->
      <template v-if="isNovelsRoute">
        <div class="my-6 border-t border-gray-200"></div>
        <h3 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">排序</h3>
        <div class="space-y-3 mb-8">
          <div class="grid grid-cols-3 gap-2">
            <button @click="updateFilter('order_by', 'like')" :class="btnClass(filters.order_by === 'like')">点赞</button>
            <button @click="updateFilter('order_by', 'id')" :class="btnClass(filters.order_by === 'create_time' || filters.order_by === 'id')">时间</button>
            <button @click="updateFilter('order_by', 'random')" :class="btnClass(filters.order_by === 'random')" :disabled="isInCollectionView" :style="isInCollectionView ? 'opacity: 0.5; cursor: not-allowed;' : ''">随机</button>
          </div>
          <div class="grid grid-cols-3 gap-2">
            <button @click="updateFilter('order_direction', 'ASC')" :class="btnClass(filters.order_direction === 'ASC')" :disabled="filters.order_by === 'random'" :style="filters.order_by === 'random' ? 'opacity: 0.5; cursor: not-allowed;' : ''">升序</button>
            <button @click="updateFilter('order_direction', 'DESC')" :class="btnClass(filters.order_direction === 'DESC')" :disabled="filters.order_by === 'random'" :style="filters.order_by === 'random' ? 'opacity: 0.5; cursor: not-allowed;' : ''">降序</button>
            <button @click="handleSectionClick('novels')" :class="btnClass(false)">重置</button>
          </div>
        </div>

        <h3 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">筛选</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm text-gray-700 mb-2">最低字数</label>
            <div class="grid grid-cols-4 gap-2">
              <button @click="updateFilter('min_text', 0)" :class="btnClass(filters.min_text === undefined || filters.min_text === 0, 'px-1')">不限</button>
              <button @click="updateFilter('min_text', 3000)" :class="btnClass(filters.min_text === 3000, 'px-1')">3.0k</button>
              <button @click="updateFilter('min_text', 10000)" :class="btnClass(filters.min_text === 10000, 'px-1')">10.0k</button>
              <button @click="updateFilter('min_text', 30000)" :class="btnClass(filters.min_text === 30000, 'px-1')">30.0k</button>
            </div>
          </div>
          <div>
            <label class="block text-sm text-gray-700 mb-2">最低点赞</label>
            <div class="grid grid-cols-4 gap-2">
              <button @click="updateFilter('min_like', 0)" :class="btnClass(filters.min_like === undefined || filters.min_like === 0, 'px-1')">不限</button>
              <button @click="updateFilter('min_like', 500)" :class="btnClass(filters.min_like === 500, 'px-1')">500</button>
              <button @click="updateFilter('min_like', 2500)" :class="btnClass(filters.min_like === 2500, 'px-1')">2.5k</button>
              <button @click="updateFilter('min_like', 5000)" :class="btnClass(filters.min_like === 5000, 'px-1')">5.0k</button>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>
