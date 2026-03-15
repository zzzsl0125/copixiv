<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { taskApi, type ScheduledTask } from '../api';
import { usePagination } from '../composables/usePagination';
import { useLocalPagination } from '../composables/useLocalPagination';
import ScheduledTaskList from '../components/ScheduledTaskList.vue';
import TaskHistoryList from '../components/TaskHistoryList.vue';
import TaskEditModal from '../components/TaskEditModal.vue';
import PageHeader from '../components/PageHeader.vue';
import SectionHeader from '../components/SectionHeader.vue';

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const activeTab = ref<'scheduled' | 'history'>('scheduled');

// --- Scheduled Tasks State ---
const { 
  items: tasks, 
  loading: loadingTasks, 
  loadData: loadTasks, 
  loadMore: loadMoreTasks, 
  hasMore: hasMoreTasks 
} = useLocalPagination(taskApi.getScheduledTasks);

// Modal state
const isModalOpen = ref(false);
const editingTask = ref<ScheduledTask | null>(null);

// --- History State ---
const { 
  items: history, 
  loading: loadingHistory, 
  loadData: loadHistory, 
  hasMore: hasMoreHistory 
} = usePagination((offset, limit) => taskApi.getTaskHistory(limit, offset));

// --- Methods ---

const openModal = (task?: ScheduledTask) => {
  editingTask.value = task || null;
  isModalOpen.value = true;
};

const closeModal = () => {
  isModalOpen.value = false;
  editingTask.value = null;
};

const saveTask = async (payload: any) => {
  try {
    if (editingTask.value) {
      await taskApi.updateScheduledTask(editingTask.value.id, payload);
    } else {
      await taskApi.createScheduledTask(payload);
    }
    closeModal();
    loadTasks();
  } catch (error) {
    console.error('Failed to save task:', error);
    alert('保存失败，请检查网络或后端状态');
  }
};

const toggleTask = async (task: ScheduledTask) => {
  try {
    await taskApi.updateScheduledTask(task.id, { is_enabled: !task.is_enabled });
    loadTasks();
  } catch (error) {
    console.error('Failed to toggle task:', error);
    alert('切换状态失败');
  }
};

const deleteTask = async (id: number) => {
  if (!confirm('确定要删除这个计划任务吗？')) return;
  try {
    await taskApi.deleteScheduledTask(id);
    loadTasks();
  } catch (error) {
    console.error('Failed to delete task:', error);
    alert('删除失败');
  }
};

const runTask = async (task: ScheduledTask) => {
  try {
    await taskApi.runScheduledTask(task.id);
    alert(`任务 "${task.name}" 已加入队列`);
    // Refresh history if active
    if (activeTab.value === 'history') {
       loadHistory();
    }
  } catch (error) {
    console.error('Failed to run task:', error);
    alert('运行失败');
  }
};

const reorderTasks = async (newTasks: ScheduledTask[]) => {
  try {
    const taskIds = newTasks.map(t => t.id);
    await taskApi.reorderScheduledTasks(taskIds);
    loadTasks();
  } catch (error) {
    console.error('Failed to reorder tasks:', error);
    alert('排序失败');
    loadTasks(); // Reload to revert order visually
  }
};

onMounted(() => {
  loadTasks();
  loadHistory();
});
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <PageHeader title="任务管理" @toggle-sidebar="$emit('toggle-sidebar')" />

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <SectionHeader 
        :tabs="[
          { name: 'scheduled', label: '计划任务' },
          { name: 'history', label: '任务队列' }
        ]"
        :active-tab="activeTab"
        @update:active-tab="activeTab = $event as 'scheduled' | 'history'"
        :add-button-text="activeTab === 'scheduled' ? '新增计划任务' : undefined"
        :show-refresh="true"
        :loading="activeTab === 'scheduled' ? loadingTasks : loadingHistory"
        @add="openModal()"
        @refresh="activeTab === 'scheduled' ? loadTasks() : loadHistory()"
      >
      </SectionHeader>

      <!-- Scheduled Tasks Tab -->
      <div v-if="activeTab === 'scheduled'">
        <ScheduledTaskList 
          :tasks="tasks" 
          :loading="loadingTasks" 
          :has-more="hasMoreTasks" 
          @load-more="loadMoreTasks" 
          @edit="openModal" 
          @delete="deleteTask"
          @toggle="toggleTask"
          @run="runTask"
          @reorder="reorderTasks"
        />
      </div>

      <!-- Task History Tab -->
      <div v-if="activeTab === 'history'">
        <TaskHistoryList 
          :history="history" 
          :loading="loadingHistory" 
          :has-more="hasMoreHistory" 
          @load-more="() => loadHistory(true)" 
        />
      </div>
    </main>

    <!-- Modal for Create/Edit Scheduled Task -->
    <TaskEditModal 
      :is-open="isModalOpen" 
      :task="editingTask" 
      @close="closeModal" 
      @save="saveTask" 
    />

  </div>
</template>
