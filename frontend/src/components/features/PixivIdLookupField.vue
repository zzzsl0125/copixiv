<script setup lang="ts">
import { computed, watch } from 'vue'
import FormField from '../ui/FormField.vue'
import { formatIdLookupHint, useIdLookup } from '../../composables'

export interface LookupResult {
  name?: string | null
  author_name?: string | null
  series_name?: string | null
  novel_count: number
}

const UNLINK_PLACEHOLDER_ID = 1

const props = withDefaults(
  defineProps<{
    modelValue: string
    entity: '作者' | '系列'
    label: string
    fetch: (id: number) => Promise<LookupResult>
    currentId?: number | null
    currentName?: string
    disabled?: boolean
    showHint?: boolean
    placeholder?: string
    emptyNote: string
    unlinkNote: string
    allowUnlink?: boolean
    unlinked?: boolean
    unlinkLabel?: string
    inputTestId?: string
    hintTestId?: string
    unlinkTestId?: string
  }>(),
  {
    currentId: null,
    currentName: '',
    disabled: false,
    showHint: true,
    allowUnlink: false,
    unlinked: false,
    unlinkLabel: '',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:unlinked': [value: boolean]
  'update:idNumber': [value: number | null | undefined]
}>()

const inputProxy = computed({
  get: () => props.modelValue,
  set: (val: string) => emit('update:modelValue', val),
})

const isInputDisabled = computed(() => props.disabled || (props.allowUnlink && props.unlinked))

const lookup = useIdLookup<LookupResult>({
  input: inputProxy,
  currentId: () => props.currentId ?? null,
  fetch: props.fetch,
  enabled: () => !isInputDisabled.value,
})

watch(
  lookup.idNumber,
  (val) => {
    emit('update:idNumber', val)
  },
  { immediate: true },
)

watch(
  () => [props.disabled, props.unlinked],
  () => {
    lookup.schedule()
  },
)

const hint = computed(() => {
  if (!props.showHint) return undefined
  const unlinking = props.allowUnlink && props.unlinked
  return formatIdLookupHint({
    entity: props.entity,
    idNumber: unlinking ? null : lookup.idNumber.value,
    currentId: unlinking ? UNLINK_PLACEHOLDER_ID : (props.currentId ?? null),
    currentName: props.currentName || '未知',
    loading: lookup.loading.value,
    failed: lookup.failed.value,
    found: lookup.result.value
      ? {
          name:
            lookup.result.value.name ??
            lookup.result.value.author_name ??
            lookup.result.value.series_name,
          novelCount: lookup.result.value.novel_count,
        }
      : null,
    emptyNote: props.emptyNote,
    unlinkNote: props.unlinkNote,
  })
})

function onInput(event: Event) {
  emit('update:modelValue', (event.target as HTMLInputElement).value)
}

function onUnlinkChange(event: Event) {
  emit('update:unlinked', (event.target as HTMLInputElement).checked)
}

defineExpose({
  idNumber: lookup.idNumber,
  hint,
  schedule: lookup.schedule,
})
</script>

<template>
  <FormField
    :label="label"
    :disabled="disabled"
    :hint="hint?.text"
    :hint-tone="hint?.tone"
    :hint-test-id="hintTestId"
  >
    <template #default="{ id }">
      <input
        :id="id"
        :value="modelValue"
        type="text"
        inputmode="numeric"
        autocomplete="off"
        :placeholder="placeholder"
        :disabled="isInputDisabled"
        :data-testid="inputTestId"
        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm disabled:bg-gray-100 disabled:text-gray-500 disabled:cursor-not-allowed"
        @input="onInput"
      />
      <label
        v-if="allowUnlink"
        class="mt-1.5 inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer"
      >
        <input
          type="checkbox"
          :checked="unlinked"
          :data-testid="unlinkTestId"
          class="h-3.5 w-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
          @change="onUnlinkChange"
        />
        {{ unlinkLabel || `解除${entity}关联` }}
      </label>
    </template>
  </FormField>
</template>
