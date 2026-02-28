<script setup lang="ts">
import { ref, watch, onUnmounted, nextTick } from 'vue';
import { Loader2 } from 'lucide-vue-next';

const props = defineProps<{
  loading: boolean;
  noMoreData: boolean;
  hasData: boolean;
}>();

const emit = defineEmits<{
  (e: 'load-more'): void;
}>();

const containerRef = ref<HTMLElement | null>(null);
let observer: IntersectionObserver | null = null;

const setupObserver = () => {
  if (observer) {
    observer.disconnect();
    observer = null;
  }
  
  if (!containerRef.value) return;

  observer = new IntersectionObserver(
    (entries) => {
      const target = entries[0];
      if (target && target.isIntersecting && !props.loading && !props.noMoreData && props.hasData) {
        emit('load-more');
      }
    },
    {
      root: null,
      rootMargin: '100px', // 提前 100px 触发
      threshold: 0.1
    }
  );

  observer.observe(containerRef.value);
};

// 监听 containerRef 的挂载/卸载（由 v-if 驱动）
watch(containerRef, (el) => {
  if (el) {
    setupObserver();
  } else if (observer) {
    observer.disconnect();
    observer = null;
  }
});

const checkAndReobserve = async () => {
  if (!props.loading && !props.noMoreData && props.hasData) {
    // 确保 Vue DOM 更新完毕，特别是瀑布流重排
    await nextTick();
    setTimeout(() => {
      if (observer && containerRef.value) {
        // 重置观察者以强制重新检查交叉状态
        observer.unobserve(containerRef.value);
        observer.observe(containerRef.value);
      }
    }, 300);
  }
};

// 当 loading 结束时，如果元素依然在视口内，重新评估
watch(() => props.loading, (newVal) => {
  if (!newVal) {
    checkAndReobserve();
  }
});

onUnmounted(() => {
  if (observer) {
    observer.disconnect();
    observer = null;
  }
});
</script>

<template>
  <div v-if="hasData" class="mt-8 flex justify-center py-6" ref="containerRef">
    <button 
      v-if="!noMoreData" 
      @click="$emit('load-more')" 
      :disabled="loading"
      class="px-6 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-full text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
    >
      <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
      {{ loading ? '加载中...' : '加载更多' }}
    </button>
    <p v-else class="text-sm text-gray-500">
      已经到底啦 ~
    </p>
  </div>
</template>
