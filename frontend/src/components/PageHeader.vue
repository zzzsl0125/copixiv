<script setup lang="ts">
import { useRouter } from 'vue-router';
import { Menu } from 'lucide-vue-next';
import AppLogo from './AppLogo.vue';

defineProps<{
  title: string;
}>();

defineEmits<{
  (e: 'toggle-sidebar'): void;
}>();

const router = useRouter();

const goToHome = () => {
  router.push('/');
};
</script>

<template>
  <div class="bg-white shadow-sm border-b border-gray-200 z-10 sticky top-0">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex items-center justify-between h-16">
        <div class="flex items-center gap-4">
          <button @click="$emit('toggle-sidebar')" class="md:hidden p-1 -ml-1 text-gray-500 hover:text-gray-700 focus:outline-none flex-shrink-0">
            <Menu class="h-6 w-6" />
          </button>
          <AppLogo @click="goToHome" />
          <span class="text-gray-500">/</span>
          <span class="text-gray-700 font-medium">{{ title }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
