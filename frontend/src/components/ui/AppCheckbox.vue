<template>
  <div class="flex items-center">
    <!-- reka 的 CheckboxRoot 默认渲染 <button type="button">：它常驻在 BaseModal 的
         <form> 里，必须永远不触发提交（原生按钮默认是 submit）。 -->
    <CheckboxRoot
      :id="id"
      :model-value="modelValue"
      class="h-4 w-4 shrink-0 rounded border border-gray-300 bg-white text-blue-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 data-[state=checked]:border-blue-600 data-[state=checked]:bg-blue-600 data-[state=checked]:text-white"
      @update:model-value="emit('update:modelValue', $event === true)"
    >
      <CheckboxIndicator class="flex h-full w-full items-center justify-center">
        <Check class="h-3 w-3" />
      </CheckboxIndicator>
    </CheckboxRoot>
    <Label v-if="label" :for="id" class="ml-2 block text-sm text-gray-900">
      {{ label }}
    </Label>
  </div>
</template>

<script setup lang="ts">
import { useId } from 'vue'
import { CheckboxIndicator, CheckboxRoot, Label } from 'reka-ui'
import { Check } from '@lucide/vue'

defineProps<{
  modelValue: boolean
  label?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
}>()

const id = useId()
</script>
