<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue';
import { type ScheduledTask, type TaskMethod, taskApi } from '../api';

const props = defineProps<{
  isOpen: boolean;
  task: ScheduledTask | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'save', payload: any): void;
}>();

const availableMethods = ref<TaskMethod[]>([]);
const dynamicParams = ref<Record<string, any>>({});
const cronMode = ref<'daily' | 'weekly' | 'monthly' | 'custom'>('daily');
const cronTime = ref({ hour: 0, minute: 0 });
const cronWeekDay = ref(0); // 0-6 (Sun-Sat)
const cronMonthDay = ref(1); // 1-31

const formState = ref({
  name: '',
  task: '',
  cron: '',
  params: '{}',
  is_enabled: true
});

onMounted(async () => {
  try {
    availableMethods.value = await taskApi.getTaskMethods();
  } catch (error) {
    console.error('Failed to load task methods:', error);
  }
});

// Helper to parse cron string back to UI state
const parseCronToUI = (cron: string) => {
  if (!cron) return;
  
  const parts = cron.split(' ');
  if (parts.length !== 5) {
    cronMode.value = 'custom';
    return;
  }

  const min = parts[0] || '0';
  const hour = parts[1] || '0';
  const dom = parts[2] || '*';
  const month = parts[3] || '*';
  const dow = parts[4] || '*';
  
  // Try to match patterns
  // Daily: m h * * *
  if (dom === '*' && month === '*' && dow === '*') {
    cronMode.value = 'daily';
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) };
  }
  // Weekly: m h * * w
  else if (dom === '*' && month === '*' && dow !== '*') {
    cronMode.value = 'weekly';
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) };
    cronWeekDay.value = parseInt(dow);
  }
  // Monthly: m h d * *
  else if (dom !== '*' && month === '*' && dow === '*') {
    cronMode.value = 'monthly';
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) };
    cronMonthDay.value = parseInt(dom);
  }
  else {
    cronMode.value = 'custom';
  }
};

// Helper to build cron string from UI state
const buildCronFromUI = () => {
  if (cronMode.value === 'custom') return formState.value.cron;

  const { minute, hour } = cronTime.value;
  
  if (cronMode.value === 'daily') {
    return `${minute} ${hour} * * *`;
  }
  if (cronMode.value === 'weekly') {
    return `${minute} ${hour} * * ${cronWeekDay.value}`;
  }
  if (cronMode.value === 'monthly') {
    return `${minute} ${hour} ${cronMonthDay.value} * *`;
  }
  return '* * * * *';
};

// Watchers to update dynamic params when task changes
watch(() => formState.value.task, (newTaskName) => {
  const method = availableMethods.value.find(m => m.name === newTaskName);
  if (method) {
    // Initialize params with defaults if not present
    const currentParams = JSON.parse(formState.value.params || '{}');
    const newParams: Record<string, any> = {};
    
    method.arguments.forEach(arg => {
      if (currentParams[arg.name] !== undefined) {
        newParams[arg.name] = currentParams[arg.name];
      } else {
        newParams[arg.name] = arg.default !== null ? arg.default : '';
      }
    });
    dynamicParams.value = newParams;
  } else {
    dynamicParams.value = {};
  }
});

// Watch dynamic params to update JSON string
watch(dynamicParams, (newVal) => {
  formState.value.params = JSON.stringify(newVal, null, 2);
}, { deep: true });

// Watch UI cron elements to update string
watch([cronMode, cronTime, cronWeekDay, cronMonthDay], () => {
  if (cronMode.value !== 'custom') {
    formState.value.cron = buildCronFromUI();
  }
}, { deep: true });


watch(() => props.isOpen, (newVal) => {
  if (newVal) {
    if (props.task) {
      formState.value = {
        name: props.task.name,
        task: props.task.task,
        cron: props.task.cron,
        params: props.task.params ? JSON.stringify(props.task.params, null, 2) : '{}',
        is_enabled: props.task.is_enabled
      };
      
      // Parse existing cron to UI
      parseCronToUI(props.task.cron);
      
      // Initialize dynamic params from existing JSON
      if (props.task.params) {
        dynamicParams.value = { ...props.task.params };
      }

    } else {
      // Default state for new task
      formState.value = {
        name: '',
        task: '',
        cron: '0 0 * * *',
        params: '{}',
        is_enabled: true
      };
      cronMode.value = 'daily';
      cronTime.value = { hour: 0, minute: 0 };
      dynamicParams.value = {};
    }
  }
});

const handleSave = () => {
  try {
    const paramsObj = JSON.parse(formState.value.params);
    
    // Convert types based on method definition
    const method = availableMethods.value.find(m => m.name === formState.value.task);
    if (method) {
      method.arguments.forEach(arg => {
        if (paramsObj[arg.name] !== undefined) {
          if (arg.type === 'int') {
            paramsObj[arg.name] = parseInt(paramsObj[arg.name]);
          } else if (arg.type === 'float') {
            paramsObj[arg.name] = parseFloat(paramsObj[arg.name]);
          } else if (arg.type === 'bool') {
             // Handle boolean conversion if it came from string input or checkbox
             // It's likely already boolean if bound to checkbox, but safe to check
          }
        }
      });
    }

    const payload = {
      name: formState.value.name,
      task: formState.value.task,
      cron: formState.value.cron,
      params: paramsObj,
      is_enabled: formState.value.is_enabled
    };
    
    emit('save', payload);
  } catch (error) {
    console.error('Failed to parse params:', error);
    alert('保存失败，请检查参数格式');
  }
};

const selectedMethodArgs = computed(() => {
  const method = availableMethods.value.find(m => m.name === formState.value.task);
  return method ? method.arguments : [];
});

const weekDays = [
  { val: 0, label: '周日' },
  { val: 1, label: '周一' },
  { val: 2, label: '周二' },
  { val: 3, label: '周三' },
  { val: 4, label: '周四' },
  { val: 5, label: '周五' },
  { val: 6, label: '周六' },
];
</script>

<template>
  <div v-if="isOpen" class="fixed inset-0 z-50 overflow-y-auto" aria-labelledby="modal-title" role="dialog" aria-modal="true">
    <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
      <div class="fixed inset-0 bg-gray-500 bg-opacity-75 transition-opacity" @click="emit('close')" aria-hidden="true"></div>

      <span class="hidden sm:inline-block sm:align-middle sm:h-screen" aria-hidden="true">&#8203;</span>
      
      <div class="inline-block align-bottom bg-white rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
        <div class="bg-white px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
          <div class="sm:flex sm:items-start">
            <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left w-full">
              <h3 class="text-lg leading-6 font-medium text-gray-900" id="modal-title">
                {{ task ? '编辑计划任务' : '新增计划任务' }}
              </h3>
              
              <div class="mt-6 space-y-6">
                <!-- Task Name -->
                <div>
                  <label class="block text-sm font-medium text-gray-700">任务名称</label>
                  <input v-model="formState.name" type="text" class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" placeholder="给任务起个名字" />
                </div>

                <!-- Task Method Selection -->
                <div>
                  <label class="block text-sm font-medium text-gray-700">任务方法</label>
                  <select v-model="formState.task" class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
                    <option value="" disabled>选择一个任务方法</option>
                    <option v-for="method in availableMethods" :key="method.name" :value="method.name">
                      {{ method.name }}
                    </option>
                  </select>
                  <p v-if="formState.task" class="mt-1 text-xs text-gray-500">
                    {{ availableMethods.find(m => m.name === formState.task)?.description || '无描述' }}
                  </p>
                </div>

                <!-- Dynamic Parameters -->
                <div v-if="selectedMethodArgs.length > 0" class="bg-gray-50 p-3 rounded-md border border-gray-200">
                  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">参数设置</h4>
                  <div v-for="arg in selectedMethodArgs" :key="arg.name" class="flex items-center mb-3 last:mb-0">
                    <label class="w-1/3 text-sm font-medium text-gray-700">
                      {{ arg.name }} 
                      <span class="text-xs text-gray-400 font-normal">({{ arg.type }})</span>
                      <span v-if="arg.required" class="text-red-500">*</span>
                    </label>
                    
                    <div class="w-2/3">
                      <!-- Boolean Input -->
                      <div v-if="arg.type === 'bool'" class="flex items-center">
                         <input v-model="dynamicParams[arg.name]" type="checkbox" class="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded" />
                         <span class="ml-2 text-sm text-gray-600">{{ dynamicParams[arg.name] ? 'Yes' : 'No' }}</span>
                      </div>
                      
                      <!-- Number Input -->
                      <input v-else-if="arg.type === 'int' || arg.type === 'float'" 
                             v-model.number="dynamicParams[arg.name]" 
                             type="number" 
                             :step="arg.type === 'float' ? '0.1' : '1'"
                             class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
                      
                      <!-- String/Default Input -->
                      <input v-else 
                             v-model="dynamicParams[arg.name]" 
                             type="text" 
                             class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
                    </div>
                  </div>
                </div>

                <!-- Frequency / Time -->
                <div>
                   <label class="block text-sm font-medium text-gray-700 mb-2">执行频率</label>
                   
                   <div class="flex items-center space-x-4 bg-gray-50 p-3 rounded-md border border-gray-200">
                     <!-- Mode Selector (Part 1) -->
                     <div class="flex-shrink-0">
                       <select v-model="cronMode" class="block w-28 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
                         <option value="daily">每天</option>
                         <option value="weekly">每周</option>
                         <option value="monthly">每月</option>
                         <option value="custom">自定义</option>
                       </select>
                     </div>
                     
                     <!-- Separator line if not custom -->
                     <div v-if="cronMode !== 'custom'" class="h-6 w-px bg-gray-300"></div>
                     
                     <!-- Day/Condition Selector (Part 2) -->
                     <div v-if="cronMode === 'weekly' || cronMode === 'monthly'" class="flex items-center">
                        <div v-if="cronMode === 'weekly'" class="flex items-center">
                          <select v-model="cronWeekDay" class="block w-24 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
                            <option v-for="d in weekDays" :key="d.val" :value="d.val">{{ d.label }}</option>
                          </select>
                        </div>
  
                        <div v-if="cronMode === 'monthly'" class="flex items-center">
                          <input v-model.number="cronMonthDay" type="number" min="1" max="31" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" />
                          <span class="ml-2 text-sm text-gray-600">日</span>
                        </div>
                     </div>
                     
                     <!-- Separator line if week/month -->
                     <div v-if="cronMode === 'weekly' || cronMode === 'monthly'" class="h-6 w-px bg-gray-300"></div>

                     <!-- Time Selector (Part 3) -->
                     <div v-if="cronMode !== 'custom'" class="flex items-center">
                        <input v-model.number="cronTime.hour" type="number" min="0" max="23" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" placeholder="HH" />
                        <span class="mx-1 font-bold text-gray-500">:</span>
                        <input v-model.number="cronTime.minute" type="number" min="0" max="59" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" placeholder="MM" />
                     </div>
                     
                     <!-- Custom Input (replaces 2 and 3) -->
                     <div v-if="cronMode === 'custom'" class="flex-1">
                       <input v-model="formState.cron" type="text" class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm font-mono" placeholder="* * * * *" />
                     </div>
                   </div>
                   <p v-if="cronMode === 'custom'" class="mt-1 text-xs text-gray-500">Cron 格式: 分 时 日 月 周</p>
                </div>
                
                <!-- Enable Toggle -->
                <div class="flex items-center pt-2">
                  <input v-model="formState.is_enabled" id="is_enabled" type="checkbox" class="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded" />
                  <label for="is_enabled" class="ml-2 block text-sm text-gray-900">
                    启用此任务
                  </label>
                </div>

              </div>
            </div>
          </div>
        </div>
        <div class="bg-gray-50 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
          <button @click="handleSave" type="button" class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-blue-600 text-base font-medium text-white hover:bg-blue-700 focus:outline-none sm:ml-3 sm:w-auto sm:text-sm transition-colors">
            保存
          </button>
          <button @click="emit('close')" type="button" class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 shadow-sm px-4 py-2 bg-white text-base font-medium text-gray-700 hover:bg-gray-50 focus:outline-none sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm transition-colors">
            取消
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
