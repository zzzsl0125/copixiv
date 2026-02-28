<script setup lang="ts">
import { ref, watch } from 'vue';
import { type ScheduledTask } from '../api';
import { Play, Square, Edit, Trash2, RefreshCw, Zap, GripVertical } from 'lucide-vue-next';
import LoadMore from './LoadMore.vue';
import draggable from 'vuedraggable';

const props = defineProps<{
  tasks: ScheduledTask[];
  loading: boolean;
  hasMore: boolean;
}>();

const emit = defineEmits<{
  (e: 'edit', task: ScheduledTask): void;
  (e: 'delete', id: number): void;
  (e: 'toggle', task: ScheduledTask): void;
  (e: 'run', task: ScheduledTask): void;
  (e: 'loadMore'): void;
  (e: 'reorder', tasks: ScheduledTask[]): void;
}>();

const localTasks = ref<ScheduledTask[]>([]);

watch(() => props.tasks, (newTasks) => {
  localTasks.value = [...newTasks];
}, { immediate: true, deep: true });

const onDragEnd = () => {
  emit('reorder', localTasks.value);
};

const formatCron = (cron: string) => {
  if (!cron) return '';
  const parts = cron.split(' ');
  if (parts.length !== 5) return cron;
  
  const minute = parts[0] || '0';
  const hour = parts[1] || '0';
  const dayOfMonth = parts[2] || '*';
  const month = parts[3] || '*';
  const dayOfWeek = parts[4] || '*';
  
  // Format times like "0 4 * * *" -> "每天4:00"
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return `每天 ${hour}:${minute.padStart(2, '0')}`;
  }
  
  // Format times like "0 4 * * 1" -> "每周一4:00"
  if (dayOfMonth === '*' && month === '*' && dayOfWeek !== '*') {
    const days = ['日', '一', '二', '三', '四', '五', '六', '日']; // 0 and 7 can both be Sunday in cron
    const dayStr = days[parseInt(dayOfWeek)];
    if (dayStr) {
      return `每周${dayStr} ${hour}:${minute.padStart(2, '0')}`;
    }
  }
  
  // Format times like "0 4 1 * *" -> "每月1日4:00"
  if (dayOfMonth !== '*' && month === '*' && dayOfWeek === '*') {
    return `每月${dayOfMonth}日 ${hour}:${minute.padStart(2, '0')}`;
  }
  
  // Fallback to original cron expression if it's more complex
  return cron;
};
</script>

<template>
  <div>
    <div class="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-gray-200">
          <thead class="bg-gray-50">
            <tr>
              <th scope="col" class="w-10 px-6 py-3"></th>
              <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">名称</th>
              <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">时间</th>
              <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">状态</th>
              <th scope="col" class="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">操作</th>
            </tr>
          </thead>
          <draggable 
            v-model="localTasks" 
            tag="tbody" 
            item-key="id" 
            handle=".drag-handle"
            class="bg-white divide-y divide-gray-200"
            @end="onDragEnd"
          >
            <template #item="{ element: task }">
              <tr class="hover:bg-gray-50">
                <td class="px-6 py-4 whitespace-nowrap text-gray-400 cursor-move drag-handle">
                  <GripVertical class="w-5 h-5" />
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                  <div class="text-sm font-medium text-gray-900">{{ task.name }}</div>
                  <div class="text-xs text-gray-500 font-mono mt-1">{{ task.task }}</div>
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                  <div class="text-sm font-medium text-gray-900">{{ formatCron(task.cron) }}</div>
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                  <button 
                    @click="emit('toggle', task)"
                    :class="task.is_enabled ? 'bg-green-100 text-green-800 border-green-200' : 'bg-gray-100 text-gray-800 border-gray-200'"
                    class="px-3 py-1 inline-flex items-center text-xs leading-5 font-semibold rounded-full border transition-colors hover:shadow-sm"
                  >
                    <span v-if="task.is_enabled" class="flex items-center"><Play class="w-3 h-3 mr-1" /> 已启用</span>
                    <span v-else class="flex items-center"><Square class="w-3 h-3 mr-1" /> 已停用</span>
                  </button>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                  <div class="flex items-center justify-end space-x-2">
                    <button @click="emit('run', task)" class="inline-flex items-center px-2.5 py-1.5 border border-orange-200 shadow-sm text-xs font-medium rounded text-orange-700 bg-orange-50 hover:bg-orange-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-orange-500 transition-colors" title="立即运行">
                      <Zap class="w-4 h-4 mr-1" />
                      运行
                    </button>
                    <button @click="emit('edit', task)" class="inline-flex items-center px-2.5 py-1.5 border border-blue-200 shadow-sm text-xs font-medium rounded text-blue-700 bg-blue-50 hover:bg-blue-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 transition-colors" title="编辑">
                      <Edit class="w-4 h-4 mr-1" />
                      编辑
                    </button>
                    <button @click="emit('delete', task.id)" class="inline-flex items-center px-2.5 py-1.5 border border-red-200 shadow-sm text-xs font-medium rounded text-red-700 bg-red-50 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500 transition-colors" title="删除">
                      <Trash2 class="w-4 h-4 mr-1" />
                      删除
                    </button>
                  </div>
                </td>
              </tr>
            </template>
            <template #footer>
              <tr v-if="loading && localTasks.length === 0">
                <td colspan="5" class="px-6 py-12 text-center text-gray-500">
                  <RefreshCw class="w-6 h-6 animate-spin mx-auto text-blue-500 mb-2" />
                  加载中...
                </td>
              </tr>
              <tr v-else-if="localTasks.length === 0">
                <td colspan="5" class="px-6 py-12 text-center text-gray-500">
                  暂无计划任务
                </td>
              </tr>
            </template>
          </draggable>
        </table>
      </div>
    </div>
    
    <LoadMore 
      :loading="loading" 
      :no-more-data="!hasMore" 
      :has-data="tasks.length > 0"
      @load-more="emit('loadMore')" 
    />
  </div>
</template>

