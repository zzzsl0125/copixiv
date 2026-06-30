<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { taskApi } from '../api'
import type { ScheduledTask } from '../types'
import { usePagination } from '../composables'
import ScheduledTaskList from '../components/features/ScheduledTaskList.vue'
import TaskHistoryList from '../components/features/TaskHistoryList.vue'
import TaskEditModal from '../components/features/TaskEditModal.vue'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'

defineEmits<{ (e: 'toggle-sidebar'): void }>()

const activeTab = ref<'scheduled' | 'history'>('scheduled')

const {
  items: tasks,
  loading: loadingTasks,
  loadData: loadTasks,
} = usePagination(async () => taskApi.getScheduledTasks())

const isModalOpen = ref(false)
const editingTask = ref<ScheduledTask | null>(null)

const {
  items: history,
  loading: loadingHistory,
  loadData: loadHistory,
  hasMore: hasMoreHistory,
} = usePagination((offset, limit) => taskApi.getTaskHistory(limit, offset))

const openModal = (task?: ScheduledTask) => {
  console.log('[Tasks] openModal called, task:', task?.name || 'null (create)')
  editingTask.value = task || null
  isModalOpen.value = true
}

const closeModal = () => {
  console.log('[Tasks] closeModal called')
  console.trace('[Tasks] closeModal stack trace')
  isModalOpen.value = false
  editingTask.value = null
}

const saveTask = async (payload: Record<string, unknown>) => {
  console.log('[Tasks] saveTask called, payload:', payload)
  try {
    if (editingTask.value) {
      await taskApi.updateScheduledTask(editingTask.value.id, payload as unknown as Parameters<typeof taskApi.updateScheduledTask>[1])
    } else {
      await taskApi.createScheduledTask(payload as unknown as Parameters<typeof taskApi.createScheduledTask>[0])
    }
    closeModal()
    loadTasks()
  } catch (err) {
    console.error('Failed to save task:', err)
    alert('保存失败，请检查网络或后端状态')
  }
}

const toggleTask = async (task: ScheduledTask) => {
  try {
    await taskApi.updateScheduledTask(task.id, { is_enabled: !task.is_enabled })
    loadTasks()
  } catch (err) {
    console.error('Failed to toggle task:', err)
    alert('切换状态失败')
  }
}

const deleteTask = async (id: number) => {
  if (!confirm('确定要删除这个计划任务吗？')) return
  try {
    await taskApi.deleteScheduledTask(id)
    loadTasks()
  } catch (err) {
    console.error('Failed to delete task:', err)
    alert('删除失败')
  }
}

const runTask = async (task: ScheduledTask) => {
  try {
    await taskApi.runScheduledTask(task.id)
    alert(`任务 "${task.name}" 已加入队列`)
    if (activeTab.value === 'history') loadHistory()
  } catch (err) {
    console.error('Failed to run task:', err)
    alert('运行失败')
  }
}

const reorderTasks = async (newTasks: ScheduledTask[]) => {
  try {
    await taskApi.reorderScheduledTasks(newTasks.map(t => t.id))
    loadTasks()
  } catch (err) {
    console.error('Failed to reorder tasks:', err)
    alert('排序失败')
    loadTasks()
  }
}

onMounted(() => { loadTasks(); loadHistory() })
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="任务管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader
        :tabs="[{ name: 'scheduled', label: '计划任务' }, { name: 'history', label: '任务队列' }]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'scheduled' | 'history'"
        :add-button-text="activeTab === 'scheduled' ? '新增计划任务' : undefined"
        :show-refresh="true"
        :loading="activeTab === 'scheduled' ? loadingTasks : loadingHistory"
        @add="openModal()"
        @refresh="activeTab === 'scheduled' ? loadTasks() : loadHistory()"
      />

      <div v-if="activeTab === 'scheduled'">
        <ScheduledTaskList :tasks="tasks" :loading="loadingTasks" @edit="openModal" @delete="deleteTask" @toggle="toggleTask" @run="runTask" @reorder="reorderTasks" />
      </div>

      <div v-if="activeTab === 'history'">
        <TaskHistoryList :history="history" :loading="loadingHistory" :has-more="hasMoreHistory" @load-more="() => loadHistory(true)" />
      </div>
    </main>

    <TaskEditModal :is-open="isModalOpen" :task="editingTask" @close="closeModal" @save="saveTask" />
  </div>
</template>
