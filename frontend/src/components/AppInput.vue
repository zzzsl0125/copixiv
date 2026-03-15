<template>
  <div>
    <label v-if="label" :for="id" class="block text-sm font-medium text-gray-700">{{ label }}</label>
    <div class="mt-1">
      <textarea
        v-if="type === 'textarea'"
        :id="id"
        :value="modelValue"
        @input="updateValue"
        :rows="rows"
        :required="required"
        class="focus:ring-blue-500 focus:border-blue-500 block w-full shadow-sm sm:text-sm border-gray-300 rounded-md p-2 border"
        :placeholder="placeholder"
      ></textarea>
      
      <input
        v-else
        :type="type"
        :id="id"
        :value="modelValue"
        @input="updateValue"
        :required="required"
        class="focus:ring-blue-500 focus:border-blue-500 block w-full shadow-sm sm:text-sm border-gray-300 rounded-md p-2 border"
        :placeholder="placeholder"
      >
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(defineProps<{
  modelValue: string | number;
  label?: string;
  type?: string;
  rows?: number | string;
  required?: boolean;
  placeholder?: string;
  id?: string;
}>(), {
  type: 'text',
  rows: 3,
  required: false
});

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void;
}>();

// Generate a random ID if not provided, for a11y label association
const generatedId = computed(() => `app-input-${Math.random().toString(36).substring(2, 9)}`);
const id = computed(() => props.id || generatedId.value);

const updateValue = (event: Event) => {
  const target = event.target as HTMLInputElement | HTMLTextAreaElement;
  emit('update:modelValue', target.value);
};
</script>
