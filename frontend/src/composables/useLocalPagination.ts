import { ref, type Ref } from 'vue';

export interface LocalPaginationOptions {
  pageSize?: number;
}

export function useLocalPagination<T>(
  fetcher: () => Promise<T[]>,
  options: LocalPaginationOptions = {}
) {
  const pageSize = options.pageSize || 20;
  
  const allItems = ref<T[]>([]) as Ref<T[]>;
  const displayedItems = ref<T[]>([]) as Ref<T[]>;
  const loading = ref(false);
  const error = ref<string | null>(null);
  const hasMore = ref(false);
  
  // Current index for pagination
  const currentIndex = ref(0);

  const loadData = async () => {
    loading.value = true;
    error.value = null;
    currentIndex.value = 0;
    
    try {
      const items = await fetcher();
      allItems.value = items;
      
      // Initial slice
      displayedItems.value = allItems.value.slice(0, pageSize);
      currentIndex.value = displayedItems.value.length;
      hasMore.value = currentIndex.value < allItems.value.length;
      
    } catch (err: any) {
      error.value = err.message || 'Wait, failed to load data';
      console.error(err);
    } finally {
      loading.value = false;
    }
  };

  const loadMore = () => {
    if (!hasMore.value) return;
    
    const nextIndex = currentIndex.value + pageSize;
    const nextBatch = allItems.value.slice(currentIndex.value, nextIndex);
    
    displayedItems.value = [...displayedItems.value, ...nextBatch];
    currentIndex.value += nextBatch.length;
    
    hasMore.value = currentIndex.value < allItems.value.length;
  };

  return {
    items: displayedItems,
    allItems,
    loading,
    error,
    hasMore,
    loadData,
    loadMore
  };
}
