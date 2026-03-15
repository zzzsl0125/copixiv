<template>
  <div class="flex items-center">
    <input 
      :id="id" 
      type="checkbox" 
      :checked="modelValue"
      @change="updateValue"
      class="focus:ring-blue-500 h-4 w-4 text-blue-600 border-gray-300 rounded"
    >
    <label v-if="label" :for="id" class="ml-2 block text-sm text-gray-900">
      {{ label }}
    </label>
    <slot name="label-after"></slot>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  modelValue: boolean;
  label?: string;
  id?: string;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void;
}>();

// Generate a random ID if not provided, for a11y label association
const generatedId = computed(() => `app-checkbox-${Math.random().toString(36).substring(2, 9)}`);
const id = computed(() => props.id || generatedId.value);

const updateValue = (event: Event) => {
  const target = event.target as HTMLInputElement;
  emit('update:modelValue', target.checked);
};
</script>
