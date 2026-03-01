<script setup lang="ts">
import { ref } from 'vue';
import { type TaskHistory } from '../api';
import { RefreshCw, Clock, FileText, CheckCircle, XCircle } from 'lucide-vue-next';
import LoadMore from './LoadMore.vue';
import StatusBadge from './StatusBadge.vue';
import LogViewer from './LogViewer.vue';

defineProps<{
  history: TaskHistory[];
  loading: boolean;
  hasMore: boolean;
}>();

const emit = defineEmits<{
  (e: 'loadMore'): void;
}>();

const resultModalOpen = ref(false);
const currentResult = ref('');

const parseResult = (resultStr?: string | null) => {
  if (!resultStr) return { log: '', new_novels_count: null };
  try {
    const parsed = JSON.parse(resultStr);
    if (parsed && typeof parsed === 'object' && 'log' in parsed) {
      return { 
        log: parsed.log || '', 
        new_novels_count: parsed.new_novels_count 
      };
    }
  } catch (e) {
    // legacy plain text log
  }
  return { log: resultStr, new_novels_count: null };
};

const showResult = (result: string) => {
  const parsed = parseResult(result);
  currentResult.value = parsed.log || '无输出日志';
  resultModalOpen.value = true;
};

const formatDate = (dateStr?: string | null) => {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleString();
};
</script>

<template>
  <div>
    <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
      <ul class="divide-y divide-gray-200">
        <li v-if="loading && history.length === 0" class="px-6 py-12 text-center text-gray-500">
          <RefreshCw class="w-6 h-6 animate-spin mx-auto text-blue-500 mb-2" />
          加载中...
        </li>
        <li v-else-if="history.length === 0" class="px-6 py-12 text-center text-gray-500">
          暂无历史记录
        </li>
        <li v-for="item in history" :key="item.id" class="px-6 py-4 hover:bg-gray-50 transition-colors">
          <div class="flex items-center justify-between w-full">
            <div class="flex items-center min-w-0 flex-1">
              <div class="flex-shrink-0 mr-4">
                 <!-- Status icons are now inside StatusBadge if we wanted, but StatusBadge is text+icon.
                      The design had large icons on the left. Let's keep the large icons here or reuse StatusBadge?
                      The original design had a large icon on the left AND a badge on the right.
                      Let's stick to the original design for the large icon, but use StatusBadge for the right side badge.
                 -->
                <StatusBadge :status="item.status" class="hidden" /> <!-- just to ensure import usage if needed, but actually we need the large icon -->
                
                <!-- Re-implementing large icons as per original design -->
                <div v-if="item.status === 'SUCCESS'" class="text-green-500"><CheckCircle class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'FAILED'" class="text-red-500"><XCircle class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'RUNNING'" class="text-blue-500 animate-spin"><RefreshCw class="w-8 h-8" /></div>
                <div v-else-if="item.status === 'PENDING'" class="text-yellow-500"><Clock class="w-8 h-8" /></div>
                <div v-else class="text-gray-400"><Clock class="w-8 h-8" /></div>
              </div>
              <div class="min-w-0 flex-1 ml-4 grid grid-cols-3 gap-4 items-center">
                <div class="col-span-1">
                  <p class="text-sm font-medium text-gray-900 truncate">
                    {{ item.name }}
                  </p>
                  <p class="text-xs text-gray-500 mt-1 font-mono truncate max-w-lg" :title="item.arguments">
                    args: {{ item.arguments || 'None' }}
                  </p>
                </div>
                
                <div class="col-span-1 text-xs text-gray-500 flex flex-col justify-center">
                  <span>开始: {{ formatDate(item.start_time) }}</span>
                  <span v-if="item.end_time">结束: {{ formatDate(item.end_time) }}</span>
                  <span v-if="parseResult(item.result).new_novels_count !== null && parseResult(item.result).new_novels_count !== undefined" class="text-green-600 font-medium mt-0.5">
                    新增小说: {{ parseResult(item.result).new_novels_count }}
                  </span>
                </div>

                <div class="col-span-1 flex items-center justify-end space-x-4">
                  <span v-if="typeof item.duration === 'number'" class="text-xs text-gray-500 whitespace-nowrap">耗时: {{ item.duration.toFixed(2) }}s</span>
                  <StatusBadge :status="item.status" />
                  
                  <div class="w-28 flex justify-end">
                    <button 
                      v-if="item.result || item.status === 'FAILED'" 
                      @click="showResult(item.result || '')" 
                      class="inline-flex items-center px-2.5 py-1.5 border border-gray-300 shadow-sm text-xs font-medium rounded text-gray-700 bg-white hover:bg-gray-50 focus:outline-none transition-colors"
                    >
                      <FileText class="w-3.5 h-3.5 mr-1" />
                      日志
                    </button>
                    <span v-else class="w-full"></span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </li>
      </ul>
    </div>
    
    <LoadMore 
      :loading="loading" 
      :no-more-data="!hasMore" 
      :has-data="history.length > 0"
      @load-more="emit('loadMore')" 
    />

    <!-- Modal for Execution Result -->
    <div v-if="resultModalOpen" class="fixed inset-0 z-50 overflow-hidden" aria-labelledby="modal-title" role="dialog" aria-modal="true">
      <div class="absolute inset-0 overflow-hidden">
        <div class="absolute inset-0 bg-gray-500 bg-opacity-75 transition-opacity" @click="resultModalOpen = false" aria-hidden="true"></div>

        <div class="pointer-events-none fixed inset-y-0 right-0 flex max-w-full pl-0 sm:pl-10">
          <div class="pointer-events-auto w-screen max-w-4xl transform transition duration-500 ease-in-out sm:duration-700">
            <div class="flex h-full flex-col bg-white shadow-xl">
               <!-- Header with close button -->
               <div class="px-4 py-3 sm:px-6 bg-gray-50 border-b border-gray-200 flex justify-between items-center">
                  <h3 class="text-lg font-medium text-gray-900">执行日志</h3>
                  <button @click="resultModalOpen = false" class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500">
                    <span class="sr-only">Close panel</span>
                    <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" aria-hidden="true">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
               </div>
               
               <!-- Log Viewer Content -->
               <div class="relative flex-1 overflow-hidden bg-[#1e1e1e]">
                  <LogViewer 
                    :log="currentResult" 
                    :title="currentResult ? 'Log Output' : 'No logs'" 
                  />
               </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
