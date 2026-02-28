<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { taskApi, type ScheduledTask } from '../api';
import { usePagination } from '../composables/usePagination';
import { useLocalPagination } from '../composables/useLocalPagination';
import { RefreshCw, Plus } from 'lucide-vue-next';
import ScheduledTaskList from '../components/ScheduledTaskList.vue';
import TaskHistoryList from '../components/TaskHistoryList.vue';
import TaskEditModal from '../components/TaskEditModal.vue';
import AppLogo from '../components/AppLogo.vue';

const router = useRouter();

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

const goToHome = () => {
  router.push('/');
};

onMounted(() => {
  loadTasks();
  loadHistory();
});
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 h-full bg-gray-50">
    <div class="bg-white shadow-sm border-b border-gray-200 z-10 sticky top-0">
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex items-center justify-between h-16">
          <div class="flex items-center gap-4">
            <AppLogo @click="goToHome" />
            <span class="text-gray-500">/</span>
            <span class="text-gray-700 font-medium">任务管理</span>
          </div>
        </div>
      </div>
    </div>

    <main class="w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-grow overflow-auto">
      <div class="mb-6 border-b border-gray-200 flex justify-between items-center">
        <nav class="-mb-px flex space-x-8" aria-label="Tabs">
          <button
            @click="activeTab = 'scheduled'"
            :class="[
              activeTab === 'scheduled' ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
              'whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors'
            ]"
          >
            计划任务
          </button>
          <button
            @click="activeTab = 'history'"
            :class="[
              activeTab === 'history' ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
              'whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors'
            ]"
          >
            任务队列
          </button>
        </nav>
        <div class="flex items-center space-x-4">
          <button v-if="activeTab === 'scheduled'" @click="() => openModal()" class="flex items-center px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors shadow-sm text-sm font-medium">
            <Plus class="w-4 h-4 mr-2" />
            新增计划任务
          </button>
          <div class="flex space-x-2">
            <button v-if="activeTab === 'scheduled'" @click="() => loadTasks()" class="p-2 text-gray-500 hover:text-blue-600 rounded-full hover:bg-gray-100 transition-colors">
              <RefreshCw class="w-5 h-5" :class="{ 'animate-spin': loadingTasks }" />
            </button>
            <button v-if="activeTab === 'history'" @click="() => loadHistory()" class="p-2 text-gray-500 hover:text-blue-600 rounded-full hover:bg-gray-100 transition-colors">
              <RefreshCw class="w-5 h-5" :class="{ 'animate-spin': loadingHistory }" />
            </button>
          </div>
        </div>
      </div>

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
