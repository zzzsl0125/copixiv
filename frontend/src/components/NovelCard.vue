<script lang="ts">
import { ref, computed } from 'vue';

const activeCardId = ref<number | string | null>(null);

if (typeof window !== 'undefined') {
  window.addEventListener('click', () => {
    activeCardId.value = null;
  });
}
</script>

<script setup lang="ts">
import { novelApi, type Novel } from '../api';

const props = defineProps<{
  novel: Novel;
}>();

const emit = defineEmits<{
  (e: 'search', type: 'author' | 'series' | 'tag', value: string | number): void;
}>();

const formatNumber = (num?: number) => {
  if (!num) return '0';
  if (num >= 10000) {
    return (num / 10000).toFixed(1) + 'w';
  }
  return num.toString();
};

const showMobileActions = computed(() => activeCardId.value === props.novel.id);

const toggleActions = (e: Event) => {
  e.stopPropagation();
  if (activeCardId.value === props.novel.id) {
    activeCardId.value = null;
  } else {
    activeCardId.value = props.novel.id;
  }
};

// 阻止事件冒泡以避免触发卡片的 toggle
const stopPropagation = (e: Event) => {
  e.stopPropagation();
};

// 处理不带值的无效点击
const handleSeriesClick = (e: Event) => {
  if (!props.novel.series_id) return;
  e.stopPropagation();
  emit('search', 'series', props.novel.series_id);
};

const handleAuthorClick = (e: Event) => {
  if (!props.novel.author_id) return;
  e.stopPropagation();
  emit('search', 'author', props.novel.author_id);
};

const handleToggleFavourite = async (e: Event) => {
  e.stopPropagation();
  try {
    await novelApi.toggleFavourite(props.novel.id);
    props.novel.is_favourite = props.novel.is_favourite ? 0 : 1;
  } catch (error) {
    console.error('Failed to toggle favourite', error);
  }
};

const handleToggleFollow = async (e: Event) => {
  e.stopPropagation();
  if (!props.novel.author_id) return;
  try {
    await novelApi.toggleSpecialFollow(props.novel.author_id);
    props.novel.is_special_follow = props.novel.is_special_follow ? 0 : 1;
  } catch (error) {
    console.error('Failed to toggle follow', error);
  }
};

const likeBorderClass = computed(() => {
  const likes = props.novel.like || 0;
  if (likes >= 5000) {
    return 'ring-2 ring-red-200 border-transparent shadow-[0_0_15px_rgba(239,68,68,0.15)]';
  } else if (likes >= 2500) {
    return 'ring-2 ring-yellow-200 border-transparent shadow-[0_0_15px_rgba(234,179,8,0.15)]';
  } else if (likes >= 500) {
    return 'ring-2 ring-blue-200 border-transparent shadow-[0_0_15px_rgba(59,130,246,0.15)]';
  }
  return 'border-gray-100'; // 默认无效果
});
</script>

<template>
  <div 
    class="relative group bg-white rounded-xl shadow-sm hover:shadow-xl transition-all duration-300 overflow-hidden flex flex-col border cursor-pointer"
    :class="likeBorderClass"
    @click="toggleActions"
  >
    <div class="p-5 flex-grow flex flex-col">
      <div class="mb-1">
        <a 
          :href="`https://www.pixiv.net/novel/show.php?id=${novel.id}`" 
          target="_blank" 
          rel="noopener noreferrer"
          class="text-lg font-bold text-gray-900 hover:text-blue-600 inline-block" 
          :title="novel.title"
          @click="stopPropagation"
        >
          {{ novel.title }}
        </a>
      </div>
      <div v-if="novel.series_name" class="text-sm text-gray-500 mb-1 flex items-start">
        <span class="shrink-0 whitespace-nowrap">系列：</span>
        <span 
          :class="[{ 'cursor-pointer hover:text-blue-600': novel.series_id }, 'break-words']"
          @click="handleSeriesClick"
        >
          {{ novel.series_name }}<template v-if="novel.series_index"> #{{ novel.series_index }}</template>
        </span>
      </div>
      <div class="text-sm text-gray-500 mb-3 flex items-start">
        <span class="shrink-0 whitespace-nowrap">作者：</span>
        <span 
          :class="[{ 'cursor-pointer hover:text-blue-600': novel.author_id }, 'break-words']"
          @click="handleAuthorClick"
        >
          {{ novel.author_name || '未知' }}
        </span>
      </div>
      
      <div class="flex flex-wrap gap-1">
        <span v-for="(tag, idx) in novel.tags || []" :key="idx" 
          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-800 cursor-pointer hover:bg-gray-200 hover:text-blue-600 transition-colors"
          @click="stopPropagation($event); emit('search', 'tag', tag)"
        >
          {{ tag }}
        </span>
      </div>
    </div>
    
    <div class="px-5 py-4 bg-gray-50 border-t border-gray-100 flex flex-col gap-3 relative overflow-hidden">
      <!-- 原底部信息栏 -->
      <div class="flex justify-between items-center text-xs text-gray-500 transition-opacity duration-300"
           :class="[showMobileActions ? 'opacity-0' : 'opacity-100 group-hover:opacity-0']">
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1" title="喜爱">❤️ {{ formatNumber(novel.like) }}</span>
          <span class="flex items-center gap-1" title="字数">📝 {{ formatNumber(novel.text) }}</span>
          <span class="flex items-center gap-1" title="日期">📝 {{ novel.create_time || '2016-01-01' }}</span>
        </div>
        <span v-if="novel.has_epub" class="px-1.5 py-0.5 rounded bg-green-100 text-green-800 text-[10px] font-bold">EPUB</span>
      </div>

      <!-- 悬停/点击操作栏浮层 -->
      <div 
        class="absolute inset-0 bg-white/95 backdrop-blur-sm border-t border-gray-100 flex items-center px-3 py-2 gap-2 transition-transform duration-300"
        :class="[
          'translate-y-full', // 默认隐藏
          'group-hover:translate-y-0', // PC Hover 显示
          showMobileActions ? '!translate-y-0' : '' // 移动端点击显示 (!important 覆盖 hover 以防冲突)
        ]"
        @click="stopPropagation"
      >
        <div class="flex-1 flex gap-2 h-full">
          <a 
            :href="novelApi.downloadNovelUrl(novel.id, 'txt')"
            target="_blank"
            class="flex-1 flex items-center justify-center text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors cursor-pointer"
            @click="stopPropagation"
          >
            下载
          </a>
          <button 
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_favourite ? 'text-white bg-red-500 hover:bg-red-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            @click="handleToggleFavourite"
          >
            {{ novel.is_favourite ? '已收藏' : '收藏' }}
          </button>
          <button 
            class="flex-1 flex items-center justify-center text-xs font-medium rounded-lg transition-colors cursor-pointer"
            :class="novel.is_special_follow ? 'text-white bg-purple-500 hover:bg-purple-600' : 'text-gray-700 bg-gray-100 hover:bg-gray-200'"
            @click="handleToggleFollow"
          >
            {{ novel.is_special_follow ? '已追更' : '追更' }}
          </button>
          <button 
            class="flex-1 flex items-center justify-center text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors cursor-pointer"
            @click="stopPropagation"
          >
            编辑
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
