import { ref, type Ref } from 'vue';

export interface PaginationOptions {
  pageSize?: number;
  initialLoad?: boolean;
}

export function usePagination<T>(
  fetcher: (offset: number, limit: number) => Promise<{ items: T[], total?: number } | T[]>,
  options: PaginationOptions = {}
) {
  const { pageSize = 20, initialLoad = true } = options;
  
  const items = ref<T[]>([]) as Ref<T[]>;
  const loading = ref(false);
  const error = ref<string | null>(null);
  const hasMore = ref(true);
  const offset = ref(0);
  const initialized = ref(false);

  const loadData = async (loadMore = false) => {
    if (loading.value) return;
    // If it's a fresh load (not loadMore), reset
    if (!loadMore) {
      offset.value = 0;
      items.value = [];
      hasMore.value = true;
    }

    // If no more data and we try to load more, stop
    if (loadMore && !hasMore.value) return;

    loading.value = true;
    error.value = null;

    try {
      const result = await fetcher(offset.value, pageSize);
      let newItems: T[] = [];
      
      if (Array.isArray(result)) {
        newItems = result;
      } else {
        newItems = result.items;
      }

      if (newItems.length < pageSize) {
        hasMore.value = false;
      } else {
        hasMore.value = true;
      }

      if (loadMore) {
        items.value = [...items.value, ...newItems];
      } else {
        items.value = newItems;
      }

      offset.value += newItems.length;
      initialized.value = true;
    } catch (err: any) {
      error.value = err.message || '加载失败';
      console.error(err);
    } finally {
      loading.value = false;
    }
  };

  if (initialLoad) {
    loadData();
  }

  return {
    items,
    loading,
    error,
    hasMore,
    loadData,
    initialized
  };
}
