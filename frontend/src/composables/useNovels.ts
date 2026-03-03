import { reactive, onMounted, onUnmounted } from 'vue';
import { novelApi, type Novel } from '../api';
import { useCursorPagination } from './useCursorPagination';

export function useNovels() {
  const urlParams = new URLSearchParams(window.location.search);
  
  const filters = reactive({
    keyword: urlParams.get('keyword') || '',
    order_by: urlParams.get('order_by') || 'random',
    order_direction: urlParams.get('order_direction') || 'DESC',
    min_like: urlParams.get('min_like') ? Number(urlParams.get('min_like')) : 500,
    min_text: urlParams.get('min_text') ? Number(urlParams.get('min_text')) : 3000,
  });

  const fetchNovels = async (cursor?: any) => {
    const queries: Record<string, string> = {};
    const keyword = filters.keyword.trim();
    let orderBy = filters.order_by;
    let orderDirection = filters.order_direction;

    if (keyword) {
      const conditions = keyword.split(';').filter(cond => cond.trim());
      for (const condition of conditions) {
        const trimmed = condition.trim();
        const colonIndex = trimmed.indexOf(':');
        if (colonIndex > 0) {
          const type = trimmed.substring(0, colonIndex).trim();
          const value = trimmed.substring(colonIndex + 1).trim();
          if (value) queries[value] = type;
        } else {
          queries[trimmed] = 'keyword';
        }
      }

      let isSpecialCase = false;
      if (conditions.length === 1 && conditions[0]) {
        const condition = conditions[0];
        const colonIndex = condition.indexOf(':');
        if (colonIndex > 0) {
          const type = condition.substring(0, colonIndex).trim();
          if (type === 'author_id' || type === 'author') {
            orderBy = 'id';
            orderDirection = 'DESC';
            isSpecialCase = true;
          } else if (type === 'series_id' || type === 'series') {
            orderBy = 'series_index';
            orderDirection = 'ASC';
            isSpecialCase = true;
          }
        }
      }
      if (!isSpecialCase) {
        orderBy = 'like';
        orderDirection = 'DESC';
      }
    }

    const res = await novelApi.getNovels({
      queries: Object.keys(queries).length > 0 ? queries : undefined,
      order_by: orderBy,
      order_direction: orderDirection,
      min_like: filters.min_like || undefined,
      min_text: filters.min_text || undefined,
      cursor: cursor || undefined,
      per_page: 10
    });

    return {
      items: res.novels || [],
      cursor: res.cursor
    };
  };

  const {
    items: novels,
    loading,
    error,
    cursor,
    noMoreData,
    loadData: loadNovelsBase,
    reset,
    handleLoadMore
  } = useCursorPagination<Novel>(fetchNovels);

  const loadNovels = (isLoadMore = false) => loadNovelsBase(isLoadMore);

  const handleSearch = (keyword?: string, updateUrl = true) => {
    if (typeof keyword === 'string') filters.keyword = keyword;
    
    reset(); // Reset cursor and clear items

    if (updateUrl) {
      const url = new URL(window.location.href);
      if (filters.keyword) url.searchParams.set('keyword', filters.keyword);
      else url.searchParams.delete('keyword');
      
      if (filters.order_by !== 'random') url.searchParams.set('order_by', filters.order_by);
      else url.searchParams.delete('order_by');
      
      if (filters.order_direction !== 'DESC') url.searchParams.set('order_direction', filters.order_direction);
      else url.searchParams.delete('order_direction');
      
      if (filters.min_like !== undefined) url.searchParams.set('min_like', filters.min_like.toString());
      else url.searchParams.delete('min_like');
      
      if (filters.min_text !== undefined) url.searchParams.set('min_text', filters.min_text.toString());
      else url.searchParams.delete('min_text');

      window.history.pushState({}, '', url);
    }

    loadNovels();
  };

  const onPopState = () => {
    const params = new URLSearchParams(window.location.search);
    filters.keyword = params.get('keyword') || '';
    filters.order_by = params.get('order_by') || 'random';
    filters.order_direction = params.get('order_direction') || 'DESC';
    filters.min_like = params.get('min_like') ? Number(params.get('min_like')) : 500;
    filters.min_text = params.get('min_text') ? Number(params.get('min_text')) : 3000;
    
    handleSearch(undefined, false);
  };

  onMounted(() => {
    window.addEventListener('popstate', onPopState);
  });

  onUnmounted(() => {
    window.removeEventListener('popstate', onPopState);
  });

  const handleCardSearch = (type: string, value: string | number) => {
    let field = type;
    if (type === 'author') field = 'author_id';
    if (type === 'series') field = 'series_id';
    if (type === 'tag') field = 'tags';
    
    const formattedValue = typeof value === 'string' && value.toString().includes(' ') ? `"${value}"` : value;
    filters.keyword = `${field}:${formattedValue};`;
handleSearch();
  };

  return {
    novels,
    loading,
    error,
    cursor,
    noMoreData,
    filters,
    loadNovels,
    handleSearch,
    handleLoadMore,
    handleCardSearch
  };
}
