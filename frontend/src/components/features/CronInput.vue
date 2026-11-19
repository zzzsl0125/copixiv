<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatCronZh } from '../../lib/cron'

const props = defineProps<{
  modelValue: string
}>()

const emit = defineEmits<{
  'update:modelValue': [cron: string]
}>()

const cronMode = ref<'daily' | 'weekly' | 'monthly' | 'custom'>('daily')
const cronTime = ref({ hour: 0, minute: 0 })
const cronWeekDay = ref(0)
const cronMonthDay = ref(1)

const weekDays = [
  { val: 0, label: '周日' },
  { val: 1, label: '周一' },
  { val: 2, label: '周二' },
  { val: 3, label: '周三' },
  { val: 4, label: '周四' },
  { val: 5, label: '周五' },
  { val: 6, label: '周六' },
]

const parseStrictInt = (raw: string, min: number, max: number): number | null => {
  if (!/^\d+$/.test(raw)) return null
  const val = Number(raw)
  return Number.isInteger(val) && val >= min && val <= max ? val : null
}

const parseCronToUI = (cron: string) => {
  const trimmed = cron.trim()
  if (!trimmed) {
    cronMode.value = 'custom'
    return
  }
  const parts = trimmed.split(/\s+/)
  if (parts.length !== 5) {
    cronMode.value = 'custom'
    return
  }

  const [minStr, hourStr, domStr, monthStr, dowStr] = parts
  const minute = parseStrictInt(minStr!, 0, 59)
  const hour = parseStrictInt(hourStr!, 0, 23)
  if (minute === null || hour === null || monthStr !== '*') {
    cronMode.value = 'custom'
    return
  }

  if (domStr === '*' && dowStr === '*') {
    cronMode.value = 'daily'
    cronTime.value = { hour, minute }
    return
  }

  if (domStr === '*') {
    const dow = parseStrictInt(dowStr!, 0, 6)
    if (dow !== null) {
      cronMode.value = 'weekly'
      cronTime.value = { hour, minute }
      cronWeekDay.value = dow
      return
    }
  }

  if (dowStr === '*') {
    const dom = parseStrictInt(domStr!, 1, 31)
    if (dom !== null) {
      cronMode.value = 'monthly'
      cronTime.value = { hour, minute }
      cronMonthDay.value = dom
      return
    }
  }

  cronMode.value = 'custom'
}

const buildCronFromUI = (): string => {
  const { minute, hour } = cronTime.value
  if (cronMode.value === 'daily') return `${minute} ${hour} * * *`
  if (cronMode.value === 'weekly') return `${minute} ${hour} * * ${cronWeekDay.value}`
  if (cronMode.value === 'monthly') return `${minute} ${hour} ${cronMonthDay.value} * *`
  return props.modelValue
}

let lastEmitted: string | null = null

watch(
  () => props.modelValue,
  (next) => {
    if (next === lastEmitted) {
      lastEmitted = null
      return
    }
    parseCronToUI(next)
  },
  { immediate: true },
)

watch(
  [cronMode, cronTime, cronWeekDay, cronMonthDay],
  () => {
    if (cronMode.value === 'custom') return
    const built = buildCronFromUI()
    if (built !== props.modelValue) {
      lastEmitted = built
      emit('update:modelValue', built)
    }
  },
  { deep: true },
)

const onCustomInput = (event: Event) => {
  const val = (event.target as HTMLInputElement).value
  lastEmitted = val
  emit('update:modelValue', val)
}

const cronDescription = computed(() => formatCronZh(props.modelValue))
</script>

<template>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-2">执行频率</label>
    <div class="flex items-center space-x-4 bg-gray-50 p-3 rounded-md border border-gray-200">
      <div class="shrink-0">
        <select
          v-model="cronMode"
          aria-label="执行频率模式"
          class="block w-28 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white"
        >
          <option value="daily">每天</option>
          <option value="weekly">每周</option>
          <option value="monthly">每月</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div v-if="cronMode !== 'custom'" class="h-6 w-px bg-gray-300"></div>
      <div v-if="cronMode === 'weekly'" class="flex items-center">
        <select
          v-model="cronWeekDay"
          aria-label="每周几"
          class="block w-24 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white"
        >
          <option v-for="d in weekDays" :key="d.val" :value="d.val">{{ d.label }}</option>
        </select>
      </div>
      <div v-if="cronMode === 'monthly'" class="flex items-center">
        <input
          v-model.number="cronMonthDay"
          type="number"
          min="1"
          max="31"
          aria-label="每月几日"
          class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center"
        />
        <span class="ml-2 text-sm text-gray-600">日</span>
      </div>
      <div v-if="cronMode === 'weekly' || cronMode === 'monthly'" class="h-6 w-px bg-gray-300"></div>
      <div v-if="cronMode !== 'custom'" class="flex items-center">
        <input
          v-model.number="cronTime.hour"
          type="number"
          min="0"
          max="23"
          aria-label="小时"
          class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center"
          placeholder="HH"
        />
        <span class="mx-1 font-bold text-gray-500">:</span>
        <input
          v-model.number="cronTime.minute"
          type="number"
          min="0"
          max="59"
          aria-label="分钟"
          class="block w-16 border border-gray-300 rounded-md shadow-sm py-1.5 px-2 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm text-center"
          placeholder="MM"
        />
      </div>
      <div v-if="cronMode === 'custom'" class="flex-1">
        <input
          :value="modelValue"
          type="text"
          aria-label="Cron 表达式"
          class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm font-mono"
          placeholder="* * * * *"
          @input="onCustomInput"
        />
      </div>
    </div>
    <p v-if="cronDescription" class="mt-1 text-xs text-blue-600" data-testid="cron-preview">
      调度预览：{{ cronDescription }}
    </p>
    <p v-if="cronMode === 'custom'" class="mt-0.5 text-xs text-gray-500">
      Cron 格式: 分 时 日 月 周
    </p>
  </div>
</template>
