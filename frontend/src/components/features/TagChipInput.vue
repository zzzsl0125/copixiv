<script setup lang="ts">
import { computed, ref } from 'vue'
import FormField from '../ui/FormField.vue'

const props = withDefaults(
  defineProps<{
    modelValue: string[]
    label?: string
    placeholder?: string
    mode?: 'chips' | 'live'
    splitPattern?: RegExp
    helperText?: string
    inputTestId?: string
    tagsTestId?: string
    tagRemoveTestIdPrefix?: string
  }>(),
  {
    label: '标签',
    mode: 'chips',
  },
)

const emit = defineEmits<{
  'update:modelValue': [tags: string[]]
}>()

const rawInput = ref('')

const effectivePattern = computed(
  () => props.splitPattern ?? (props.mode === 'live' ? /[,，\s]+/ : /[/|#、\\]/),
)

function parseLiveInput(value: string) {
  const unique = Array.from(
    new Set(
      value
        .split(effectivePattern.value)
        .map((t) => t.trim())
        .filter(Boolean),
    ),
  )
  emit('update:modelValue', unique)
}

function onInput(event: Event) {
  const next = (event.target as HTMLInputElement).value
  rawInput.value = next
  if (props.mode === 'live') {
    parseLiveInput(next)
  }
}

function commitChipInput() {
  if (props.mode !== 'chips') return
  const parts = rawInput.value
    .split(effectivePattern.value)
    .map((t) => t.trim())
    .filter(Boolean)
  if (parts.length === 0) return

  const seen = new Set(props.modelValue.map((t) => t.toLowerCase()))
  const next = [...props.modelValue]
  for (const part of parts) {
    if (seen.has(part.toLowerCase())) continue
    seen.add(part.toLowerCase())
    next.push(part)
  }
  emit('update:modelValue', next)
  rawInput.value = ''
}

function removeTag(tag: string) {
  emit(
    'update:modelValue',
    props.modelValue.filter((t) => t !== tag),
  )
}

function reset() {
  rawInput.value = ''
  if (props.mode === 'live') {
    emit('update:modelValue', [])
  }
}

defineExpose({ reset })
</script>

<template>
  <FormField :label="label">
    <template #default="{ id }">
      <div
        v-if="mode === 'chips' && modelValue.length"
        class="mt-1 mb-2 flex flex-wrap gap-1"
        :data-testid="tagsTestId"
      >
        <span
          v-for="tag in modelValue"
          :key="tag"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700"
        >
          {{ tag }}
          <button
            type="button"
            class="text-blue-400 hover:text-blue-700 cursor-pointer"
            :aria-label="`删除标签 ${tag}`"
            :data-testid="tagRemoveTestIdPrefix ? `${tagRemoveTestIdPrefix}${tag}` : undefined"
            @click="removeTag(tag)"
          >
            ×
          </button>
        </span>
      </div>

      <input
        :id="id"
        :value="rawInput"
        type="text"
        autocomplete="off"
        :placeholder="placeholder"
        :data-testid="inputTestId"
        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
        @input="onInput"
        @keydown.enter="
          (e) => {
            if (mode === 'chips') {
              e.preventDefault()
              commitChipInput()
            }
          }
        "
      />

      <template v-if="mode === 'live'">
        <p v-if="modelValue.length > 0" class="mt-2 flex flex-wrap gap-1" :data-testid="tagsTestId">
          <span
            v-for="tag in modelValue"
            :key="tag"
            class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700"
          >
            {{ tag }}
          </span>
          <span class="text-xs text-gray-400 self-center">共 {{ modelValue.length }} 个</span>
        </p>
        <p v-else-if="helperText" class="mt-1 text-xs text-gray-400">{{ helperText }}</p>
      </template>
    </template>
  </FormField>
</template>
