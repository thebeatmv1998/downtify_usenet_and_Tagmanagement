<template>
  <ProviderOrder
    :model-value="modelValue"
    :providers="providers"
    :first-when-enabled="['slskd', 'usenet']"
    @update:model-value="(value) => $emit('update:modelValue', value)"
    @enabled="onEnabled"
  >
    <template #badge="{ row }">
      <SourceBadge :source="row.id" />
    </template>
  </ProviderOrder>
</template>

<script setup>
import { computed } from 'vue'
import ProviderOrder from './ProviderOrder.vue'
import SourceBadge from '../ui/SourceBadge.vue'
import { useI18n } from '/src/i18n'

defineProps({ modelValue: { type: Array, required: true } })
const emit = defineEmits(['update:modelValue', 'enable-slskd', 'enable-usenet'])
const { t } = useI18n()

function onEnabled(id) {
  if (id === 'slskd') emit('enable-slskd')
  if (id === 'usenet') emit('enable-usenet')
}

// slskd and usenet first: the lossless sources lead when they're on.
const providers = computed(() => [
  {
    id: 'slskd',
    title: t('settings.sourceSlskd'),
    description: t('settings.sourceSlskdHint'),
  },
  {
    id: 'usenet',
    title: t('settings.sourceUsenet'),
    description: t('settings.sourceUsenetHint'),
  },
  {
    id: 'youtube-music',
    title: t('settings.sourceYtm'),
    description: t('settings.sourceYtmHint'),
  },
  {
    id: 'youtube',
    title: t('settings.sourceYt'),
    description: t('settings.sourceYtHint'),
  },
])
</script>
