<script setup lang="ts">
import { computed, ref } from 'vue'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/vue-query'
import { getApiErrorMessage, queryKeys, taskApi } from '../api'
import type { ScheduledTask } from '../types'
import { useConfirm, useToast } from '../composables'
import ScheduledTaskList from '../components/features/ScheduledTaskList.vue'
import TaskHistoryList from '../components/features/TaskHistoryList.vue'
import TaskEditModal from '../components/features/TaskEditModal.vue'
import PageHeader from '../components/features/PageHeader.vue'
import SectionHeader from '../components/features/SectionHeader.vue'
import ErrorAlert from '../components/ui/ErrorAlert.vue'

defineOptions({ inheritAttrs: false })
defineEmits<{ (e: 'toggle-sidebar'): void }>()

const toast = useToast()
const { confirm } = useConfirm()
const queryClient = useQueryClient()
const activeTab = ref<'scheduled' | 'history'>('scheduled')
const runningIds = ref<Set<number>>(new Set())
const cancellingIds = ref<Set<number>>(new Set())

const scheduledQuery = useQuery({
  queryKey: queryKeys.tasksScheduled(),
  queryFn: ({ signal }) => taskApi.getScheduledTasks(signal),
})

const methodsQuery = useQuery({
  queryKey: queryKeys.tasksMethods(),
  queryFn: ({ signal }) => taskApi.getTaskMethods(signal),
  staleTime: Number.POSITIVE_INFINITY,
})

const methodSourceMap = computed(() => {
  const map = new Map<string, string | null>()
  for (const method of methodsQuery.data.value ?? []) {
    map.set(method.name, method.source ?? null)
  }
  return map
})

const historyPageSize = 50
const historyQuery = useInfiniteQuery({
  queryKey: queryKeys.tasksHistory(),
  queryFn: ({ pageParam, signal }) => taskApi.getTaskHistory(historyPageSize, pageParam, signal),
  initialPageParam: 0,
  enabled: computed(() => activeTab.value === 'history'),
  getNextPageParam: (lastPage, allPages) => {
    const loaded = allPages.reduce((sum, p) => sum + p.items.length, 0)
    return loaded < lastPage.total ? loaded : undefined
  },
  // Refresh history progress every 5s while on history tab and any loaded row is still active.
  refetchInterval: (q) => {
    if (activeTab.value !== 'history') return false
    const hasActive = q.state.data?.pages.some((p) =>
      p.items.some((i) => i.status === 'pending' || i.status === 'running'),
    )
    return hasActive ? 5000 : false
  },
  refetchIntervalInBackground: false,
})

const tasks = computed(() => scheduledQuery.data.value ?? [])
const loadingTasks = computed(() => scheduledQuery.isPending.value)
const tasksError = computed(() =>
  scheduledQuery.error.value ? getApiErrorMessage(scheduledQuery.error.value, '加载失败') : null,
)

const history = computed(() => historyQuery.data.value?.pages.flatMap((p) => p.items) ?? [])
const loadingHistory = computed(
  () => historyQuery.isPending.value || historyQuery.isFetchingNextPage.value,
)
const historyError = computed(() =>
  historyQuery.error.value ? getApiErrorMessage(historyQuery.error.value, '加载失败') : null,
)
const hasMoreHistory = computed(() => historyQuery.hasNextPage.value)

const isModalOpen = ref(false)
const editingTask = ref<ScheduledTask | null>(null)
const savingTask = ref(false)

const activeError = computed(() =>
  activeTab.value === 'scheduled' ? tasksError.value : historyError.value,
)

const headerLoading = computed(() =>
  activeTab.value === 'scheduled'
    ? scheduledQuery.isFetching.value
    : historyQuery.isFetching.value,
)

const loadTasks = () => scheduledQuery.refetch()
const loadHistory = (loadMore = false) =>
  loadMore ? historyQuery.fetchNextPage() : historyQuery.refetch()

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
      await taskApi.updateScheduledTask(
        editingTask.value.id,
        payload as unknown as Parameters<typeof taskApi.updateScheduledTask>[1],
      )
    } else {
      await taskApi.createScheduledTask(
        payload as unknown as Parameters<typeof taskApi.createScheduledTask>[0],
      )
    }
    dismissModal()
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasksScheduled() })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '保存失败，请检查网络或后端状态'))
  } finally {
    savingTask.value = false
  }
}

const toggleTask = async (task: ScheduledTask) => {
  try {
    await taskApi.updateScheduledTask(task.id, { is_enabled: !task.is_enabled })
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasksScheduled() })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '切换状态失败'))
  }
}

const deleteTask = async (id: number) => {
  const confirmed = await confirm({
    title: '删除计划任务',
    description: '确定要删除这个计划任务吗？',
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await taskApi.deleteScheduledTask(id)
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasksScheduled() })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '删除失败'))
  }
}

const runTask = async (task: ScheduledTask) => {
  if (runningIds.value.has(task.id)) return
  const next = new Set(runningIds.value)
  next.add(task.id)
  runningIds.value = next
  try {
    await taskApi.runScheduledTask(task.id)
    void queryClient.invalidateQueries({ queryKey: queryKeys.tasksHistory() })
    toast.success(`任务 "${task.name}" 已加入队列`)
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '运行失败'))
  } finally {
    const remaining = new Set(runningIds.value)
    remaining.delete(task.id)
    runningIds.value = remaining
  }
}

const cancelTask = async (taskId: number) => {
  if (cancellingIds.value.has(taskId)) return
  const next = new Set(cancellingIds.value)
  next.add(taskId)
  cancellingIds.value = next
  try {
    const res = await taskApi.cancelTask(taskId)
    toast.info(res.status === 'cancelled' ? '已取消任务' : '已发送取消请求')
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasksHistory() })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '取消任务失败'))
  } finally {
    const remaining = new Set(cancellingIds.value)
    remaining.delete(taskId)
    cancellingIds.value = remaining
  }
}

const reorderTasks = async (newTasks: ScheduledTask[]) => {
  queryClient.setQueryData(queryKeys.tasksScheduled(), newTasks)
  try {
    await taskApi.reorderScheduledTasks(newTasks.map((t) => t.id))
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '排序失败'))
  } finally {
    await queryClient.invalidateQueries({ queryKey: queryKeys.tasksScheduled() })
  }
}
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 bg-gray-50">
    <PageHeader title="任务管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow">
      <SectionHeader
        :tabs="[{ name: 'scheduled', label: '计划任务' }, { name: 'history', label: '任务队列' }]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'scheduled' | 'history'"
        :add-button-text="activeTab === 'scheduled' ? '新增计划任务' : undefined"
        :show-refresh="true"
        :loading="headerLoading"
        @add="openModal()"
        @refresh="activeTab === 'scheduled' ? void loadTasks() : void loadHistory()"
      />

      <ErrorAlert
        :message="activeError"
        :retryable="true"
        @retry="activeTab === 'scheduled' ? void loadTasks() : void loadHistory()"
      />

      <div v-if="activeTab === 'scheduled'">
        <ScheduledTaskList
          :tasks="tasks"
          :loading="loadingTasks"
          :running-ids="runningIds"
          :method-source-map="methodSourceMap"
          @edit="openModal"
          @delete="deleteTask"
          @toggle="toggleTask"
          @run="runTask"
          @reorder="reorderTasks"
        />
      </div>

      <div v-if="activeTab === 'history'">
        <TaskHistoryList
          :history="history"
          :loading="loadingHistory"
          :has-more="hasMoreHistory"
          :cancelling-ids="cancellingIds"
          @load-more="() => void loadHistory(true)"
          @cancel="cancelTask"
        />
      </div>
    </main>

    <TaskEditModal
      :is-open="isModalOpen"
      :task="editingTask"
      :loading="savingTask"
      @close="closeModal"
      @save="saveTask"
    />
  </div>
</template>
