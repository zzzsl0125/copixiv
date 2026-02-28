import { ref, type Ref } from 'vue';

export interface CursorPaginationResult<T> {
    items: Ref<T[]>;
    loading: Ref<boolean>;
    error: Ref<string | null>;
    cursor: Ref<any>;
    noMoreData: Ref<boolean>;
    loadData: (isLoadMore?: boolean) => Promise<void>;
    reset: () => void;
    handleLoadMore: () => void;
}

export function useCursorPagination<T>(
  fetcher: (cursor?: any) => Promise<{ items: T[]; cursor?: any }>,
): CursorPaginationResult<T> {
  const items = ref<T[]>([]) as Ref<T[]>;
  const loading = ref(false);
  const error = ref<string | null>(null);
  const cursor = ref<any>(null);
  const noMoreData = ref(false);

  const loadData = async (isLoadMore = false) => {
    if (loading.value) return;
    if (isLoadMore && noMoreData.value) return;

    loading.value = true;
    error.value = null;

    try {
      const currentCursor = isLoadMore ? cursor.value : undefined;
      const res = await fetcher(currentCursor);
      
      const newItems = res.items || [];

      if (isLoadMore) {
        items.value.push(...newItems);
      } else {
        items.value = newItems;
      }

      if (res.cursor) {
        cursor.value = res.cursor;
        noMoreData.value = false;
      } else {
        cursor.value = null;
        noMoreData.value = true;
      }
    } catch (err: any) {
      console.error(err);
      error.value = err.message || '加载失败，请检查网络或后端状态';
    } finally {
      loading.value = false;
    }
  };

  const reset = () => {
    cursor.value = null;
    noMoreData.value = false;
    items.value = []; 
  };

  const handleLoadMore = () => {
      loadData(true);
  };

  return {
    items,
    loading,
    error,
    cursor,
    noMoreData,
    loadData,
    reset,
    handleLoadMore
  };
}
