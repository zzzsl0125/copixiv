<script setup lang="ts">
import { SwitchRoot, SwitchThumb } from 'reka-ui'

defineProps<{
  modelValue: boolean
  disabled?: boolean
  ariaLabel?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
}>()
</script>

<template>
  <SwitchRoot
    :model-value="modelValue"
    :disabled="disabled"
    :aria-label="ariaLabel"
    class="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 disabled:opacity-50 cursor-pointer"
    :class="modelValue ? 'bg-indigo-600' : 'bg-gray-300'"
    @update:model-value="emit('update:modelValue', $event === true)"
  >
    <SwitchThumb
      class="inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-6 data-[state=unchecked]:translate-x-1"
    />
  </SwitchRoot>
</template>
