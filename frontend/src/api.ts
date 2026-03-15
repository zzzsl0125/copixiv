import axios from 'axios';

export const api = axios.create({
  baseURL: '/api', // Proxy routes it to http://127.0.0.1:9000
});

export interface Novel {
  id: number;
  title: string;
  author_id?: number;
  author_name?: string;
  series_id?: number;
  series_name?: string;
  series_index?: number;
  like: number;
  view: number;
  text: number;
  caption?: string;
  create_time?: string;
  has_epub: number;
  tags: string[];
  is_favourite: number;
  is_special_follow: number;
}

export interface GetNovelsParams {
  queries?: Record<string, any>;
  order_by?: string;
  order_direction?: string;
  cursor?: Record<string, any>;
  per_page?: number;
  min_like?: number;
  min_text?: number;
}

export const novelApi = {
  getNovels: async (params: GetNovelsParams) => {
    // queries and cursor need to be JSON stringified
    const queryParams: Record<string, any> = { ...params };
    
    if (params.queries) {
      queryParams.queries = JSON.stringify(params.queries);
    }
    if (params.cursor) {
      queryParams.cursor = JSON.stringify(params.cursor);
    }
    
    const url = api.getUri({ url: '/novels/', params: queryParams });
    console.log("Request URL:", url);

    const response = await api.get('/novels/', { params: queryParams });
    return response.data as { novels: Novel[], cursor: Record<string, any> | null };
  },

  toggleFavourite: async (novelId: number) => {
    await api.post(`/novels/${novelId}/favourite`);
  },

  toggleSpecialFollow: async (authorId: number) => {
    await api.post(`/novels/author/${authorId}/follow`);
  },
  
  downloadNovelUrl: (novelId: number, format: 'txt' | 'epub' = 'txt') => {
    return `${api.defaults.baseURL || '/api'}/novels/${novelId}/download?format=${format}`;
  }
};

// --- Tag Preference API ---
// Note: These functions handle tag preferences.

export interface TagPreferenceResponse {
  id: number;
  tag: string;
  preference: 'favourite' | 'blocked';
  sort_index: number;
}

export const tagPreferenceApi = {
  getTagPreferences: async () => {
    const response = await api.get('/tag-preferences/');
    return response.data as TagPreferenceResponse[];
  },

  setTagPreference: async (tag: string, preference: 'favourite' | 'blocked') => {
    const response = await api.post('/tag-preferences/', { tag, preference });
    return response.data;
  },

  deleteTagPreference: async (tag: string) => {
    const response = await api.delete(`/tag-preferences/${tag}`);
    return response.data;
  },

  reorderTagPreferences: async (tagIds: number[]) => {
    const response = await api.post('/tag-preferences/reorder', tagIds);
    return response.data;
  },
};

// --- Task API ---

export interface ScheduledTask {
  id: number;
  name: string;
  task: string;
  cron: string;
  params?: Record<string, any>;
  config?: Record<string, any>;
  is_enabled: boolean;
}

export interface ScheduledTaskCreate {
  name: string;
  task: string;
  cron: string;
  params?: Record<string, any>;
  config?: Record<string, any>;
  is_enabled: boolean;
}

export interface ScheduledTaskUpdate {
  name?: string;
  task?: string;
  cron?: string;
  params?: Record<string, any>;
  config?: Record<string, any>;
  is_enabled?: boolean;
}

export interface TaskHistory {
  id: number;
  name: string;
  arguments?: string;
  status: string;
  start_time: string;
  end_time?: string;
  duration?: number;
  result?: string;
}

export interface TaskArgument {
  name: string;
  type: string;
  default?: any;
  required: boolean;
}

export interface TaskMethod {
  name: string;
  description?: string;
  arguments: TaskArgument[];
}

export const taskApi = {
  getTaskMethods: async () => {
    const response = await api.get('/tasks/methods');
    return response.data as TaskMethod[];
  },
  getScheduledTasks: async () => {
    const response = await api.get('/tasks/scheduled');
    return response.data as ScheduledTask[];
  },
  
  createScheduledTask: async (data: ScheduledTaskCreate) => {
    const response = await api.post('/tasks/scheduled', data);
    return response.data as ScheduledTask;
  },
  
  updateScheduledTask: async (id: number, data: ScheduledTaskUpdate) => {
    const response = await api.put(`/tasks/scheduled/${id}`, data);
    return response.data as ScheduledTask;
  },
  
  deleteScheduledTask: async (id: number) => {
    const response = await api.delete(`/tasks/scheduled/${id}`);
    return response.data;
  },
  
  runScheduledTask: async (id: number) => {
    const response = await api.post(`/tasks/scheduled/${id}/run`);
    return response.data;
  },

  reorderScheduledTasks: async (taskIds: number[]) => {
    const response = await api.post('/tasks/scheduled/reorder', taskIds);
    return response.data;
  },

  getTaskHistory: async (limit = 50, offset = 0) => {
    const response = await api.get('/tasks/history', { params: { limit, offset } });
    return response.data as { items: TaskHistory[], total: number };
  },
};

export interface SystemConfig {
  default_min_like: number;
  default_min_text: number;
}

export const systemApi = {
  getConfig: async () => {
    const response = await api.get('/system/config');
    return response.data as SystemConfig;
  },
};

// --- Search History API ---

export interface SearchHistory {
  id: number;
  type: string;
  value: string;
  display_value?: string;
  timestamp: string;
}

export const searchHistoryApi = {
  getSearchHistory: async () => {
    const response = await api.get('/search-history/');
    return response.data as SearchHistory[];
  },

  deleteSearchHistoryItem: async (historyId: number) => {
    await api.delete(`/search-history/${historyId}`);
  },

  clearSearchHistory: async () => {
    await api.delete('/search-history/');
  },
};

// --- Token API ---

export interface Token {
  id: number;
  name: string;
  token: string;
  premium: boolean;
  special: boolean;
  valid: boolean;
}

export interface TokenCreate {
  name: string;
  token: string;
  premium?: boolean;
  special?: boolean;
  valid?: boolean;
}

export interface TokenUpdate {
  name?: string;
  token?: string;
  premium?: boolean;
  special?: boolean;
  valid?: boolean;
}

export const tokenApi = {
  getTokens: async () => {
    const response = await api.get('/tokens');
    return response.data as Token[];
  },

  createToken: async (data: TokenCreate) => {
    const response = await api.post('/tokens', data);
    return response.data as Token;
  },

  updateToken: async (id: number, data: TokenUpdate) => {
    const response = await api.put(`/tokens/${id}`, data);
    return response.data as Token;
  },

  deleteToken: async (id: number) => {
    const response = await api.delete(`/tokens/${id}`);
    return response.data;
  },

  reorderTokens: async (tokenIds: number[]) => {
    const response = await api.post('/tokens/reorder', tokenIds);
    return response.data;
  },
};

