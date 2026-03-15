<template>
  <button 
    @click="$emit('click', $event)"
    :class="computedClasses"
    class="px-3 py-1 inline-flex items-center text-xs leading-5 font-semibold rounded-full border transition-colors hover:shadow-sm"
  >
    <span>
      <slot></slot>
    </span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(defineProps<{
  active: boolean;
  activeTheme?: 'yellow' | 'purple' | 'green' | 'blue' | 'red' | 'gray';
  inactiveTheme?: 'gray' | 'red' | 'yellow' | 'purple' | 'green' | 'blue';
}>(), {
  activeTheme: 'green',
  inactiveTheme: 'gray'
});

defineEmits<{
  (e: 'click', event: MouseEvent): void;
}>();

const computedClasses = computed(() => {
  const theme = props.active ? props.activeTheme : props.inactiveTheme;
  
  // Tailwind 需要完整的 class 名字，不能动态拼接如 `bg-${theme}-100`，否则 purge 时可能会被丢弃。
  // 因此使用一个完整的映射表：
  const themeMap: Record<string, string> = {
    yellow: 'bg-yellow-100 text-yellow-800 border-yellow-200',
    purple: 'bg-purple-100 text-purple-800 border-purple-200',
    green: 'bg-green-100 text-green-800 border-green-200',
    blue: 'bg-blue-100 text-blue-800 border-blue-200',
    red: 'bg-red-100 text-red-800 border-red-200',
    gray: 'bg-gray-100 text-gray-800 border-gray-200'
  };

  return themeMap[theme] || themeMap.gray;
});
</script>
