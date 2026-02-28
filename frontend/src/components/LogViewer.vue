<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { Download, Copy, Maximize2, Minimize2, Search, ArrowDown, ArrowUp } from 'lucide-vue-next';

const props = defineProps<{
  log: string;
  title?: string;
  maxHeight?: string; // CSS value, e.g., '500px' or '80vh'
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

// const containerRef = ref<HTMLElement | null>(null);
const logContentRef = ref<HTMLElement | null>(null);
const searchTerm = ref('');
const showSearch = ref(false);
// const autoScroll = ref(false); // Default to false for static logs, could be true for streaming
const isFullscreen = ref(false);

// Format logs: support basic ANSI colors if needed, or just line numbers
// For now, let's just do line numbers and basic highlighting
const processedLines = computed(() => {
  if (!props.log) return [];
  return props.log.split('\n').map((line, index) => ({
    number: index + 1,
    content: line,
    id: `L${index + 1}`
  }));
});

// Search functionality
const currentMatchIndex = ref(-1);
const matches = computed(() => {
  if (!searchTerm.value) return [];
  const results: { lineIndex: number; start: number; end: number }[] = [];
  processedLines.value.forEach((line, idx) => {
    if (line.content.toLowerCase().includes(searchTerm.value.toLowerCase())) {
      results.push({ lineIndex: idx, start: 0, end: 0 }); // Simplified match
    }
  });
  return results;
});

watch(searchTerm, () => {
  currentMatchIndex.value = matches.value.length > 0 ? 0 : -1;
  scrollToMatch();
});

const nextMatch = () => {
  if (matches.value.length === 0) return;
  currentMatchIndex.value = (currentMatchIndex.value + 1) % matches.value.length;
  scrollToMatch();
};

const prevMatch = () => {
  if (matches.value.length === 0) return;
  currentMatchIndex.value = (currentMatchIndex.value - 1 + matches.value.length) % matches.value.length;
  scrollToMatch();
};

const scrollToMatch = () => {
  const match = matches.value[currentMatchIndex.value];
  if (!match || !logContentRef.value) return;
  
  const el = logContentRef.value.children[match.lineIndex] as HTMLElement;
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
};

const copyLog = async () => {
  try {
    await navigator.clipboard.writeText(props.log);
    // Maybe show a toast
    alert('日志已复制到剪贴板');
  } catch (err) {
    console.error('Failed to copy!', err);
  }
};

const downloadLog = () => {
  const blob = new Blob([props.log], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `log-${new Date().toISOString()}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

const toggleFullscreen = () => {
  isFullscreen.value = !isFullscreen.value;
};

// Auto-scroll logic (if we were streaming)
/*
watch(() => props.log, () => {
  if (autoScroll.value && containerRef.value) {
    nextTick(() => {
        if(containerRef.value)
            containerRef.value.scrollTop = containerRef.value.scrollHeight;
    });
  }
});
*/

</script>

<template>
  <div 
    class="flex flex-col bg-[#1e1e1e] text-[#d4d4d4] rounded-lg overflow-hidden border border-[#333] shadow-xl transition-all duration-300"
    :class="[
      isFullscreen ? 'fixed inset-0 z-50 h-screen w-screen rounded-none' : 'w-full',
      !isFullscreen && maxHeight ? '' : 'h-full'
    ]"
    :style="!isFullscreen && maxHeight ? { height: maxHeight } : {}"
  >
    <!-- Toolbar -->
    <div class="flex items-center justify-between px-4 py-2 bg-[#252526] border-b border-[#333] select-none">
      <div class="flex items-center space-x-3 overflow-hidden">
        <span class="text-sm font-medium text-gray-300 truncate">{{ title || 'Execution Log' }}</span>
        <span class="text-xs text-gray-500 font-mono">{{ processedLines.length }} lines</span>
      </div>
      
      <div class="flex items-center space-x-2">
        <!-- Search Toggle -->
        <button 
          @click="showSearch = !showSearch" 
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors"
          title="Search"
        >
          <Search class="w-4 h-4" />
        </button>

        <!-- Copy -->
        <button 
          @click="copyLog" 
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors"
          title="Copy All"
        >
          <Copy class="w-4 h-4" />
        </button>

        <!-- Download -->
        <button 
          @click="downloadLog" 
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors"
          title="Download"
        >
          <Download class="w-4 h-4" />
        </button>

        <!-- Fullscreen -->
        <button 
          @click="toggleFullscreen" 
          class="p-1.5 hover:bg-[#3c3c3c] rounded text-gray-400 hover:text-white transition-colors"
          :title="isFullscreen ? 'Exit Fullscreen' : 'Fullscreen'"
        >
          <Minimize2 v-if="isFullscreen" class="w-4 h-4" />
          <Maximize2 v-else class="w-4 h-4" />
        </button>
      </div>
    </div>

    <!-- Search Bar -->
    <div v-if="showSearch" class="flex items-center px-4 py-2 bg-[#2d2d2d] border-b border-[#333]">
      <div class="relative flex-grow">
        <input 
          v-model="searchTerm"
          type="text" 
          placeholder="Search..." 
          class="w-full bg-[#1e1e1e] text-white text-xs rounded px-3 py-1.5 border border-[#3e3e42] focus:outline-none focus:border-blue-500"
          @keydown.enter="nextMatch"
        >
        <span v-if="matches.length > 0" class="absolute right-2 top-1.5 text-xs text-gray-500">
          {{ currentMatchIndex + 1 }}/{{ matches.length }}
        </span>
      </div>
      <div class="flex ml-2 space-x-1">
        <button @click="prevMatch" class="p-1 hover:bg-[#3c3c3c] rounded text-gray-400"><ArrowUp class="w-4 h-4" /></button>
        <button @click="nextMatch" class="p-1 hover:bg-[#3c3c3c] rounded text-gray-400"><ArrowDown class="w-4 h-4" /></button>
      </div>
    </div>

    <!-- Log Content -->
    <div 
      ref="containerRef"
      class="flex-grow overflow-auto font-mono text-xs sm:text-sm leading-relaxed p-4 scrollbar-thin scrollbar-thumb-[#424242] scrollbar-track-transparent"
    >
      <div ref="logContentRef" class="w-full">
        <div 
          v-for="(line, index) in processedLines" 
          :key="line.id"
          class="flex group hover:bg-[#2a2d2e]"
          :class="{'bg-[#37373d]': matches.length > 0 && matches[currentMatchIndex]?.lineIndex === index}"
        >
          <!-- Line Number -->
          <span class="flex-shrink-0 w-8 sm:w-12 text-right text-gray-600 select-none mr-3 sm:mr-4 border-r border-[#333] pr-3 sm:pr-4 group-hover:text-gray-500">
            {{ line.number }}
          </span>
          
          <!-- Line Content -->
          <span class="whitespace-pre-wrap break-all text-[#d4d4d4] flex-1">
            <template v-if="searchTerm && line.content.toLowerCase().includes(searchTerm.toLowerCase())">
                <!-- Highlight line background handled by parent div class -->
                {{ line.content }}
            </template>
            <template v-else>
              {{ line.content }}
            </template>
          </span>
        </div>
        
        <div v-if="processedLines.length === 0" class="text-gray-500 italic text-center mt-10">
          No logs available
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Custom Scrollbar for Webkit */
.scrollbar-thin::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
.scrollbar-thin::-webkit-scrollbar-track {
  background: transparent;
}
.scrollbar-thin::-webkit-scrollbar-thumb {
  background: #424242;
  border-radius: 5px;
  border: 2px solid #1e1e1e;
}
.scrollbar-thin::-webkit-scrollbar-thumb:hover {
  background: #4f4f4f;
}
</style>
