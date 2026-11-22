<script setup lang="ts">
import { useId } from 'vue'
import { Label } from 'reka-ui'

defineProps<{
  label?: string
  hint?: string
  hintTone?: 'muted' | 'ok' | 'warn' | 'error'
  disabled?: boolean
  hintTestId?: string
}>()

const fieldId = useId()
</script>

<template>
  <div class="min-w-0">
    <Label
      v-if="label"
      :for="fieldId"
      class="block text-sm font-medium"
      :class="disabled ? 'text-gray-400' : 'text-gray-700'"
    >
      {{ label }}
    </Label>
    <slot :id="fieldId"></slot>
    <p
      v-if="hint"
      class="mt-0.5 text-xs"
      :class="{
        'text-gray-400': !hintTone || hintTone === 'muted',
        'text-green-600': hintTone === 'ok',
        'text-amber-600': hintTone === 'warn',
        'text-red-600': hintTone === 'error',
      }"
      :data-testid="hintTestId"
    >
      {{ hint }}
    </p>
  </div>
</template>
