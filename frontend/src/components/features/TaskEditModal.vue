<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getApiErrorMessage, taskApi } from '../../api'
import type { ScheduledTask, TaskArgumentField, TaskMethod } from '../../types'
import { useToast } from '../../composables'
import BaseModal from '../ui/BaseModal.vue'
import AppInput from '../ui/AppInput.vue'
import AppCheckbox from '../ui/AppCheckbox.vue'
import CronInput from './CronInput.vue'

const props = defineProps<{
  isOpen: boolean
  task: ScheduledTask | null
  loading?: boolean
}>()

const emit = defineEmits<{
  close: []
  save: [payload: Record<string, unknown>]
}>()

const toast = useToast()
const availableMethods = ref<TaskMethod[]>([])
const dynamicParams = ref<Record<string, unknown>>({})

const formState = ref({
  name: '',
  task: '',
  cron: '',
  is_enabled: true,
})

const methodsLoaded = ref(false)
let methodsLoadingPromise: Promise<void> | null = null

const ensureMethodsLoaded = async () => {
  if (methodsLoaded.value || methodsLoadingPromise) return methodsLoadingPromise
  methodsLoadingPromise = (async () => {
    try {
      availableMethods.value = await taskApi.getTaskMethods()
      methodsLoaded.value = true
    } catch (err: unknown) {
      toast.error(getApiErrorMessage(err, '任务方法加载失败'))
    } finally {
      methodsLoadingPromise = null
    }
  })()
  return methodsLoadingPromise
}

const SOURCE_LABELS: Record<string, string> = {
  pixiv: 'Pixiv',
  esj: 'ESJZone',
  local: '本地',
}

const formatSourceScope = (source?: string | null): string => {
  if (!source) return '通用'
  return SOURCE_LABELS[source] ?? source.toUpperCase()
}

interface MethodGroup {
  key: string
  label: string
  methods: TaskMethod[]
}

const groupedMethods = computed<MethodGroup[]>(() => {
  const common: TaskMethod[] = []
  const bySource = new Map<string, TaskMethod[]>()
  for (const method of availableMethods.value) {
    const src = method.source ?? ''
    if (!src) {
      common.push(method)
      continue
    }
    const bucket = bySource.get(src)
    if (bucket) {
      bucket.push(method)
    } else {
      bySource.set(src, [method])
    }
  }
  const groups: MethodGroup[] = []
  if (common.length > 0) {
    groups.push({ key: 'common', label: '通用任务', methods: common })
  }
  for (const [src, methods] of bySource.entries()) {
    groups.push({
      key: src,
      label: `${formatSourceScope(src)} 专用`,
      methods,
    })
  }
  return groups
})

const selectedMethod = computed(() =>
  availableMethods.value.find((m) => m.name === formState.value.task),
)
const selectedMethodArgs = computed<TaskArgumentField[]>(
  () => selectedMethod.value?.arguments ?? [],
)

const savedParams = (): Record<string, unknown> =>
  props.task && formState.value.task === props.task.task && props.task.params
    ? props.task.params
    : {}

const toFormValue = (value: unknown, type: TaskArgumentField['type']): unknown => {
  if (type === 'array') {
    if (value == null) return ''
    return typeof value === 'string' ? value : JSON.stringify(value)
  }
  if (type === 'boolean') return !!value
  return value == null ? '' : value
}

const defaultFormValue = (arg: TaskArgumentField): unknown => {
  if (arg.default != null) return toFormValue(arg.default, arg.type)
  if (arg.type === 'boolean') return false
  if (arg.type === 'array') return '[]'
  return ''
}

const initialDynamicParams = (taskName: string): Record<string, unknown> => {
  const method = availableMethods.value.find((m) => m.name === taskName)
  if (!method) return {}
  const current = savedParams()
  const next: Record<string, unknown> = {}
  for (const arg of method.arguments) {
    next[arg.name] =
      current[arg.name] !== undefined
        ? toFormValue(current[arg.name], arg.type)
        : defaultFormValue(arg)
  }
  return next
}

watch(
  () => formState.value.task,
  (newTaskName) => {
    dynamicParams.value = initialDynamicParams(newTaskName)
  },
)

watch(availableMethods, () => {
  if (props.isOpen && formState.value.task) {
    dynamicParams.value = initialDynamicParams(formState.value.task)
  }
})

watch(
  () => props.isOpen,
  (newVal) => {
    if (!newVal) return
    void ensureMethodsLoaded()
    if (props.task) {
      formState.value = {
        name: props.task.name,
        task: props.task.task,
        cron: props.task.cron,
        is_enabled: props.task.is_enabled,
      }
      dynamicParams.value = initialDynamicParams(props.task.task)
    } else {
      formState.value = { name: '', task: '', cron: '0 0 * * *', is_enabled: true }
      dynamicParams.value = {}
    }
  },
  { immediate: true },
)

const coerceFormValue = (arg: TaskArgumentField, value: unknown): unknown => {
  if (arg.type === 'array') {
    if (Array.isArray(value)) return value
    const text = typeof value === 'string' ? value.trim() : ''
    return text === '' ? [] : JSON.parse(text)
  }
  if (arg.type === 'boolean') return !!value
  const text = value == null ? '' : String(value)
  if (text === '' && arg.nullable) return null
  if (arg.type === 'integer' || arg.type === 'number') {
    const parsed = arg.type === 'integer' ? parseInt(text, 10) : parseFloat(text)
    if (Number.isNaN(parsed)) throw new Error(`参数 ${arg.name} 必须是数字`)
    return parsed
  }
  return value ?? ''
}

const handleSave = () => {
  try {
    let paramsObj: Record<string, unknown>
    if (selectedMethod.value) {
      paramsObj = {}
      for (const arg of selectedMethodArgs.value) {
        if (arg.name in dynamicParams.value) {
          paramsObj[arg.name] = coerceFormValue(arg, dynamicParams.value[arg.name])
        }
      }
    } else {
      paramsObj = { ...savedParams() }
    }
    emit('save', {
      name: formState.value.name,
      task: formState.value.task,
      cron: formState.value.cron,
      params: paramsObj,
      is_enabled: formState.value.is_enabled,
    })
  } catch (err: unknown) {
    toast.error(getApiErrorMessage(err, '参数格式错误，请检查参数'))
  }
}
</script>

<template>
  <BaseModal
    :is-open="isOpen"
    :title="task ? '编辑计划任务' : '新增计划任务'"
    :loading="loading"
    @close="emit('close')"
    @confirm="handleSave"
  >
    <AppInput v-model="formState.name" label="任务名称" placeholder="给任务起个名字" />
    <div>
      <label class="block text-sm font-medium text-gray-700">任务方法</label>
      <select
        v-model="formState.task"
        class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white"
      >
        <option value="" disabled>选择一个任务方法</option>
        <option v-if="formState.task && !selectedMethod" :value="formState.task" disabled>
          {{ formState.task }}（已移除）
        </option>
        <optgroup v-for="group in groupedMethods" :key="group.key" :label="group.label">
          <option v-for="method in group.methods" :key="method.name" :value="method.name">
            {{ method.name }}
          </option>
        </optgroup>
      </select>
      <div v-if="formState.task" class="mt-1.5 flex items-center gap-2 text-xs text-gray-500">
        <span
          v-if="selectedMethod"
          :class="
            selectedMethod.source
              ? 'bg-blue-50 text-blue-700 border-blue-200'
              : 'bg-gray-100 text-gray-700 border-gray-200'
          "
          class="inline-flex items-center px-2 py-0.5 rounded border font-medium shrink-0"
        >
          {{ selectedMethod.source ? `${formatSourceScope(selectedMethod.source)} 专用` : '通用' }}
        </span>
        <span
          v-else-if="methodsLoaded"
          class="inline-flex items-center px-2 py-0.5 rounded border font-medium shrink-0 bg-amber-50 text-amber-700 border-amber-200"
        >
          已移除
        </span>
        <span>
          {{
            selectedMethod?.description ||
            (methodsLoaded && !selectedMethod ? '该方法已从任务目录移除' : '无描述')
          }}
        </span>
      </div>
    </div>

    <div
      v-if="selectedMethodArgs.length > 0"
      class="bg-gray-50 p-3 rounded-md border border-gray-200"
    >
      <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">参数设置</h4>
      <div
        v-for="arg in selectedMethodArgs"
        :key="arg.name"
        class="flex items-start mb-3 last:mb-0"
      >
        <div class="w-1/3 pr-2">
          <label class="block text-sm font-medium text-gray-700">
            {{ arg.name }} <span class="text-xs text-gray-400 font-normal">({{ arg.type }})</span>
            <span v-if="arg.required" class="text-red-500">*</span>
          </label>
          <p v-if="arg.description" class="mt-0.5 text-xs text-gray-500">{{ arg.description }}</p>
        </div>
        <div class="w-2/3">
          <div v-if="arg.type === 'boolean'" class="flex items-center">
            <AppCheckbox
              :model-value="!!dynamicParams[arg.name]"
              @update:model-value="dynamicParams[arg.name] = $event"
            />
            <span class="ml-2 text-sm text-gray-600">
              {{ dynamicParams[arg.name] ? 'Yes' : 'No' }}
            </span>
          </div>
          <input
            v-else-if="arg.type === 'integer' || arg.type === 'number'"
            v-model.number="dynamicParams[arg.name]"
            type="number"
            :step="arg.type === 'number' ? '0.1' : '1'"
            class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          />
          <input
            v-else
            v-model="dynamicParams[arg.name]"
            type="text"
            class="block w-full border border-gray-300 rounded-md shadow-sm py-1.5 px-3 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
          />
        </div>
      </div>
    </div>

    <CronInput v-model="formState.cron" />

    <div class="pt-2"><AppCheckbox v-model="formState.is_enabled" label="启用此任务" /></div>
  </BaseModal>
</template>
