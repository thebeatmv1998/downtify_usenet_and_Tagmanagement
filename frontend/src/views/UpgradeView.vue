<template>
  <div
    class="mx-auto flex max-w-[1100px] flex-col gap-6 px-4 pt-6 sm:px-6 md:pt-8 lg:px-10"
  >
    <PageHeader :title="t('upgrade.title')" :subtitle="t('upgrade.subtitle')">
      <UiButton variant="ghost" icon="library" :to="{ name: 'Library' }">
        {{ t('library.backToLibrary') }}
      </UiButton>
      <UiButton v-if="canPause" variant="secondary" icon="pause" @click="pause">
        {{ t('upgrade.pause') }}
      </UiButton>
      <UiButton v-if="canResume" variant="primary" icon="play" @click="resume">
        {{ t('upgrade.resume') }}
      </UiButton>
      <UiButton v-if="canCancel" variant="danger" icon="x" @click="cancel">
        {{ t('upgrade.cancel') }}
      </UiButton>
      <UiButton
        v-if="canScan"
        variant="primary"
        icon="refresh"
        :loading="loading"
        @click="runScan"
      >
        {{ hasRun ? t('upgrade.rescan') : t('upgrade.scan') }}
      </UiButton>
    </PageHeader>

    <p v-if="error" class="text-[13px] text-danger" role="alert">
      {{ error }}
    </p>

    <!-- Scanning -->
    <UiPanel v-if="isScanning" :title="t('upgrade.scanning')">
      <UiProgress :value="scan.pct" :indeterminate="!scan.total" :height="6" />
      <p class="mt-2.5 text-[13px] text-muted">
        {{
          t('upgrade.scanProgress', {
            done: scan.scanned,
            total: scan.total,
          })
        }}
      </p>
    </UiPanel>

    <!-- Running / paused / finished -->
    <UiPanel
      v-else-if="hasQueue"
      :title="isRunning ? t('upgrade.working') : stateTitle"
    >
      <UiProgress :value="tracks.pct" :height="6" />
      <div
        class="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-muted"
      >
        <span class="font-semibold text-fg">
          {{
            t('upgrade.ofTracks', { done: tracks.done, total: tracks.total })
          }}
        </span>
        <span v-if="bytes.total">
          {{
            t('upgrade.ofBytes', {
              done: formatBytes(bytes.done),
              total: formatBytes(bytes.total),
            })
          }}
        </span>
        <span>{{ tracks.pct }}%</span>
      </div>
      <dl class="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[13px]">
        <div v-for="item in tallies" :key="item.key" class="flex gap-1.5">
          <dt class="text-muted">{{ item.label }}</dt>
          <dd class="font-semibold text-fg">{{ item.value }}</dd>
        </div>
      </dl>
      <p
        v-if="isRunning"
        class="mt-4 flex items-start gap-2 text-[13px] text-muted"
      >
        <AppIcon name="info" :size="16" class="mt-0.5 shrink-0" />
        {{ t('upgrade.longJob') }}
      </p>
    </UiPanel>

    <!-- Scan finished, waiting for the user to choose -->
    <template v-if="waitingToStart">
      <UiPanel :title="t('upgrade.scanResult')">
        <p class="text-[13px] text-muted">
          {{ t('upgrade.libraryCount', { count: summary.libraryTracks }) }}
          ·
          {{ t('upgrade.librarySize') }}:
          {{ formatBytes(summary.libraryBytes) }}
        </p>
        <div class="mt-4 flex flex-col gap-3">
          <div
            v-for="name in found"
            :key="name"
            class="flex items-start gap-3 rounded-control border border-line-2 bg-surface-2 p-3.5"
          >
            <UiSwitch
              :model-value="selected.includes(name)"
              :label="t(`upgrade.${name}`)"
              :description="t(`upgrade.${name}Hint`)"
              class="flex-1"
              @update:model-value="(on) => toggle(name, on)"
            />
            <UiBadge tone="accent">
              {{
                t('upgrade.tracksAffected', {
                  count: summary.categories[name] || 0,
                })
              }}
            </UiBadge>
          </div>
        </div>
      </UiPanel>

      <UiPanel :title="t('upgrade.options')">
        <div class="flex flex-col gap-4">
          <label class="flex flex-wrap items-center justify-between gap-3">
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-semibold text-fg">
                {{ t('upgrade.artworkMin') }}
              </span>
              <span class="mt-0.5 block text-[13px] text-muted">
                {{ t('upgrade.artworkMinHint') }}
              </span>
            </span>
            <UiSelect
              v-model="artworkMin"
              :options="sizeOptions"
              :label="t('upgrade.artworkMin')"
              icon="disc"
            />
          </label>
          <label class="flex flex-wrap items-center justify-between gap-3">
            <span class="min-w-0 flex-1 text-sm font-semibold text-fg">
              {{ t('upgrade.artworkSource') }}
            </span>
            <UiSelect
              v-model="artworkSource"
              :options="sourceOptions"
              :label="t('upgrade.artworkSource')"
              icon="palette"
            />
          </label>
          <label class="flex flex-wrap items-center justify-between gap-3">
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-semibold text-fg">
                {{ t('upgrade.recheck') }}
              </span>
              <span class="mt-0.5 block text-[13px] text-muted">
                {{ t('upgrade.recheckHint') }}
              </span>
            </span>
            <UiSelect
              v-model="recheckDays"
              :options="recheckOptions"
              :label="t('upgrade.recheck')"
              icon="clock"
            />
          </label>
          <label class="flex flex-wrap items-center justify-between gap-3">
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-semibold text-fg">
                {{ t('upgrade.metadataMode') }}
              </span>
              <span class="mt-0.5 block text-[13px] text-muted">
                {{ metadataModeHint }}
              </span>
            </span>
            <UiSelect
              v-model="metadataMode"
              :options="metadataModeOptions"
              :label="t('upgrade.metadataMode')"
              icon="tag"
            />
          </label>
        </div>
      </UiPanel>

      <div class="flex flex-col gap-3">
        <p class="flex items-start gap-2 text-[13px] text-pretty text-muted">
          <AppIcon name="lock" :size="16" class="mt-0.5 shrink-0" />
          {{ t('upgrade.safeNote') }}
        </p>
        <p class="flex items-start gap-2 text-[13px] text-pretty text-muted">
          <AppIcon name="info" :size="16" class="mt-0.5 shrink-0" />
          {{ t('upgrade.audioNote') }}
        </p>
        <div class="flex flex-wrap items-center gap-3">
          <UiButton
            variant="primary"
            size="lg"
            icon="wand"
            :disabled="!selected.length"
            @click="start"
          >
            {{ t('upgrade.startCount', { count: willRun }) }}
          </UiButton>
          <p v-if="!selected.length" class="text-[13px] text-muted">
            {{ t('upgrade.pickSomething') }}
          </p>
        </div>
      </div>
    </template>

    <!-- Nothing to do -->
    <UiEmpty
      v-else-if="state === 'ready'"
      icon="check"
      :title="t('upgrade.nothingTitle')"
      :body="
        summary.recentlyChecked
          ? t('upgrade.nothingCheckedBody', {
              count: summary.recentlyChecked,
            })
          : t('upgrade.nothingBody')
      "
    >
      <UiButton
        v-if="summary.recentlyChecked"
        variant="secondary"
        icon="refresh"
        :loading="loading"
        @click="rescanEverything"
      >
        {{ t('upgrade.recheckNever') }}
      </UiButton>
    </UiEmpty>

    <!-- Never scanned -->
    <UiEmpty
      v-else-if="!busy && !hasQueue"
      icon="sparkle"
      :title="t('upgrade.notScannedTitle')"
      :body="t('upgrade.notScannedBody')"
    >
      <UiButton
        variant="primary"
        icon="refresh"
        :loading="loading"
        @click="runScan"
      >
        {{ t('upgrade.scan') }}
      </UiButton>
    </UiEmpty>

    <!-- Merge split albums -->
    <UiPanel :title="t('upgrade.mergeTitle')">
      <p class="text-[13px] text-pretty text-muted">
        {{ t('upgrade.mergeHint') }}
      </p>
      <div class="mt-3 flex flex-wrap items-center gap-3">
        <UiButton
          variant="secondary"
          icon="refresh"
          :loading="mergeLoading"
          :disabled="mergeLoading || mergeBusy"
          @click="findSplitAlbums"
        >
          {{ t('upgrade.mergeFind') }}
        </UiButton>
        <UiButton
          variant="secondary"
          icon="wand"
          :loading="tidyBusy"
          :disabled="mergeLoading || mergeBusy || tidyBusy"
          @click="tidyLibrary"
        >
          {{ t('upgrade.tidyAction') }}
        </UiButton>
        <span v-if="mergeLoading" class="text-[13px] text-muted">
          {{ t('upgrade.mergeSearching') }}
        </span>
        <span v-else-if="mergeCandidates.length" class="text-[13px] text-muted">
          {{ t('upgrade.mergeFound', { count: mergeCandidates.length }) }}
        </span>
        <span v-else-if="mergeSearched" class="text-[13px] text-muted">
          {{ t('upgrade.mergeNone') }}
        </span>
      </div>
      <div
        v-if="mergeLoading || mergeBusy || tidyBusy"
        class="mt-3"
        aria-live="polite"
      >
        <div class="mb-2 flex justify-between text-[13px] text-muted">
          <span>{{ t(`upgrade.mergeStage${mergeProgress.stage}`) }}</span>
          <span v-if="mergeProgress.total">
            {{ mergeProgress.completed }} / {{ mergeProgress.total }}
          </span>
        </div>
        <UiProgress
          :value="
            mergeProgress.total
              ? (100 * mergeProgress.completed) / mergeProgress.total
              : 0
          "
          :indeterminate="!mergeProgress.total"
          :aria-label="t(`upgrade.mergeStage${mergeProgress.stage}`)"
          :height="6"
        />
      </div>
      <p v-if="mergeSearched" class="mt-1.5 text-[13px] text-muted">
        {{
          t('upgrade.mergeScanned', {
            tracks: mergeTracks,
            seconds: (mergeElapsedMs / 1000).toFixed(1),
          })
        }}
        <template v-if="mergeLookedUp > 0">
          ·
          {{
            t('upgrade.mergeStats', {
              resolved: mergeResolved,
              looked_up: mergeLookedUp,
            })
          }}
        </template>
      </p>
      <p
        v-if="mergeSearched && mergeLookedUp > 0 && mergeResolved === 0"
        class="mt-1.5 text-[13px] text-warn"
      >
        {{ t('upgrade.mergeLimited') }}
      </p>
      <ul v-if="mergeCandidates.length" class="mt-4 flex flex-col gap-3">
        <li
          v-for="(plan, index) in mergeCandidates"
          :key="`${plan.album_artist}-${plan.album}-${index}`"
          class="flex items-start gap-3 rounded-control border border-line-2 bg-surface-2 p-3.5"
        >
          <UiSwitch
            :model-value="mergeSelected.includes(index)"
            :label="plan.album"
            :description="plan.album_artist"
            class="flex-1"
            @update:model-value="(on) => toggleMerge(index, on)"
          >
            <span
              class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-muted"
            >
              <span v-if="plan.editions?.length">
                {{
                  t('upgrade.mergeEditions', {
                    editions: plan.editions.join(' · '),
                  })
                }}
              </span>
              <span v-if="plan.dropped_count" class="text-warn">
                {{
                  t('upgrade.mergeDuplicates', {
                    count: plan.dropped_count,
                  })
                }}
              </span>
            </span>
          </UiSwitch>
          <UiBadge tone="accent">
            {{ t('upgrade.tracksAffected', { count: plan.track_count }) }}
          </UiBadge>
        </li>
      </ul>
      <div
        v-if="mergeCandidates.length"
        class="mt-4 flex flex-wrap items-center gap-3"
      >
        <UiButton
          variant="primary"
          icon="wand"
          :disabled="!mergeSelected.length || mergeBusy || mergeLoading"
          :loading="mergeBusy"
          @click="applyMerge"
        >
          {{ t('upgrade.mergeApply', { count: mergeSelected.length }) }}
        </UiButton>
        <span v-if="mergeNote" class="text-[13px] text-muted">
          {{ mergeNote }}
        </span>
      </div>
      <p v-else-if="mergeNote" class="mt-3 text-[13px] text-muted">
        {{ mergeNote }}
      </p>
      <p class="mt-3 text-[13px] text-pretty text-muted">
        {{ t('upgrade.tidyHint') }}
      </p>
      <p v-if="tidyNote" class="mt-1.5 text-[13px] text-muted">
        {{ tidyNote }}
      </p>
    </UiPanel>

    <!-- Rebuild playlist M3Us from the library -->
    <UiPanel :title="t('upgrade.playlistsTitle')">
      <p class="text-[13px] text-pretty text-muted">
        {{ t('upgrade.playlistsHint') }}
      </p>
      <div class="mt-3 flex flex-wrap items-center gap-3">
        <UiButton
          variant="secondary"
          icon="refresh"
          :loading="playlistBusy"
          @click="rebuildPlaylists"
        >
          {{ t('upgrade.playlistsRebuild') }}
        </UiButton>
        <span v-if="playlistNote" class="text-[13px] text-muted">
          {{ playlistNote }}
        </span>
      </div>
    </UiPanel>

    <!-- Per-track rows -->
    <UiPanel v-if="jobs.length" :title="t('upgrade.tracksTitle')" padded>
      <ul class="flex flex-col divide-y divide-line-2">
        <li
          v-for="job in jobs"
          :key="job.file"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5 first:pt-0 last:pb-0"
        >
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm font-semibold text-fg">
              {{ job.title || job.file }}
            </span>
            <span class="block truncate text-[13px] text-muted">
              {{ job.artist }}
            </span>
          </span>
          <span v-if="job.detail" class="text-[13px] text-muted">
            {{ job.detail }}
          </span>
          <UiBadge :tone="toneFor(job)" :icon="iconFor(job)">
            {{ labelFor(job) }}
          </UiBadge>
        </li>
      </ul>
    </UiPanel>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppIcon from '/src/components/ui/AppIcon.vue'
import UiBadge from '/src/components/ui/UiBadge.vue'
import UiButton from '/src/components/ui/UiButton.vue'
import UiEmpty from '/src/components/ui/UiEmpty.vue'
import UiPanel from '/src/components/ui/UiPanel.vue'
import UiProgress from '/src/components/ui/UiProgress.vue'
import UiSelect from '/src/components/ui/UiSelect.vue'
import UiSwitch from '/src/components/ui/UiSwitch.vue'
import PageHeader from '/src/components/library/PageHeader.vue'
import { formatBytes } from '/src/lib/format'
import { ARTWORK_SIZES } from '/src/lib/upgrade'
import API from '/src/model/api'
import { useUpgrade } from '/src/model/upgrade'
import { useI18n } from '/src/i18n'

const { t } = useI18n()
const {
  status,
  state,
  counts,
  summary,
  jobs,
  tracks,
  bytes,
  scan,
  found,
  busy,
  selected,
  willRun,
  loading,
  error,
  load,
  startScan,
  start,
  pause,
  resume,
  cancel,
  refreshJobs,
  toggle,
  isScanning,
  isRunning,
  waitingToStart,
  canScan,
  canPause,
  canResume,
  canCancel,
} = useUpgrade()

const artworkMin = ref(600)
const artworkSource = ref('highest')
const recheckDays = ref(30)
const metadataMode = ref('fill')

const runScan = () =>
  startScan({
    artwork_min_px: artworkMin.value,
    artwork_source: artworkSource.value,
    recheck_days: recheckDays.value,
    metadata_mode: metadataMode.value,
  })

// "Nothing to upgrade" when everything was simply checked recently is a
// dead end without a way past the memory.
function rescanEverything() {
  recheckDays.value = 0
  return runScan()
}

onMounted(() => load())

// Show the values the last scan actually ran with, so re-running it
// from another device doesn't silently use different ones.
watch(
  () => status.value.options,
  (options) => {
    if (!options) return
    if (options.artwork_min_px) artworkMin.value = options.artwork_min_px
    if (options.artwork_source) artworkSource.value = options.artwork_source
    if (typeof options.recheck_days === 'number') {
      recheckDays.value = options.recheck_days
    }
    if (options.metadata_mode) metadataMode.value = options.metadata_mode
  },
  { immediate: true }
)

const hasRun = computed(() => state.value !== 'idle')
const hasQueue = computed(
  () =>
    counts.value.total > 0 &&
    ['running', 'paused', 'done', 'cancelled'].includes(state.value)
)

const stateTitle = computed(
  () =>
    ({
      paused: t('upgrade.paused'),
      done: t('upgrade.done'),
      cancelled: t('upgrade.cancelled'),
    })[state.value] || t('upgrade.working')
)

const tallies = computed(() =>
  [
    { key: 'completed', value: counts.value.completed },
    { key: 'skipped', value: counts.value.skipped },
    { key: 'failed', value: counts.value.failed },
    { key: 'queued', value: counts.value.queued },
  ].map((item) => ({ ...item, label: t(`upgrade.${item.key}`) }))
)

const sizeOptions = computed(() =>
  ARTWORK_SIZES.map((px) => ({ value: px, label: `${px}px` }))
)

const sourceOptions = computed(() => [
  { value: 'highest', label: t('upgrade.highest') },
  { value: 'spotify', label: t('upgrade.preferSpotify') },
  { value: 'itunes', label: t('upgrade.preferItunes') },
  { value: 'youtube-music', label: t('upgrade.preferYoutube') },
])

const recheckOptions = computed(() => [
  { value: 0, label: t('upgrade.recheckNever') },
  ...[7, 30, 90, 365].map((days) => ({
    value: days,
    label: t('upgrade.recheckDays', { count: days }),
  })),
])

const metadataModeOptions = computed(() => [
  { value: 'fill', label: t('upgrade.metadataModeFill') },
  { value: 'verify', label: t('upgrade.metadataModeVerify') },
])

const metadataModeHint = computed(() =>
  metadataMode.value === 'verify'
    ? t('upgrade.metadataModeVerifyHint')
    : t('upgrade.metadataModeFillHint')
)

// ── Merge split albums (album-level, outside the per-track queue) ────
const mergeLoading = ref(false)
const mergeSearched = ref(false)
const mergeCandidates = ref([])
const mergeSelected = ref([])
const mergeBusy = ref(false)
const mergeNote = ref('')
const mergeLookedUp = ref(0)
const mergeResolved = ref(0)
const mergeTracks = ref(0)
const mergeElapsedMs = ref(0)
const mergeProgress = ref({ stage: 'reading', completed: 0, total: 0 })
const tidyBusy = ref(false)
const tidyNote = ref('')
let mergeProgressTimer
let mergeOperationId = ''

function stopMergeProgress() {
  mergeOperationId = ''
  clearTimeout(mergeProgressTimer)
}

function startMergeProgress(stage) {
  stopMergeProgress()
  const id =
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now()}-${Math.random().toString(36).slice(2)}`
  mergeOperationId = id
  mergeProgress.value = { stage, completed: 0, total: 0 }
  const poll = async () => {
    try {
      const res = await API.getAlbumMergeProgress(id)
      if (mergeOperationId !== id) return
      if (
        [
          'reading',
          'matching',
          'grouping',
          'rehoming',
          'merging',
          'playlists',
        ].includes(res.data?.stage)
      ) {
        mergeProgress.value = res.data
      }
    } catch {
      // A temporary polling error must not interrupt the merge itself.
    }
    if (mergeOperationId === id) {
      mergeProgressTimer = setTimeout(poll, 700)
    }
  }
  mergeProgressTimer = setTimeout(poll, 300)
  return id
}

onBeforeUnmount(stopMergeProgress)

const describeError = (err) =>
  err?.response?.data?.detail || err?.message || 'failed'

async function findSplitAlbums() {
  if (mergeLoading.value || mergeBusy.value) return
  mergeLoading.value = true
  mergeNote.value = ''
  try {
    const res = await API.findSplitAlbums(startMergeProgress('reading'))
    mergeCandidates.value = res.data?.candidates || []
    mergeSelected.value = mergeCandidates.value.map((_, index) => index)
    mergeLookedUp.value = res.data?.looked_up || 0
    mergeResolved.value = res.data?.resolved || 0
    mergeTracks.value = res.data?.tracks || 0
    mergeElapsedMs.value = res.data?.elapsed_ms || 0
    mergeSearched.value = true
  } catch (err) {
    mergeNote.value = describeError(err)
  } finally {
    stopMergeProgress()
    mergeLoading.value = false
  }
}

function toggleMerge(index, on) {
  const next = new Set(mergeSelected.value)
  if (on) next.add(index)
  else next.delete(index)
  mergeSelected.value = [...next].sort((a, b) => a - b)
}

async function applyMerge() {
  if (mergeLoading.value || mergeBusy.value) return
  const plans = mergeSelected.value
    .map((index) => mergeCandidates.value[index])
    .filter(Boolean)
  if (!plans.length) return
  mergeBusy.value = true
  mergeNote.value = ''
  try {
    const res = await API.mergeAlbums(plans, startMergeProgress('merging'))
    mergeNote.value = t('upgrade.mergeDone', {
      moved: res.data?.moved || 0,
      deleted: res.data?.deleted || 0,
    })
    mergeCandidates.value = []
    mergeSelected.value = []
    mergeSearched.value = false
    await load()
    await refreshJobs()
  } catch (err) {
    mergeNote.value = describeError(err)
  } finally {
    stopMergeProgress()
    mergeBusy.value = false
  }
}

async function tidyLibrary() {
  if (mergeLoading.value || mergeBusy.value || tidyBusy.value) return
  tidyBusy.value = true
  tidyNote.value = ''
  try {
    const res = await API.tidyLibrary(startMergeProgress('rehoming'))
    tidyNote.value = t('upgrade.tidyDone', {
      rehomed: res.data?.rehomed || 0,
      duplicates: res.data?.duplicates_removed || 0,
      folders: res.data?.folders_removed || 0,
    })
    const left =
      (res.data?.remaining || 0) + (res.data?.unresolved || 0)
    if (left) {
      tidyNote.value += ` ${t('upgrade.tidyRemaining', { count: left })}`
    }
    await load()
    await refreshJobs()
  } catch (err) {
    tidyNote.value = describeError(err)
  } finally {
    stopMergeProgress()
    tidyBusy.value = false
  }
}

// ── Rebuild playlist M3Us from the library ───────────────────────────
const playlistBusy = ref(false)
const playlistNote = ref('')

async function rebuildPlaylists() {
  playlistBusy.value = true
  playlistNote.value = ''
  try {
    const res = await API.rebuildLibraryPlaylists()
    playlistNote.value = t('upgrade.playlistsDone', {
      rebuilt: res.data?.rebuilt || 0,
      linked: res.data?.tracks_linked || 0,
    })
  } catch (err) {
    playlistNote.value = describeError(err)
  } finally {
    playlistBusy.value = false
  }
}

const JOB_TONES = {
  done: 'accent',
  failed: 'danger',
  running: 'warn',
  queued: 'neutral',
  skipped: 'neutral',
}
const JOB_ICONS = {
  done: 'check',
  failed: 'alert',
  running: 'refresh',
  queued: 'clock',
  skipped: 'minus',
}
const STAGE_KEYS = {
  matching: 'upgrade.stageMatching',
  artwork: 'upgrade.stageArtwork',
  lyrics: 'upgrade.stageLyrics',
  writing: 'upgrade.stageWriting',
}

const toneFor = (job) => JOB_TONES[job.status] || 'neutral'
const iconFor = (job) => JOB_ICONS[job.status] || ''

function labelFor(job) {
  if (job.status === 'running' && STAGE_KEYS[job.stage]) {
    return t(STAGE_KEYS[job.stage])
  }
  return t(`upgrade.${job.status === 'done' ? 'completed' : job.status}`)
}
</script>
