<script setup lang="ts">
import { ref } from 'vue';
import { api } from '../api';

const props = defineProps<{
  isOpen: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:isOpen', value: boolean): void;
  (e: 'restarting'): void;
  (e: 'restarted'): void;
  (e: 'error', message: string): void;
}>();

const password = ref('');
const isRestarting = ref(false);

const close = () => {
  emit('update:isOpen', false);
  password.value = '';
};

const confirm = async () => {
  if (!password.value || isRestarting.value) return;

  try {
    isRestarting.value = true;
    emit('restarting');
    const pwd = password.value; // Store to clear early
    password.value = '';
    emit('update:isOpen', false); // close modal early
    
    await api.post('/system/restart', { password: pwd });
    emit('restarted');
  } catch (err: any) {
    emit('error', err.response?.data?.detail || err.message);
  } finally {
    isRestarting.value = false;
  }
};
</script>

<template>
  <div v-if="isOpen" class="fixed inset-0 bg-gray-600 bg-opacity-75 flex items-center justify-center z-50">
    <div class="bg-white rounded-lg p-6 shadow-xl w-80 max-w-[90%]">
      <h3 class="text-lg font-medium text-gray-900 mb-4">重启应用</h3>
      <p class="text-sm text-gray-500 mb-4">请输入 sudo 密码以继续</p>
      <input 
        v-model="password" 
        type="password" 
        class="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm mb-4"
        placeholder="sudo 密码"
        @keyup.enter="confirm"
        autocomplete="off"
        :disabled="isRestarting"
      />
      <div class="flex justify-end space-x-3">
        <button 
          @click="close" 
          :disabled="isRestarting"
          class="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-md transition-colors disabled:opacity-50"
        >
          取消
        </button>
        <button 
          @click="confirm" 
          :disabled="isRestarting || !password"
          class="px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-md transition-colors disabled:opacity-50"
        >
          {{ isRestarting ? '重启中...' : '确认' }}
        </button>
      </div>
    </div>
  </div>
</template>
