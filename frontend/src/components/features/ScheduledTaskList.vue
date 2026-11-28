<script setup lang="ts">
import { Play, Square, Edit, Trash2, Zap } from '@lucide/vue'
import DraggableTable, { type TableColumn } from './DraggableTable.vue'
import { formatCronZh } from '../../lib/cron'
import type { ScheduledTask } from '../../types'

const props = defineProps<{
  tasks: ScheduledTask[]
  loading: boolean
  runningIds?: ReadonlySet<number>
  methodSourceMap?: ReadonlyMap<string, string | null>
}>()

const emit = defineEmits<{
  edit: [task: ScheduledTask]
  delete: [id: number]
  toggle: [task: ScheduledTask]
  run: [task: ScheduledTask]
  reorder: [tasks: ScheduledTask[]]
}>()

const columns: TableColumn[] = [
  { key: 'name', label: '名称' },
  { key: 'cron', label: '时间' },
  { key: 'status', label: '状态' },
  { key: 'actions', label: '操作', align: 'right' },
]

const isRunning = (task: ScheduledTask) => props.runningIds?.has(task.id) ?? false

const SOURCE_LABELS: Record<string, string> = {
  pixiv: 'Pixiv',
  esj: 'ESJZone',
  local: '本地',
}

const methodScopeBadge = (taskName: string): { label: string; classes: string } | null => {
  const map = props.methodSourceMap
  if (!map || map.size === 0) return null
  if (!map.has(taskName)) {
    return { label: '已移除', classes: 'bg-amber-50 text-amber-700 border-amber-200' }
  }
  const src = map.get(taskName)
  if (!src) {
    return { label: '通用', classes: 'bg-gray-100 text-gray-700 border-gray-200' }
  }
  return {
    label: SOURCE_LABELS[src] ?? src.toUpperCase(),
    classes: 'bg-blue-50 text-blue-700 border-blue-200',
  }
}
</script>

<template>
  <div>
    <DraggableTable
      :items="tasks"
      :columns="columns"
      :loading="loading"
      empty-text="暂无计划任务"
      @reorder="emit('reorder', $event)"
    >
      <template #name="{ item: task }">
        <div class="text-sm font-medium text-gray-900">{{ task.name }}</div>
        <div class="flex items-center gap-2 mt-1">
          <span class="text-xs text-gray-500 font-mono">{{ task.task }}</span>
          <span
            v-if="methodScopeBadge(task.task)"
            :class="methodScopeBadge(task.task)!.classes"
            class="px-1.5 py-0.5 text-[11px] leading-none font-medium rounded border"
          >
            {{ methodScopeBadge(task.task)!.label }}
          </span>
        </div>
      </template>
      <template #cron="{ item: task }">
        <div class="text-sm font-medium text-gray-900" :title="task.cron">
          {{ formatCronZh(task.cron) }}
        </div>
      </template>
      <template #status="{ item: task }">
        <button
          @click="emit('toggle', task)"
          :class="task.is_enabled ? 'bg-green-100 text-green-800 border-green-200' : 'bg-gray-100 text-gray-800 border-gray-200'"
          class="px-3 py-1 inline-flex items-center text-xs leading-5 font-semibold rounded-full border transition-colors cursor-pointer"
        >
          <span v-if="task.is_enabled" class="flex items-center"><Play class="w-3 h-3 mr-1" /> 已启用</span>
          <span v-else class="flex items-center"><Square class="w-3 h-3 mr-1" /> 已停用</span>
        </button>
      </template>
      <template #actions="{ item: task }">
        <div class="flex items-center justify-end space-x-2">
          <button
            @click="emit('run', task)"
            :disabled="isRunning(task)"
            :title="isRunning(task) ? '入队中…' : '立即运行'"
            class="inline-flex items-center px-2.5 py-1.5 border border-orange-200 shadow-sm text-xs font-medium rounded text-orange-700 bg-orange-50 hover:bg-orange-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-orange-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            <Zap class="w-4 h-4 mr-1" /> 运行
          </button>
          <button
            @click="emit('edit', task)"
            class="inline-flex items-center px-2.5 py-1.5 border border-blue-200 shadow-sm text-xs font-medium rounded text-blue-700 bg-blue-50 hover:bg-blue-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 transition-colors cursor-pointer"
            title="编辑"
          >
            <Edit class="w-4 h-4 mr-1" /> 编辑
          </button>
          <button
            @click="emit('delete', task.id)"
            class="inline-flex items-center px-2.5 py-1.5 border border-red-200 shadow-sm text-xs font-medium rounded text-red-700 bg-red-50 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500 transition-colors cursor-pointer"
            title="删除"
          >
            <Trash2 class="w-4 h-4 mr-1" /> 删除
          </button>
        </div>
      </template>
    </DraggableTable>
  </div>
</template>
