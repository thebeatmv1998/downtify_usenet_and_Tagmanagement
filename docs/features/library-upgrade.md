---
icon: lucide/wand-sparkles
---

# Upgrade library

A library built up over time carries whatever each Downtify version could do when a track was downloaded: the smallest cover a source offered, no lyrics, tags without an album or a year, an album folder with no cover image. **Upgrade library** looks at what is already on disk and repairs only what is behind — no re-downloading, and nothing to sort out by hand.

Open it from **Library → Upgrade library**.

::: info Audio files are never replaced
An upgrade rewrites **artwork, lyrics and tags** (and writes album folder covers). It does not fetch a different audio file, so a track that was matched to the wrong song, a live version or a remix stays as it is — use [Replace audio](replace-audio.md) on that track to pick the right version. See [Why a normal upgrade doesn't replace audio](#why-a-normal-upgrade-doesnt-replace-audio). The one exception is [Merge split albums](#merge-split-albums), which moves and retags whole albums after a preview.
:::

## How it works

1. **Scan.** Downtify reads every library track and reports what is behind, per category. Nothing is written.
2. **Choose.** Each category the scan found something for is listed with its track count and a switch. Untick what you don't want.
3. **Upgrade.** The chosen tracks go into a queue that is worked through one at a time. You can pause, resume or stop it, and it survives a restart.

## What it repairs

| Category | What it does | Where it looks |
|----------|--------------|----------------|
| **Upgrade artwork** | Replaces a cover smaller than your target size | Spotify, Apple Music (iTunes), YouTube Music |
| **Add missing lyrics** | Fills in lyrics for tracks that have none, embedded and as an `.lrc` sidecar | Your [lyrics providers](lyrics.md), in your order |
| **Refresh tags** | Fills in or corrects album, album artist, release date, track number and genre — including the album older single-track downloads are missing | Spotify; Apple Music (iTunes) for genre and tracks without a Spotify ID |
| **Add album folder covers** | Writes a `cover.jpg`/`cover.png` into an album folder that has audio but no image of its own | The tracks' own embedded covers, then Spotify / Apple Music (iTunes) / YouTube Music |

A track counts as behind when:

- its embedded cover's short side is **smaller than the target size** (a track with no cover at all counts too);
- it has **neither** embedded lyrics **nor** an `.lrc` sidecar (next to the file or in the lyrics folder from Settings);
- any of **album**, **album artist**, **year**, **track number** or **genre** is missing from its tags — or holds a placeholder like *Unknown album*, *unknown* or *unbekannt*, which carries no more information than an empty tag (*Various Artists* is a legitimate album artist and does **not** count);
- its **album folder** has tracks but no cover image (see [Album folder covers](#album-folder-covers)).

**Refresh tags** re-reads tracks Downtify still has a Spotify track ID for from Spotify (see the [track index](library-catalog.md#what-gets-stored)). Everything else falls back to an Apple Music (iTunes) search, which matches on artist and title and cross-checks the duration before trusting a result. Tracks no source knows come back as *Nothing to do*.

Genre is also fetched from Apple Music when Spotify has none, with results cached per artist/album. **Fill in missing tags** preserves an existing genre; **Verify and correct tags** can replace it. An unavailable genre never clears an existing value. If a recent scan skipped these tracks, choose **Always check every track** to include them again.

## Options

### Upgrade artwork below

`300px` · `500px` · **`600px`** · `1000px` · `1200px`

Covers at this size or larger are left alone. Downtify only writes a cover that is **strictly bigger** than the one already in the file, so running an upgrade twice never rewrites anything for nothing.

A track with **no cover at all** is never left bare: when no source knows any artwork for it, Downtify embeds its bundled placeholder image instead — some players (Navidrome clients, for example) misbehave on coverless files. New downloads follow the same rule.

Where the sizes come from:

| Source | Typical size |
|--------|--------------|
| **Apple Music (iTunes)** | 1200px and up |
| **Spotify** | up to 640px |
| **YouTube Music** | up to the [cover resolution](download-settings.md) setting, ~1200px at most |

This is also why a library downloaded before is often stuck at 300–640px: Spotify publishes its covers in fixed sizes, and the largest one it offers is 640px.

### Artwork source

| Choice | Behaviour |
|--------|-----------|
| **Highest resolution available** (default) | Asks every source and keeps the biggest image |
| **Prefer Spotify** / **Prefer Apple Music** / **Prefer YouTube Music** | Uses that source when it has anything at all; the others are only a fallback for when it has nothing |

### Metadata mode

| Choice | Behaviour |
|--------|-----------|
| **Fill in missing tags** (default) | Only completes what is empty or a placeholder (*Unknown album*, *unknown*, *unbekannt*, *various*). Values already in the file are kept |
| **Verify and correct tags** | Compares every track with Spotify or Apple Music and rewrites album, album artist, year and track number when they disagree |

The mode changes what the scan queues, too: in **fill** a track only shows up when its tags are visibly incomplete, because wrong values can't be told from right ones on disk. **Verify** queues every track whose metadata check is due — each one costs a remote lookup (Spotify, or an Apple Music search when no Spotify track is known), so a verify run over a large library is noticeably slower than a fill run. The per-track result says what was corrected, e.g. `metadata: album "Unknown album" → "Discovery"`.

When no source knows an album for the track, the album tag is set to the track's own **title** — a single's album is its own name, so the tag is never left empty or a placeholder after an upgrade. New downloads follow the same rule.

### Album folder covers

Some media servers read a folder's cover image rather than the artwork embedded in each file, so a library whose tracks all carry covers can still show an empty album tile when the folder has no image of its own. This category fills that gap **without touching the audio**: it drops the artwork already embedded in the folder's tracks into `cover.jpg` (or, when there is none, the best a tag source offers, or the bundled placeholder).

- A track whose file sits in a **library root** (a flat library, not an album folder) has no album folder of its own and is left alone.
- The file is written **once per album folder**, so a folder full of tracks counts as one thing to repair, not one per file.
- An existing `cover.*`/`folder.*`/`front.*` image is never overwritten.

### Don't check a track again for

`Always check every track` · `7 days` · **`30 days`** · `90 days` · `365 days`

After a track has been looked at, it is skipped by the next scan for this long, so a second run over a large library doesn't ask the same sources about the same tracks again.

The memory is kept **per track and per category**, which is what makes partial runs work: repairing only the artwork of 16,000 tracks says nothing about whether anyone ever went looking for their lyrics, so a later lyrics run still considers every one of them. A scan also records the categories it examined and found nothing to do for — looking and finding nothing is a check like any other.

Downtify remembers, for each track and category: when it was checked, the Downtify version that checked it, and — for artwork — the largest cover found and where it came from. Which lyrics providers were already asked is kept separately, per song and provider (see [Lyrics](lyrics.md)).

Two things always override it:

- a **newer Downtify version** re-checks everything, since it may match or tag better than the one that ran before;
- choosing **Always check every track** ignores the memory for that scan.

When a scan finds nothing left to do *because* everything was checked recently, it says so and offers to run again without the memory, rather than leaving you with an unexplained "nothing to upgrade".

## While it runs

The progress panel shows tracks done out of the queue, an approximate data figure (`8.4 GB of 51.3 GB`) and how many were upgraded, had nothing to do, or failed. Each track shows what it is doing — *Matching*, *Looking for artwork*, *Looking for lyrics*, *Writing the file* — and ends with what changed, e.g. `artwork 1200px (itunes)`.

**Pause** stops after the track in flight and keeps the queue. **Stop** drops the rest of it; tracks already upgraded stay upgraded. You can leave the page — the run keeps going, and any other open tab follows along.

The queue lives in `/data/downtify_library.db`, so **restarting Downtify (or the container) mid-run picks it back up** where it stopped. A track that was in flight when the process died is put back in the queue.

## Nothing is lost if an upgrade fails

Every write goes to a working copy beside the original (`<name>.downtify-upgrade.<ext>`). The copy has to open as audio of the same length before it is moved into place; otherwise it is thrown away and the original is left untouched. That file never shows up as a track of its own, and it is gone as soon as the track finishes.

The original **modification time is kept**, so upgrading a library doesn't reshuffle *Recently added*.

### Read-only music folders

An upgrade rewrites tags in place, so tracks on a read-only filesystem are **skipped**, with an explanation, before looking up metadata, artwork or lyrics. They remain playable and in their playlists; their files and mount permissions are left unchanged. A skipped read-only track is not remembered as successfully checked, so it can be considered again if its folder becomes writable later. Unexpected permission, disk-space and I/O errors still appear as failures.

This differs from [existing-folder enrichment](external-library.md), which can keep artwork and lyrics separately in Downtify's data folder without rewriting protected audio files.

## Why a normal upgrade doesn't replace audio

Swapping the audio file a user already has is a different problem from rewriting its tags: it needs match confidence, a review step for uncertain matches, and every M3U, Navidrome playlist and catalog entry that points at the old filename has to follow it. Doing that half-way risks overwriting a good file with a worse one, so the per-track upgrade deliberately leaves the audio alone — [Merge split albums](#merge-split-albums) below is the one deliberate exception, and it asks first.

## Merge split albums

An album that was downloaded one single at a time often ends up filed under the *song's* own title, so a player shows a dozen one-track albums instead of the album they belong to. The same album also often sits in the library twice, once as the standard edition and once as a deluxe edition with bonus tracks. **Merge split albums** groups tracks by their album title with edition markers (`Deluxe`, `Expanded`, `Special`, `Remastered`, release years such as `(1982)`, ...) removed, so both cases become one album. The same normalization is applied to album folder names when the tags already agree, so `Thriller` and `Thriller (1982)` are recognized even if both folders carry the tag `Thriller`.

The edition with the most distinct songs wins. Every song is kept once: FLAC/ALAC beats lossy, then higher bitrate/sample rate wins, and the largest file breaks a tie. Duplicate copies are marked as such in the preview and deleted only after the kept copy has been moved into the album folder successfully. Copies whose lengths differ by more than five seconds are treated as different recordings (an extended mix or a live version), so both survive.

Albums whose names are not just an edition marker but still belong together are
recognized by their songs: when one title extends the other with edition words
(`GUTS` / `GUTS (Spilled)`, `… (Deluxe)`, `… – Complete Edition`) **and** most
of the smaller release's songs also appear in the larger one, both are merged.
That catches deluxe/spilled editions with their own chart entry while keeping
genuinely different releases apart: `Global Warming` and `Global Warming
Meltdown` share only a name prefix, and live/acoustic/remix/karaoke albums are
excluded from the name relation entirely, so they stay separate records.

1. Click **Find split albums**. Nothing is written. Standard/deluxe editions and duplicate copies are found from the local tags with no network request at all; only tracks that look like standalone singles are looked up, in parallel and cached. A repeated scan therefore costs almost nothing.
2. Each candidate is listed with its editions, the resulting track count and how many duplicate files will be removed. Untick what you don't want. During scanning and merging, a progress bar shows the current stage and completed work. Reading the library and refreshing playlists use an animated bar until their size is known.
3. **Merge** rewrites album, album artist, year and track number (the winning edition's order, with bonus tracks appended), moves the kept files into one `Artist/Album/` folder (following your [file organization](file-organization.md) layout), deletes the losing duplicate copies, and refreshes the M3Us and Navidrome playlists that pointed at the old paths. A `.lrc` sidecar moves with its track; an album folder left without audio has its cover and empty parents removed, and the merge finishes by deleting every empty folder left below the library roots.

Tracks that were downloaded without album metadata live in an `unknown` folder
(or carry an `unknown` album tag). **Re-home unknown & clean folders** resolves
their real album from Spotify, Deezer or iTunes, moves and retags them into the
normal output-template folder, and removes the `unknown` folder once it is
empty. Before any network request it first looks for the same artist and title
in a correctly tagged library copy, so duplicated songs are fixed offline. A
copy that already exists at the destination is kept; the unknown copy is
deleted instead of producing a `(2)` file, and any other empty album folders
are removed at the end. Folders that only still hold a cover, lyrics or a
release `.nfo` count as empty and are cleaned up too.

New downloads also look the album up before they are filed: when a track's tags
carry no album, Deezer/iTunes are asked once and the result is used for both the
folder and the tags, so fresh downloads no longer create `unknown` folders.

The cleanup processes **every** unknown track (no 500-track cutoff) and shows a
progress bar while it works. A title tag that still contains the artist prefix
(`Olivia Rodrigo - brutal`) is matched as `brutal`, so it lines up with the
correctly tagged copy and with the tag sources. The result line reports how many
tracks were filed, how many duplicate copies were removed, how many empty
folders were deleted, and how many tracks no source could place.

Use this after fixing a batch that downloaded without album tags; the merge
preview itself never needs it.

A track the tag sources can't place with confidence is left where it is; the preview never guesses. The preview reports how many tracks a source matched and how long the scan took. If a source was rate-limited (Apple Music refuses a burst of requests), the count is low and a note tells you to try again shortly. Deezer is asked before Apple Music because it answers faster, and every successful answer is cached for the life of the process.

::: tip This is the one upgrade that moves files
Everything else on this page rewrites tags in place. A merge renames and moves the audio and deletes duplicate copies, which is why it always shows a preview and is never part of the per-track queue.
:::

## Rebuild playlist files

A playlist's `.m3u` is written from the tracks its download produced. Songs your library already had (downloaded for another playlist, as part of an album, or as a single) were skipped, so they were missing from the file even though they are on disk.

**Rebuild playlist files** re-reads every known playlist's full track list from Spotify, YouTube Music or Deezer and rewrites its `.m3u` with every track that resolves to a file in the library — matched by Spotify id, by the expected filename, or by artist and title. It also records the tracks it found on their watch, so the next monitor sweep keeps them.

This is a one-off repair for playlists built before the fix. New playlist downloads already write their full track list.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/library/upgrade` | Run state, queue counts and what the scan found |
| `GET` | `/api/library/upgrade/jobs` | Per-track rows (`status`, `stage`, `detail`) |
| `POST` | `/api/library/upgrade/scan` | Scan the library; writes nothing |
| `POST` | `/api/library/upgrade/start` | Start the run for the chosen `categories` |
| `POST` | `/api/library/upgrade/pause` | Stop after the current track, keep the queue |
| `POST` | `/api/library/upgrade/resume` | Continue a paused run |
| `POST` | `/api/library/upgrade/cancel` | Drop the rest of the queue |
| `POST` | `/api/library/albums/merge/preview` | Album merge candidates; writes nothing |
| `POST` | `/api/library/albums/merge` | Merge the confirmed albums (body `{plans}`) |
| `POST` | `/api/library/albums/tidy` | Re-home `unknown` tracks, delete duplicates and empty folders |
| `POST` | `/api/library/playlists/rebuild` | Rewrite every known playlist's M3U from the library |

Progress is also pushed over the [WebSocket](../api-reference.md#websocket) as `{"type": "library_upgrade", "upgrade": {…}}`.

See the [API reference](../api-reference.md#library) for request and response shapes.
