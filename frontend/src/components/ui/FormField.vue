<script setup lang="ts">
import { computed, useId } from 'vue'
import { Label } from 'reka-ui'

const props = defineProps<{
  label?: string
  forId?: string
  hint?: string
  hintTone?: 'muted' | 'ok' | 'warn' | 'error'
  error?: string | null
  disabled?: boolean
  hintTestId?: string
}>()

const generatedId = useId()
const fieldId = computed(() => props.forId || generatedId)
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
    <p v-if="error" class="mt-0.5 text-xs text-red-600">
      {{ error }}
    </p>
  </div>
</template>
