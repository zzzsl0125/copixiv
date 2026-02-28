<script lang="ts">
import { defineComponent, h, computed } from 'vue';
import { CheckCircle, XCircle, RefreshCw, Clock } from 'lucide-vue-next';

export default defineComponent({
  name: 'StatusBadge',
  props: {
    status: {
      type: String,
      required: true
    }
  },
  setup(props) {
    const statusConfig = computed(() => {
      switch (props.status?.toUpperCase()) {
        case 'SUCCESS':
          return {
            colorClass: 'text-green-600 bg-green-50 border-green-200',
            icon: CheckCircle,
            iconClass: 'text-green-500'
          };
        case 'FAILED':
          return {
            colorClass: 'text-red-600 bg-red-50 border-red-200',
            icon: XCircle,
            iconClass: 'text-red-500'
          };
        case 'RUNNING':
          return {
            colorClass: 'text-blue-600 bg-blue-50 border-blue-200',
            icon: RefreshCw,
            iconClass: 'text-blue-500 animate-spin'
          };
        case 'PENDING':
          return {
            colorClass: 'text-yellow-600 bg-yellow-50 border-yellow-200',
            icon: Clock,
            iconClass: 'text-yellow-500'
          };
        default:
          return {
            colorClass: 'text-gray-600 bg-gray-50 border-gray-200',
            icon: Clock,
            iconClass: 'text-gray-400'
          };
      }
    });

    return () => h('span', {
      class: [
        'px-2.5 py-0.5 rounded-full text-xs font-medium border inline-flex items-center gap-1',
        statusConfig.value.colorClass
      ]
    }, [
      h(statusConfig.value.icon, { class: ['w-3 h-3', statusConfig.value.iconClass] }),
      props.status
    ]);
  }
});
</script>
