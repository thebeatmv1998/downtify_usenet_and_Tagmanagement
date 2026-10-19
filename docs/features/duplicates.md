---
icon: lucide/copy
---

# Duplicate finder

A library built from playlists, albums and singles accumulates the same
song more than once: downloaded for two playlists, tagged slightly
differently, saved under two folders. **Duplicates** finds those so you can
review and delete the copies. Open it from **Library → Duplicates**
(admin-only).

Nothing is deleted on its own — the page only lists what it found.

## What it looks for

| Section | Grouped by | A group appears when |
|---------|-----------|----------------------|
| **Duplicate titles** | Artist + title | The same song is on disk more than once |
| **Duplicate albums** | Album artist + album | The album is saved in **more than one folder**, or holds the **same song twice** |

Matching titles is deliberately loose, the same key "already downloaded"
detection uses: `Song` and `Song (Remastered)` are the same track, while a
cover by a different artist — or a distinct `Song (Live)` — is not.

## Reviewing and deleting

Each group lists every file with its album, codec and size. Tick the copies
to remove, or **Select all but one** to keep the first and flag the rest,
then **Delete selected**. The files are removed from disk the normal way:
their playlists and M3Us are updated, sidecars and orphaned covers are
cleaned up, and the Library refreshes.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/library/duplicates` | Duplicate titles and albums in the library |

Deleting uses the existing `DELETE /delete/batch` endpoint documented in the [API reference](../api-reference.md).
