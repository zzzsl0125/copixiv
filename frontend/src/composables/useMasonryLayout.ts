import { ref, watch, nextTick, computed, onMounted, onUnmounted, type Ref } from 'vue';

export function useMasonryLayout<T>(
  items: Ref<T[]>,
  columnRefs: Ref<HTMLElement[]> // 传入每一列的 DOM 引用
) {
  const windowWidth = ref(window.innerWidth);
  
  // 维护实际渲染到每列的数据
  const columns = ref<T[][]>([]) as Ref<T[][]>;

  const updateWidth = () => {
    windowWidth.value = window.innerWidth;
  };

  onMounted(() => {
    window.addEventListener('resize', updateWidth);
  });

  onUnmounted(() => {
    window.removeEventListener('resize', updateWidth);
  });

  const columnCount = computed(() => {
    if (windowWidth.value >= 1000) return 3; 
    if (windowWidth.value >= 500) return 2;  
    return 1;
  });

  // 重新分配所有项到各列
  const layoutItems = async () => {
    const count = columnCount.value;

    const totalRendered = columns.value.reduce((acc, col) => acc + col.length, 0);
    const isLoadMore = items.value.length > totalRendered && totalRendered !== 0;

    let startIndex = 0;

    if (!isLoadMore) {
      // 1. 初始化列
      const newColumns: T[][] = Array.from({ length: count }, () => []);
      // 使用 any 绕过 vue 响应式对范型解包导致的类型不兼容
      columns.value = newColumns as any;
      
      // 2. 等待 Vue 渲染出空的列 DOM
      await nextTick();
      startIndex = 0;
    } else {
      startIndex = totalRendered;
    }

    // 3. 逐个分配项
    for (let index = startIndex; index < items.value.length; index++) {
      const item = items.value[index];

      // 找到当前高度最小的列
      let minHeightColIndex = 0;
      let minHeight = Infinity;

      for (let i = 0; i < count; i++) {
        const colElement = columnRefs?.value?.[i];
        const height = colElement ? colElement.offsetHeight : 0;
        
        if (height < minHeight) {
          minHeight = height;
          minHeightColIndex = i;
        }
      }

      // 将项放入最小高度的列
      const targetCol = columns.value[minHeightColIndex];
      if (targetCol) {
        (targetCol as any).push(item);
      }
      
      // 等待该项渲染完成，以便下一次循环能获取到更新后的列高度
      await nextTick();
    }
  };

  // 监听列数变化，重新排版
  watch(columnCount, () => {
    columns.value = [];
    layoutItems();
  });

  // 监听数据源变化，重新排版
  watch(items, () => {
    layoutItems();
  }, { deep: true, immediate: true });

  return {
    windowWidth,
    columnCount,
    columns
  };
}
