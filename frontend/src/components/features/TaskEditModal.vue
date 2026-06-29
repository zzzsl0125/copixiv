<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue'
import { taskApi } from '../../api'
import type { ScheduledTask, TaskMethod } from '../../types'
import BaseModal from '../ui/BaseModal.vue'
import AppInput from '../ui/AppInput.vue'
import AppCheckbox from '../ui/AppCheckbox.vue'

const props = defineProps<{
  isOpen: boolean
  task: ScheduledTask | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', payload: Record<string, unknown>): void
}>()

const availableMethods = ref<TaskMethod[]>([])
const dynamicParams = ref<Record<string, unknown>>({})
const cronMode = ref<'daily' | 'weekly' | 'monthly' | 'custom'>('daily')
const cronTime = ref({ hour: 0, minute: 0 })
const cronWeekDay = ref(0)
const cronMonthDay = ref(1)
const notifyOnNewNovel = ref(true)

const formState = ref({
  name: '',
  task: '',
  cron: '',
  params: '{}',
  config: '{}',
  is_enabled: true,
})

onMounted(async () => {
  try {
    availableMethods.value = await taskApi.getTaskMethods()
  } catch (err) {
    console.error('Failed to load task methods:', err)
  }
})

const parseCronToUI = (cron: string) => {
  if (!cron) return
  const parts = cron.split(' ')
  if (parts.length !== 5) { cronMode.value = 'custom'; return }

  const [min, hour, dom, month, dow] = parts
  if (dom === '*' && month === '*' && dow === '*') {
    cronMode.value = 'daily'
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) }
  } else if (dom === '*' && month === '*' && dow !== '*') {
    cronMode.value = 'weekly'
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) }
    cronWeekDay.value = parseInt(dow)
  } else if (dom !== '*' && month === '*' && dow === '*') {
    cronMode.value = 'monthly'
    cronTime.value = { hour: parseInt(hour), minute: parseInt(min) }
    cronMonthDay.value = parseInt(dom)
  } else {
    cronMode.value = 'custom'
  }
}

const buildCronFromUI = () => {
  if (cronMode.value === 'custom') return formState.value.cron
  const { minute, hour } = cronTime.value
  if (cronMode.value === 'daily') return `${minute} ${hour} * * *`
  if (cronMode.value === 'weekly') return `${minute} ${hour} * * ${cronWeekDay.value}`
  if (cronMode.value === 'monthly') return `${minute} ${hour} ${cronMonthDay.value} * *`
  return '* * * * *'
}

watch(() => formState.value.task, (newTaskName) => {
  const method = availableMethods.value.find(m => m.name === newTaskName)
  if (method) {
    const currentParams = JSON.parse(formState.value.params || '{}')
    const newParams: Record<string, unknown> = {}
    method.arguments.forEach(arg => {
      newParams[arg.name] = currentParams[arg.name] !== undefined ? currentParams[arg.name] : (arg.default !== null ? arg.default : '')
    })
    dynamicParams.value = newParams
  } else {
    dynamicParams.value = {}
  }
})

watch(dynamicParams, (newVal) => {
  formState.value.params = JSON.stringify(newVal, null, 2)
}, { deep: true })

watch([cronMode, cronTime, cronWeekDay, cronMonthDay], () => {
  if (cronMode.value !== 'custom') formState.value.cron = buildCronFromUI()
}, { deep: true })

watch(notifyOnNewNovel, (newVal) => {
  try {
    const config = JSON.parse(formState.value.config || '{}')
    config.notify_on_new_novel = newVal
    formState.value.config = JSON.stringify(config, null, 2)
  } catch {
    formState.value.config = JSON.stringify({ notify_on_new_novel: newVal }, null, 2)
  }
})

watch(() => props.isOpen, (newVal) => {
  if (newVal) {
    if (props.task) {
      formState.value = {
        name: props.task.name,
        task: props.task.task,
        cron: props.task.cron,
        params: props.task.params ? JSON.stringify(props.task.params, null, 2) : '{}',
        config: props.task.config ? JSON.stringify(props.task.config, null, 2) : '{}',
        is_enabled: props.task.is_enabled,
      }
      try {
        const config = JSON.parse(formState.value.config || '{}')
        notifyOnNewNovel.value = config.notify_on_new_novel !== false
      } catch { notifyOnNewNovel.value = true }
      parseCronToUI(props.task.cron)
      if (props.task.params) dynamicParams.value = { ...props.task.params }
    } else {
      formState.value = { name: '', task: '', cron: '0 0 * * *', params: '{}', config: '{"notify_on_new_novel": true}', is_enabled: true }
      notifyOnNewNovel.value = true
      cronMode.value = 'daily'
      cronTime.value = { hour: 0, minute: 0 }
      dynamicParams.value = {}
    }
  }
})

const handleSave = () => {
  try {
    const paramsObj = JSON.parse(formState.value.params)
    const configObj = JSON.parse(formState.value.config || '{}')
    const method = availableMethods.value.find(m => m.name === formState.value.task)
    if (method) {
      method.arguments.forEach(arg => {
        if (paramsObj[arg.name] !== undefined) {
          if (arg.type === 'int') paramsObj[arg.name] = parseInt(paramsObj[arg.name])
          else if (arg.type === 'float') paramsObj[arg.name] = parseFloat(paramsObj[arg.name])
        }
      })
    }
    emit('save', {
      name: formState.value.name,
      task: formState.value.task,
      cron: formState.value.cron,
      params: paramsObj,
      config: configObj,
      is_enabled: formState.value.is_enabled,
    })
  } catch (err) {
    console.error('Failed to parse params:', err)
    alert('保存失败，请检查参数格式')
  }
}

const selectedMethodArgs = computed(() => {
  const method = availableMethods.value.find(m => m.name === formState.value.task)
  return method ? method.arguments : []
})

const weekDays = [
  { val: 0, label: '周日' }, { val: 1, label: '周一' }, { val: 2, label: '周二' },
  { val: 3, label: '周三' }, { val: 4, label: '周四' }, { val: 5, label: '周五' }, { val: 6, label: '周六' },
]
</script>

<template>
  <BaseModal :is-open="isOpen" :title="task ? '编辑计划任务' : '新增计划任务'" @close="emit('close')" @confirm="handleSave">
    <AppInput v-model="formState.name" label="任务名称" placeholder="给任务起个名字" />
    <div>
      <label class="block text-sm font-medium text-gray-700">任务方法</label>
      <select v-model="formState.task" class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
        <option value="" disabled>选择一个任务方法</option>
        <option v-for="method in availableMethods" :key="method.name" :value="method.name">{{ method.name }}</option>
      </select>
      <p v-if="formState.task" class="mt-1 text-xs text-gray-500">
        {{ availableMethods.find(m => m.name === formState.task)?.description || '无描述' }}
      </p>
    </div>

    <div v-if="selectedMethodArgs.length > 0" class="bg-gray-50 p-3 rounded-md border border-gray-200">
      <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">参数设置</h4>
      <div v-for="arg in selectedMethodArgs" :key="arg.name" class="flex items-center mb-3 last:mb-0">
        <label class="w-1/3 text-sm font-medium text-gray-700">
          {{ arg.name }} <span class="text-xs text-gray-400 font-normal">({{ arg.type }})</span>
          <span v-if="arg.required" class="text-red-500">*</span>
        </label>
        <div class="w-2/3">
          <div v-if="arg.type === 'bool'" class="flex items-center">
            <AppCheckbox :model-value="!!dynamicParams[arg.name]" @update:model-value="dynamicParams[arg.name] = $event" />
            <span class="ml-2 text-sm text-gray-600">{{ dynamicParams[arg.name] ? 'Yes' : 'No' }}</span>
          </div>
          <input v-else-if="arg.type === 'int' || arg.type === 'float'" v-model.number="dynamicParams[arg.name]" type="number" :step="arg.type === 'float' ? '0.1' : '1'" class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
          <input v-else v-model="dynamicParams[arg.name]" type="text" class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm" />
        </div>
      </div>
    </div>

    <div>
      <label class="block text-sm font-medium text-gray-700 mb-2">执行频率</label>
      <div class="flex items-center space-x-4 bg-gray-50 p-3 rounded-md border border-gray-200">
        <div class="flex-shrink-0">
          <select v-model="cronMode" class="block w-28 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
            <option value="daily">每天</option>
            <option value="weekly">每周</option>
            <option value="monthly">每月</option>
            <option value="custom">自定义</option>
          </select>
        </div>
        <div v-if="cronMode !== 'custom'" class="h-6 w-px bg-gray-300"></div>
        <div v-if="cronMode === 'weekly'" class="flex items-center">
          <select v-model="cronWeekDay" class="block w-24 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white">
            <option v-for="d in weekDays" :key="d.val" :value="d.val">{{ d.label }}</option>
          </select>
        </div>
        <div v-if="cronMode === 'monthly'" class="flex items-center">
          <input v-model.number="cronMonthDay" type="number" min="1" max="31" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" />
          <span class="ml-2 text-sm text-gray-600">日</span>
        </div>
        <div v-if="cronMode === 'weekly' || cronMode === 'monthly'" class="h-6 w-px bg-gray-300"></div>
        <div v-if="cronMode !== 'custom'" class="flex items-center">
          <input v-model.number="cronTime.hour" type="number" min="0" max="23" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" placeholder="HH" />
          <span class="mx-1 font-bold text-gray-500">:</span>
          <input v-model.number="cronTime.minute" type="number" min="0" max="59" class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center" placeholder="MM" />
        </div>
        <div v-if="cronMode === 'custom'" class="flex-1">
          <input v-model="formState.cron" type="text" class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm font-mono" placeholder="* * * * *" />
        </div>
      </div>
      <p v-if="cronMode === 'custom'" class="mt-1 text-xs text-gray-500">Cron 格式: 分 时 日 月 周</p>
    </div>

    <div class="pt-2"><AppCheckbox v-model="formState.is_enabled" label="启用此任务" /></div>
    <div class="pt-2"><AppCheckbox v-model="notifyOnNewNovel" label="详细小说列表通知" /></div>
  </BaseModal>
</template>
