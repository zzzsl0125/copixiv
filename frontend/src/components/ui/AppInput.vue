<template>
  <div>
    <!-- reka-ui 没有文本输入原语（headless 库不接管原生 input/textarea 的
         value/受控语义），所以只把 <label> 换成 reka 的 Label 以获得一致的
         for/id 关联；input/textarea 继续用原生元素承载 v-model 行为。 -->
    <Label v-if="label" :for="id" class="block text-sm font-medium text-gray-700">{{ label }}</Label>
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
import { computed } from 'vue'
import { Label } from 'reka-ui'

const props = withDefaults(defineProps<{
  modelValue: string | number
  label?: string
  type?: string
  rows?: number | string
  required?: boolean
  placeholder?: string
  id?: string
}>(), {
  type: 'text',
  rows: 3,
  required: false,
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const generatedId = computed(() => `app-input-${Math.random().toString(36).substring(2, 9)}`)
const id = computed(() => props.id || generatedId.value)

const updateValue = (event: Event) => {
  const target = event.target as HTMLInputElement | HTMLTextAreaElement
  emit('update:modelValue', target.value)
}
</script>
