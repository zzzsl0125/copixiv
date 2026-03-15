<script setup lang="ts">
import { ref, watch } from 'vue';
import { type ScheduledTask } from '../api';
import { Play, Square, Edit, Trash2, Zap } from 'lucide-vue-next';
import LoadMore from './LoadMore.vue';
import DraggableTable, { type TableColumn } from './DraggableTable.vue';

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

const onDragEnd = (reorderedTasks: ScheduledTask[]) => {
  emit('reorder', reorderedTasks);
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
  
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return `每天 ${hour}:${minute.padStart(2, '0')}`;
  }
  
  if (dayOfMonth === '*' && month === '*' && dayOfWeek !== '*') {
    const days = ['日', '一', '二', '三', '四', '五', '六', '日'];
    const dayStr = days[parseInt(dayOfWeek)];
    if (dayStr) {
      return `每周${dayStr} ${hour}:${minute.padStart(2, '0')}`;
    }
  }
  
  if (dayOfMonth !== '*' && month === '*' && dayOfWeek === '*') {
    return `每月${dayOfMonth}日 ${hour}:${minute.padStart(2, '0')}`;
  }
  
  return cron;
};

const columns: TableColumn[] = [
  { key: 'name', label: '名称' },
  { key: 'cron', label: '时间' },
  { key: 'status', label: '状态' },
  { key: 'actions', label: '操作', align: 'right' }
];
</script>

<template>
  <div>
    <DraggableTable 
      :items="tasks" 
      :columns="columns"
      :loading="loading" 
      empty-text="暂无计划任务"
      @reorder="onDragEnd"
    >
      <template #name="{ item: task }">
        <div class="text-sm font-medium text-gray-900">{{ task.name }}</div>
        <div class="text-xs text-gray-500 font-mono mt-1">{{ task.task }}</div>
      </template>
      
      <template #cron="{ item: task }">
        <div class="text-sm font-medium text-gray-900">{{ formatCron(task.cron) }}</div>
      </template>
      
      <template #status="{ item: task }">
        <button 
          @click="emit('toggle', task)"
          :class="task.is_enabled ? 'bg-green-100 text-green-800 border-green-200' : 'bg-gray-100 text-gray-800 border-gray-200'"
          class="px-3 py-1 inline-flex items-center text-xs leading-5 font-semibold rounded-full border transition-colors hover:shadow-sm"
        >
          <span v-if="task.is_enabled" class="flex items-center"><Play class="w-3 h-3 mr-1" /> 已启用</span>
          <span v-else class="flex items-center"><Square class="w-3 h-3 mr-1" /> 已停用</span>
        </button>
      </template>
      
      <template #actions="{ item: task }">
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
      </template>
    </DraggableTable>
    
    <LoadMore 
      :loading="loading" 
      :no-more-data="!hasMore" 
      :has-data="tasks.length > 0"
      @load-more="emit('loadMore')" 
    />
  </div>
</template>

