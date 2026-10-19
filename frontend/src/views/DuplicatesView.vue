<template>
  <div
    class="mx-auto flex max-w-[1100px] flex-col gap-6 px-4 pt-6 sm:px-6 md:pt-8 lg:px-10"
  >
    <PageHeader
      :title="t('duplicates.title')"
      :subtitle="t('duplicates.subtitle')"
    >
      <UiButton variant="ghost" icon="library" :to="{ name: 'Library' }">
        {{ t('library.backToLibrary') }}
      </UiButton>
      <UiButton
        variant="secondary"
        icon="refresh"
        :loading="loading"
        @click="load"
      >
        {{ loading ? t('duplicates.checking') : t('duplicates.checkAgain') }}
      </UiButton>
    </PageHeader>

    <p v-if="error" class="text-[13px] text-danger" role="alert">{{ error }}</p>

    <template v-if="report">
      <UiEmpty
        v-if="!report.track_count && !report.album_count"
        icon="check"
        :title="t('duplicates.noneTitle')"
        :body="t('duplicates.noneBody')"
      />

      <template v-else>
        <UiPanel v-if="report.tracks.length" :title="t('duplicates.titles')">
          <ul class="flex flex-col gap-4">
            <li
              v-for="group in report.tracks"
              :key="`t-${group.title}-${group.artist}`"
              class="rounded-control border border-line-2 bg-surface-2 p-3.5"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-sm font-semibold text-fg">
                    {{ group.title }}
                  </span>
                  <span class="block truncate text-[13px] text-muted">
                    {{ group.artist }}
                  </span>
                </span>
                <UiBadge tone="neutral">
                  {{ t('duplicates.copyCount', { count: group.count }) }}
                </UiBadge>
                <UiButton
                  size="sm"
                  variant="ghost"
                  icon="copy"
                  @click="selectExtras(group)"
                >
                  {{ t('duplicates.selectExtras') }}
                </UiButton>
              </div>
              <ul class="mt-2 flex flex-col divide-y divide-line-2">
                <li
                  v-for="file in group.files"
                  :key="file.file"
                  class="flex items-center gap-3 py-2"
                >
                  <input
                    type="checkbox"
                    class="size-4"
                    :checked="selected.has(file.file)"
                    @change="toggle(file.file, $event.target.checked)"
                  />
                  <span class="min-w-0 flex-1">
                    <span class="block truncate text-[13px] text-fg">
                      {{ file.file }}
                    </span>
                    <span class="block truncate text-xs text-muted">
                      {{ file.album }}
                      <template v-if="file.codec"> · {{ file.codec }}</template>
                      · {{ formatBytes(file.size) }}
                    </span>
                  </span>
                </li>
              </ul>
            </li>
          </ul>
        </UiPanel>

        <UiPanel v-if="report.albums.length" :title="t('duplicates.albums')">
          <ul class="flex flex-col gap-4">
            <li
              v-for="group in report.albums"
              :key="`a-${group.album}-${group.album_artist}`"
              class="rounded-control border border-line-2 bg-surface-2 p-3.5"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-sm font-semibold text-fg">
                    {{ group.album }}
                  </span>
                  <span class="block truncate text-[13px] text-muted">
                    {{ group.album_artist }}
                  </span>
                </span>
                <UiBadge tone="neutral">
                  {{ t('duplicates.trackCount', { count: group.track_count }) }}
                </UiBadge>
                <UiButton
                  size="sm"
                  variant="ghost"
                  icon="copy"
                  @click="selectExtras(group)"
                >
                  {{ t('duplicates.selectExtras') }}
                </UiButton>
              </div>
              <p class="mt-1.5 text-[13px] text-warn">
                {{ reasonText(group) }}
              </p>
              <ul class="mt-2 flex flex-col divide-y divide-line-2">
                <li
                  v-for="file in group.files"
                  :key="file.file"
                  class="flex items-center gap-3 py-2"
                >
                  <input
                    type="checkbox"
                    class="size-4"
                    :checked="selected.has(file.file)"
                    @change="toggle(file.file, $event.target.checked)"
                  />
                  <span class="min-w-0 flex-1">
                    <span class="block truncate text-[13px] text-fg">
                      {{ file.file }}
                    </span>
                    <span class="block truncate text-xs text-muted">
                      {{ file.title }}
                      <template v-if="file.track_number">
                        · #{{ file.track_number }}
                      </template>
                      · {{ formatBytes(file.size) }}
                    </span>
                  </span>
                </li>
              </ul>
            </li>
          </ul>
        </UiPanel>
      </template>

      <div
        v-if="selected.size"
        class="sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-30 flex flex-wrap items-center gap-3 rounded-panel border border-line-3 bg-surface px-4 py-3 shadow-float md:bottom-4"
      >
        <span class="min-w-0 flex-1 text-[13px] text-muted">
          {{ t('duplicates.selected', { count: selected.size }) }}
        </span>
        <UiButton
          variant="danger"
          icon="trash"
          :loading="deleting"
          @click="removeSelected"
        >
          {{ t('duplicates.deleteSelected') }}
        </UiButton>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'

import PageHeader from '/src/components/library/PageHeader.vue'
import UiBadge from '/src/components/ui/UiBadge.vue'
import UiButton from '/src/components/ui/UiButton.vue'
import UiEmpty from '/src/components/ui/UiEmpty.vue'
import UiPanel from '/src/components/ui/UiPanel.vue'
import { formatBytes } from '/src/lib/format'
import API from '/src/model/api'
import { useLibrary } from '/src/model/library'
import { useUi } from '/src/model/ui'
import { useI18n } from '/src/i18n'

const { t } = useI18n()
const ui = useUi()
const library = useLibrary()

const report = ref(null)
const loading = ref(false)
const deleting = ref(false)
const error = ref('')
const selected = ref(new Set())

function detail(err) {
  return err?.response?.data?.detail || err?.message || 'failed'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await API.getDuplicates()
    report.value = res.data || null
    selected.value = new Set()
  } catch (err) {
    error.value = detail(err)
  } finally {
    loading.value = false
  }
}

function toggle(file, on) {
  const next = new Set(selected.value)
  if (on) next.add(file)
  else next.delete(file)
  selected.value = next
}

// Keep one copy: select everything except the first (alphabetical) file.
function selectExtras(group) {
  const next = new Set(selected.value)
  for (const file of group.files.slice(1)) next.add(file.file)
  selected.value = next
}

function reasonText(group) {
  const parts = []
  if (group.reasons.includes('multiple folders')) {
    parts.push(t('duplicates.reasonMultipleFolders'))
  }
  if (group.reasons.includes('repeated song')) {
    parts.push(t('duplicates.reasonRepeatedSong'))
  }
  return parts.join(' · ')
}

async function removeSelected() {
  const files = [...selected.value]
  if (!files.length) return
  const ok = await ui.confirm({
    title: t('duplicates.deleteTitle'),
    body: t('duplicates.deleteBody'),
    confirmLabel: t('duplicates.deleteSelected'),
    danger: true,
  })
  if (!ok) return
  deleting.value = true
  try {
    const { deleted, failed } = await library.deleteFiles(files)
    if (failed) {
      ui.toast(t('toast.deletePartial', { count: failed }), { kind: 'error' })
    } else {
      ui.toast(t('duplicates.deleted', { count: deleted.length }), {
        kind: 'success',
      })
    }
    library.refreshSoon(500)
    await load()
  } catch (err) {
    ui.toast(detail(err), { kind: 'error' })
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>
