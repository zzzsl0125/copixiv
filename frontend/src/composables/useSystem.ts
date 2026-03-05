import { ref, onMounted } from 'vue';
import { systemApi, type SystemConfig } from '../api';

const systemConfig = ref<SystemConfig | null>(null);
const loading = ref(false);
const error = ref<any>(null);

export function useSystem() {
  const fetchConfig = async () => {
    if (systemConfig.value) {
      return;
    }

    loading.value = true;
    error.value = null;
    try {
      systemConfig.value = await systemApi.getConfig();
    } catch (e) {
      error.value = e;
    } finally {
      loading.value = false;
    }
  };

  onMounted(fetchConfig);

  return {
    systemConfig,
    loading,
    error,
    fetchConfig,
  };
}
