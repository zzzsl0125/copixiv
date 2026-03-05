<script setup lang="ts">
import { Search as SearchIcon, Settings, Power } from 'lucide-vue-next';
import { ref } from 'vue';
import RestartModal from './RestartModal.vue';

const props = defineProps<{
  isOpen: boolean;
  showFilters?: boolean;
  activeSection: 'novels' | 'favourites' | 'special_follow' | null;
  configLoadedAndApplied?: boolean;
  filters: {
    keyword: string;
    order_by: string;
    order_direction: string;
    min_like?: number;
    min_text?: number;
  };
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'search', keyword: string | undefined, section: 'novels' | 'favourites' | 'special_follow'): void;
  (e: 'update:filters', filters: any): void;
  (e: 'reset-to-defaults'): void;
}>();

const isRestarting = ref(false);
const showRestartModal = ref(false);

const handleRestartClick = () => {
  showRestartModal.value = true;
};

const handleRestarting = () => {
  isRestarting.value = true;
};

const handleRestarted = () => {
  isRestarting.value = false;
  alert('应用正在重启，请稍后手动刷新页面。');
};

const handleRestartError = (message: string) => {
  isRestarting.value = false;
  alert(`重启失败: ${message}`);
};

const updateFilter = (key: keyof typeof props.filters, value: any) => {
  const newFilters = { ...props.filters, [key]: value };
  emit('update:filters', newFilters);
};

const btnClass = (isActive: boolean, pxClass = 'px-3') => [
  `py-2 ${pxClass} text-sm font-medium rounded-md transition-colors`,
  isActive ? 'bg-blue-500 text-white' : 'bg-gray-50 text-gray-700 hover:bg-gray-100'
];

const navItemClass = (isActive: boolean) => [
  "group flex items-center justify-center px-3 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 hover:bg-gray-50 rounded-md",
  isActive ? 'bg-gray-100' : ''
];
const navIconClass = "mr-3 h-5 w-5 text-gray-400 group-hover:text-gray-500";
</script>

<template>
  <!-- Mobile backdrop -->
  <div 
    v-if="isOpen" 
    class="fixed inset-0 bg-gray-600 bg-opacity-75 z-20 md:hidden transition-opacity"
    @click="$emit('close')"
  ></div>

  <!-- Sidebar -->
  <div 
    :class="[
      isOpen ? 'translate-x-0' : '-translate-x-full',
      'fixed inset-y-0 left-0 z-30 w-72 bg-white shadow-xl transform transition-transform duration-300 ease-in-out md:translate-x-0 md:sticky md:top-0 md:h-screen md:flex-shrink-0 flex flex-col'
    ]"
  >

    <!-- Navigation Area -->
    <div class="flex-1 overflow-y-auto px-8 py-6">
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">功能</h3>
      <nav class="space-y-2 mb-8">
        <a href="#" :class="navItemClass(activeSection === 'novels')" @click.prevent="() => { if($route.path !== '/') $router.push('/'); $emit('reset-to-defaults'); }">
          <SearchIcon :class="navIconClass" />
          小说列表
        </a>
        <a href="#" :class="navItemClass(activeSection === 'favourites')" @click.prevent="() => { if($route.path !== '/') $router.push('/'); $emit('search', 'is_favourite:true;', 'favourites'); }">
          <SearchIcon :class="navIconClass" />
          我的收藏
        </a>
        <a href="#" :class="navItemClass(activeSection === 'special_follow')" @click.prevent="() => { if($route.path !== '/') $router.push('/'); $emit('search', 'is_special_follow:true;', 'special_follow'); }">
          <SearchIcon :class="navIconClass" />
          特别关注
        </a>
        <router-link to="/tasks" :class="[navItemClass(false), $route.path === '/tasks' ? 'bg-gray-100' : '']">
          <Settings :class="navIconClass" />
          任务管理
        </router-link>
        <router-link to="/tag-management" :class="[navItemClass(false), $route.path === '/tag-management' ? 'bg-gray-100' : '']">
          <Settings :class="navIconClass" />
          标签管理
        </router-link>
        <button @click="handleRestartClick" :disabled="isRestarting" :class="navItemClass(false) + ' w-full'">
          <Power :class="navIconClass" />
          {{ isRestarting ? '重启中...' : '重启应用' }}
        </button>
      </nav>

      <!-- Filter Area (Only show on Novels view) -->
      <template v-if="showFilters !== false && configLoadedAndApplied">
        <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">排序</h3>
      <div class="space-y-3 mb-8">
        <div class="grid grid-cols-3 gap-2">
          <button 
            @click="updateFilter('order_by', 'like')"
            :class="btnClass(filters.order_by === 'like')"
          >
            点赞
          </button>
          <button 
            @click="updateFilter('order_by', 'id')"
            :class="btnClass(filters.order_by === 'create_time' || filters.order_by === 'id')"
          >
            时间
          </button>
          <button 
            @click="updateFilter('order_by', 'random')"
            :class="btnClass(filters.order_by === 'random')"
          >
            随机
          </button>
        </div>
        
        <div class="grid grid-cols-3 gap-2">
          <button 
            @click="updateFilter('order_direction', 'ASC')"
            :class="btnClass(filters.order_direction === 'ASC')"
            :disabled="filters.order_by === 'random'"
            :style="filters.order_by === 'random' ? 'opacity: 0.5; cursor: not-allowed;' : ''"
          >
            升序
          </button>
          <button 
            @click="updateFilter('order_direction', 'DESC')"
            :class="btnClass(filters.order_direction === 'DESC')"
            :disabled="filters.order_by === 'random'"
            :style="filters.order_by === 'random' ? 'opacity: 0.5; cursor: not-allowed;' : ''"
          >
            降序
          </button>
          <button 
            @click="$emit('reset-to-defaults')"
            :class="btnClass(false)"
          >
            重置
          </button>
        </div>
      </div>

      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">筛选</h3>
      <div class="space-y-4">
        <div>
          <label class="block text-sm text-gray-700 mb-2">最低字数</label>
          <div class="grid grid-cols-4 gap-2">
            <button 
              @click="updateFilter('min_text', undefined)"
              :class="btnClass(filters.min_text === undefined, 'px-1')"
            >
              不限
            </button>
            <button 
              @click="updateFilter('min_text', 3000)"
              :class="btnClass(filters.min_text === 3000, 'px-1')"
            >
              3.0k
            </button>
            <button 
              @click="updateFilter('min_text', 10000)"
              :class="btnClass(filters.min_text === 10000, 'px-1')"
            >
              10.0k
            </button>
            <button 
              @click="updateFilter('min_text', 30000)"
              :class="btnClass(filters.min_text === 30000, 'px-1')"
            >
              30.0k
            </button>
          </div>
        </div>

        <div>
          <label class="block text-sm text-gray-700 mb-2">最低点赞</label>
          <div class="grid grid-cols-4 gap-2">
            <button 
              @click="updateFilter('min_like', undefined)"
              :class="btnClass(filters.min_like === undefined, 'px-1')"
            >
              不限
            </button>
            <button 
              @click="updateFilter('min_like', 500)"
              :class="btnClass(filters.min_like === 500, 'px-1')"
            >
              500
            </button>
            <button 
              @click="updateFilter('min_like', 2500)"
              :class="btnClass(filters.min_like === 2500, 'px-1')"
            >
              2.5k
            </button>
            <button 
              @click="updateFilter('min_like', 5000)"
              :class="btnClass(filters.min_like === 5000, 'px-1')"
            >
              5.0k
            </button>
          </div>
        </div>
      </div>
      </template>
    </div>
  </div>

  <RestartModal 
    v-model:isOpen="showRestartModal"
    @restarting="handleRestarting"
    @restarted="handleRestarted"
    @error="handleRestartError"
  />
</template>
