<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { taskApi, getApiErrorMessage } from '../api'
import type { ScheduledTask } from '../types'
import { usePagination, useToast } from '../composables'
import ScheduledTaskList from '../components/features/ScheduledTaskList.vue'
import TaskHistoryList from '../components/features/TaskHistoryList.vue'
import TaskEditModal from '../components/features/TaskEditModal.vue'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const toast = useToast()
const activeTab = ref<'scheduled' | 'history'>('scheduled')

const {
  items: tasks,
  loading: loadingTasks,
  loadData: loadTasks,
} = usePagination(async () => taskApi.getScheduledTasks())

const isModalOpen = ref(false)
const editingTask = ref<ScheduledTask | null>(null)
const savingTask = ref(false)

const {
  items: history,
  loading: loadingHistory,
  loadData: loadHistory,
  refresh: refreshHistory,
  hasMore: hasMoreHistory,
} = usePagination((offset, limit) => taskApi.getTaskHistory(limit, offset))

const openModal = (task?: ScheduledTask) => {
  editingTask.value = task || null
  isModalOpen.value = true
}

const dismissModal = () => {
  isModalOpen.value = false
  editingTask.value = null
}

const closeModal = () => {
  if (savingTask.value) return
  dismissModal()
}

const saveTask = async (payload: Record<string, unknown>) => {
  if (savingTask.value) return
  savingTask.value = true
  try {
    if (editingTask.value) {
      await taskApi.updateScheduledTask(editingTask.value.id, payload as unknown as Parameters<typeof taskApi.updateScheduledTask>[1])
    } else {
      await taskApi.createScheduledTask(payload as unknown as Parameters<typeof taskApi.createScheduledTask>[0])
    }
    // 保存成功后直接关闭（closeModal 在 savingTask=true 期间会被守卫拦截）
    dismissModal()
    loadTasks()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '保存失败，请检查网络或后端状态'))
  } finally {
    savingTask.value = false
  }
}

const toggleTask = async (task: ScheduledTask) => {
  try {
    await taskApi.updateScheduledTask(task.id, { is_enabled: !task.is_enabled })
    loadTasks()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '切换状态失败'))
  }
}

const deleteTask = async (id: number) => {
  if (!confirm('确定要删除这个计划任务吗？')) return
  try {
    await taskApi.deleteScheduledTask(id)
    loadTasks()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除失败'))
  }
}

const runTask = async (task: ScheduledTask) => {
  try {
    await taskApi.runScheduledTask(task.id)
    toast.success(`任务 "${task.name}" 已加入队列`)
    if (activeTab.value === 'history') loadHistory()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '运行失败'))
  }
}

const reorderTasks = async (newTasks: ScheduledTask[]) => {
  try {
    await taskApi.reorderScheduledTasks(newTasks.map(t => t.id))
    loadTasks()
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '排序失败'))
    loadTasks()
  }
}

onMounted(() => { loadTasks(); loadHistory() })

// ---- live progress: poll the history while any task is pending/running
// (batch_operation rows self-report progress into the `progress` column) ----
const hasRunningTask = computed(() =>
  history.value.some(h => ['pending', 'running'].includes((h.status || '').toLowerCase())),
)

let pollTimer: ReturnType<typeof setInterval> | null = null

watch(hasRunningTask, (running) => {
  if (running && pollTimer === null) {
    // Silent in-place refresh — no list clear, no loading spinner, so the
    // progress updates render without the page flickering.
    pollTimer = setInterval(() => { void refreshHistory({ silent: true }) }, 5000)
  } else if (!running && pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
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

    <TaskEditModal :is-open="isModalOpen" :task="editingTask" :loading="savingTask" @close="closeModal" @save="saveTask" />
  </div>
</template>
