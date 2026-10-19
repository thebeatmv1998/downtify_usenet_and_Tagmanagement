<template>
  <UiModal
    :open="Boolean(track)"
    :title="t('tags.title')"
    :description="track ? t('tags.description', { title: track.title }) : ''"
    width="sm:max-w-2xl"
    @close="close"
  >
    <p v-if="error" class="text-[13px] text-danger" role="alert">
      {{ error }}
    </p>
    <p v-else-if="loading" class="text-[13px] text-muted">
      {{ t('tags.loading') }}
    </p>

    <div v-else class="flex flex-col gap-4">
      <!-- Cover -->
      <div class="flex items-center gap-4">
        <img
          v-if="coverShown"
          :src="coverPreview"
          alt=""
          class="h-24 w-24 shrink-0 rounded-control border border-line-2 object-cover"
        />
        <div
          v-else
          class="flex h-24 w-24 shrink-0 items-center justify-center rounded-control border border-line-2 bg-surface-2 text-faint"
        >
          <AppIcon name="disc" :size="28" />
        </div>
        <div class="flex flex-col items-start gap-2">
          <input
            ref="coverInput"
            type="file"
            accept="image/*"
            class="hidden"
            @change="onCoverPick"
          />
          <UiButton
            variant="secondary"
            icon="upload"
            @click="coverInput?.click()"
          >
            {{ t('tags.changeCover') }}
          </UiButton>
          <UiButton
            v-if="coverShown"
            variant="ghost"
            icon="trash"
            @click="markRemoveCover"
          >
            {{ t('tags.removeCover') }}
          </UiButton>
        </div>
      </div>

      <!-- Fields -->
      <div class="grid gap-3 sm:grid-cols-2">
        <UiInput v-model="form.title" :label="t('tags.fieldTitle')" />
        <UiInput v-model="form.artist" :label="t('tags.fieldArtist')" />
        <UiInput v-model="form.album" :label="t('tags.fieldAlbum')" />
        <UiInput
          v-model="form.album_artist"
          :label="t('tags.fieldAlbumArtist')"
        />
        <div class="grid grid-cols-2 gap-3">
          <UiInput
            v-model="form.track_number"
            type="number"
            min="0"
            :label="t('tags.fieldTrack')"
          />
          <UiInput
            v-model="form.track_total"
            type="number"
            min="0"
            :label="t('tags.fieldTotal')"
          />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <UiInput
            v-model="form.disc_number"
            type="number"
            min="0"
            :label="t('tags.fieldDisc')"
          />
          <UiInput
            v-model="form.disc_total"
            type="number"
            min="0"
            :label="t('tags.fieldDiscTotal')"
          />
        </div>
        <UiInput v-model="form.date" :label="t('tags.fieldDate')" />
        <UiInput v-model="form.genre" :label="t('tags.fieldGenre')" />
        <UiInput v-model="form.composer" :label="t('tags.fieldComposer')" />
        <UiInput v-model="form.comment" :label="t('tags.fieldComment')" />
      </div>

      <!-- Fill from a tag source -->
      <div class="rounded-control border border-line-2 bg-surface-2 p-3.5">
        <p class="text-sm font-semibold text-fg">{{ t('tags.searchTitle') }}</p>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <UiSelect
            v-model="source"
            :options="sourceOptions"
            :label="t('tags.fieldSource')"
          />
          <div class="min-w-[12rem] flex-1">
            <UiInput
              v-model="query"
              :placeholder="t('tags.searchPlaceholder')"
              @keyup.enter="runSearch"
            />
          </div>
          <UiButton icon="search" :loading="searching" @click="runSearch">
            {{ t('tags.search') }}
          </UiButton>
        </div>
        <p v-if="searchError" class="mt-2 text-[13px] text-danger">
          {{ searchError }}
        </p>
        <ul
          v-if="results.length"
          class="mt-3 flex flex-col divide-y divide-line-2"
        >
          <li
            v-for="(result, index) in results"
            :key="index"
            class="flex items-center gap-3 py-2"
          >
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-semibold text-fg">
                {{ result.album || result.title }}
              </span>
              <span class="block truncate text-[13px] text-muted">
                {{ result.album_artist || result.artist }}
                <template v-if="result.year"> · {{ result.year }}</template>
              </span>
            </span>
            <UiButton
              size="sm"
              variant="secondary"
              :loading="applying === index"
              @click="useResult(result, index)"
            >
              {{ t('tags.use') }}
            </UiButton>
          </li>
        </ul>
        <p
          v-else-if="searched && !searching"
          class="mt-2 text-[13px] text-muted"
        >
          {{ t('tags.noResults') }}
        </p>
      </div>
    </div>

    <template #footer>
      <UiButton variant="ghost" @click="close">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton
        variant="primary"
        icon="check"
        :loading="saving"
        :disabled="loading || !track"
        @click="save"
      >
        {{ t('tags.save') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

import AppIcon from '/src/components/ui/AppIcon.vue'
import UiButton from '/src/components/ui/UiButton.vue'
import UiInput from '/src/components/ui/UiInput.vue'
import UiModal from '/src/components/ui/UiModal.vue'
import UiSelect from '/src/components/ui/UiSelect.vue'
import API from '/src/model/api'
import { useLibrary } from '/src/model/library'
import { useTagEditor } from '/src/model/tagEditor'
import { useUi } from '/src/model/ui'
import { useI18n } from '/src/i18n'

const { t } = useI18n()
const ui = useUi()
const library = useLibrary()
const editor = useTagEditor()
const track = editor.track

const FIELDS = [
  'title',
  'artist',
  'album',
  'album_artist',
  'track_number',
  'track_total',
  'disc_number',
  'disc_total',
  'date',
  'genre',
  'comment',
  'composer',
]

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const form = ref({})
const tags = ref(null)
const hasCover = ref(false)
const coverFile = ref('')
const removeCover = ref(false)
const coverVersion = ref(0)
const coverInput = ref(null)

const source = ref('musicbrainz')
const query = ref('')
const results = ref([])
const searching = ref(false)
const searched = ref(false)
const searchError = ref('')
const applying = ref(-1)

const sourceOptions = computed(() => [
  { value: 'musicbrainz', label: t('tags.sourceMusicbrainz') },
  { value: 'discogs', label: t('tags.sourceDiscogs') },
])

const coverShown = computed(() => {
  if (coverFile.value) return true
  if (removeCover.value) return false
  return hasCover.value
})

const coverPreview = computed(() => {
  if (coverFile.value) return coverFile.value
  if (!track.value) return ''
  return `/cover?file=${encodeURIComponent(track.value.file)}&v=${coverVersion.value}`
})

function detail(err) {
  return err?.response?.data?.detail || err?.message || 'failed'
}

function reset() {
  error.value = ''
  results.value = []
  searched.value = false
  searchError.value = ''
  applying.value = -1
  coverFile.value = ''
  removeCover.value = false
  form.value = {}
  tags.value = null
  hasCover.value = false
}

async function load() {
  if (!track.value) return
  loading.value = true
  error.value = ''
  try {
    const res = await API.getTrackTags(track.value.file)
    const data = res.data?.tags || {}
    tags.value = data
    hasCover.value = Boolean(data.has_cover)
    form.value = { ...data }
    query.value = [data.artist, data.title].filter(Boolean).join(' ')
    coverVersion.value += 1
  } catch (err) {
    error.value = detail(err)
  } finally {
    loading.value = false
  }
}

function onCoverPick(event) {
  const file = event.target.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    coverFile.value = String(reader.result || '')
    removeCover.value = false
  }
  reader.readAsDataURL(file)
  event.target.value = ''
}

function markRemoveCover() {
  coverFile.value = ''
  removeCover.value = true
  hasCover.value = false
}

function normalise(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/\([^)]*\)|\[[^\]]*\]/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
}

async function runSearch() {
  searching.value = true
  searched.value = false
  searchError.value = ''
  results.value = []
  try {
    const res = await API.searchTags({
      source: source.value,
      q: query.value,
      title: form.value.title || '',
      artist: form.value.artist || '',
    })
    results.value = res.data?.results || []
    searched.value = true
  } catch (err) {
    searchError.value = detail(err)
  } finally {
    searching.value = false
  }
}

async function useResult(result, index) {
  applying.value = index
  try {
    if (result.album) form.value.album = result.album
    if (result.album_artist) form.value.album_artist = result.album_artist
    if (result.date) form.value.date = result.date
    else if (result.year) form.value.date = result.year
    if (result.genre) form.value.genre = result.genre
    if (result.release_id) {
      const res = await API.getTagRelease(result.source, result.release_id)
      const data = res.data || {}
      if (data.album) form.value.album = data.album
      if (data.album_artist) form.value.album_artist = data.album_artist
      if (data.date) form.value.date = data.date
      if (data.genre) form.value.genre = data.genre
      if (data.track_total) form.value.track_total = data.track_total
      const wanted = normalise(form.value.title)
      const match = (data.tracks || []).find(
        (entry) => normalise(entry.title) === wanted
      )
      if (match) {
        if (match.position) form.value.track_number = match.position
        if (match.disc) form.value.disc_number = match.disc
      }
    }
    ui.toast(t('tags.filled'), { kind: 'success' })
  } catch (err) {
    searchError.value = detail(err)
  } finally {
    applying.value = -1
  }
}

async function save() {
  if (!track.value) return
  saving.value = true
  error.value = ''
  const fields = {}
  for (const key of FIELDS) fields[key] = form.value[key] ?? ''
  const payload = { file: track.value.file, fields }
  if (coverFile.value) payload.cover = coverFile.value
  if (removeCover.value) payload.remove_cover = true
  try {
    const res = await API.writeTrackTags(payload)
    const data = res.data?.tags || {}
    tags.value = data
    form.value = { ...data }
    hasCover.value = Boolean(data.has_cover)
    coverFile.value = ''
    removeCover.value = false
    coverVersion.value += 1
    ui.toast(t('tags.saved'), { kind: 'success' })
    library.refreshSoon(500)
  } catch (err) {
    error.value = detail(err)
    ui.toast(detail(err), { kind: 'error' })
  } finally {
    saving.value = false
  }
}

function close() {
  editor.close()
}

watch(
  track,
  (value) => {
    reset()
    if (value) load()
  },
  { immediate: true }
)
</script>
