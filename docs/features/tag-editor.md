---
icon: lucide/tag
---

# Tag editor

Fix a track's own tags without leaving Downtify. Open a track's menu (the
kebab on its row, or right-click) and pick **Edit tags…**. The editor is
admin-only, like the rest of the Library maintenance tools.

::: info Tags only — the audio is never touched
The editor rewrites the file's tag block in place (the way a desktop
tagger does). It does not re-encode, replace or move the audio.
:::

## What you can edit

| Field | Notes |
|-------|-------|
| **Title**, **Artist** | Several artists are separated with `;` |
| **Album**, **Album artist** | |
| **Track** / **Tracks total**, **Disc** / **Discs total** | Written as `n/total` where the format supports it |
| **Date** | `YYYY` or `YYYY-MM-DD` |
| **Genre**, **Composer**, **Comment** | |
| **Cover** | Upload a new image, or remove the embedded one |

Every field is optional: an empty one is removed rather than written as an
empty tag, and a field you don't change is left exactly as it was.

MP3 (ID3), M4A/MP4, FLAC and Ogg Vorbis/Opus are supported. Saving refreshes
the Library, its cover and the file's entry in the metadata cache
immediately.

## Filling tags from a source

The **Fill from a tag source** box searches the title and artist you have
open against:

- **MusicBrainz** — keyless. Downtify sends a descriptive user agent and
  keeps to MusicBrainz' one-request-a-second limit.
- **Discogs** — needs a personal access token; add one under **Settings →
  Tags**. Without it the Discogs tab explains what's missing.

Pick a release and Downtify fetches its tracklist, then fills **Album**,
**Album artist**, **Date** and **Genre**. When the current title matches one
of the release's tracks, the **Track** and **Disc** numbers are filled in
too. Review anything it fills before saving — the editor never saves on its
own.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/library/tags?file=…` | Every editable tag of one track |
| `POST` | `/api/library/tags` | Write tags: `{file, fields, cover?, remove_cover?}` |
| `GET` | `/api/library/tags/search` | `source` (`musicbrainz`/`discogs`), `q`/`title`/`artist` → candidates |
| `GET` | `/api/library/tags/release` | `source`, `id` → album metadata and ordered tracklist |

See the [API reference](../api-reference.md#tag-editor) for shapes.
