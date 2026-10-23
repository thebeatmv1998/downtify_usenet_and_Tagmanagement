# Unraid

Downtify runs on Unraid as a Docker container. A ready-made template ships in the
repository at [`unraid/downtify.xml`](https://github.com/henriquesebastiao/downtify/blob/main/unraid/downtify.xml).

> **This fork:** the Usenet audio source is only in this repository, not in the
> official `downtify/downtify` image. Build the image yourself and point the
> template's **Repository** at it, e.g. on your Unraid box:
>
> ```sh
> git clone https://github.com/betterix/downtify /mnt/user/appdata/downtify-src
> cd /mnt/user/appdata/downtify-src
> docker build -t downtify-usenet:latest .
> ```
>
> Then set **Repository** to `downtify-usenet:latest` in **Docker → Add
> Container** (see below).

## Installation

1. In the Unraid web UI, open **Docker → Add Container**.
2. Fill in the fields from the template, or copy `unraid/downtify.xml` into
   `/boot/config/plugins/dockerMan/templates-user/` on your flash drive — it then
   shows up as a template preset.
3. Map at least:
   - **WebUI Port** — any free host port → container `8000`
   - **Downloads** — e.g. `/mnt/user/downloads/music/downtify` → `/downloads`
   - **App Data** — e.g. `/mnt/user/appdata/downtify` → `/data` (settings, databases,
     an uploaded `cookies.txt`; survives image updates)
4. `UID=99` / `GID=100` / `UMASK=022` are the Unraid defaults and work out of the box.

## Soulseek (slskd)

Mount your slskd download folder at `/slskd`, then enable and point the slskd
integration at it in **Settings → slskd**.

## Usenet (Newznab indexer + SABnzbd)

Downtify can search one or more Newznab indexers (Prowlarr, NZBHydra2, NZBGeek,
DrunkenSlug, …) and hand the resulting NZB to SABnzbd:

1. Mount SABnzbd's **Completed Downloads** folder into the Downtify container,
   e.g. `/mnt/user/downloads/complete` → `/usenet`. Use the same path SABnzbd
   itself reports, or set `complete_dir` in Downtify's settings to the mount path.
2. In **Settings → Usenet**, add your indexer(s) (name, base URL, API key) and the
   SABnzbd connection (URL, API key, category `music`). Prowlarr works as the
   Newznab source too: base URL `http://<host>:9696` (its Newznab endpoint
   `/api/v1/search` is used automatically) plus the Prowlarr API key.
3. Use the built-in connection test to verify SABnzbd and every indexer.
4. Put `usenet` at the top of the source order if you want Usenet tried first.

After a successful download, Downtify copies the file into your library, tags it,
and — unless disabled — removes the job and its files from SABnzbd's history.

Album releases are downloaded **once**. The album folder is kept so every
other track of the same release is matched from it and marked successful
without another NZB; this is forced for album, artist-discography and repeated
album tracks in a playlist, and can be enabled for everything else with
**Prefer album releases**. The cache uses the album artist, so a featured track
does not trigger a second copy. Once a batch or artist sweep has copied all of
its tracks, the SABnzbd release folder is deleted automatically.

Album batches are filed under `Artist/Album` when neither the output template
nor the organize-by-artist/album switches already define a folder layout.

**Settings → Usenet → Clean up SABnzbd now** deletes every finished SABnzbd job
plus any remaining cached release folders immediately, including folders whose
history entry is already gone.

## YouTube bot check ("Sign in to confirm you're not a bot")

YouTube aggressively gates datacenter IPs. If downloads or metadata lookups fail:

- **Best fix — automatic PO Tokens via a sidecar container.** Add
  [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  from the Community Apps (or
  `brainicism/bgutil-ytdlp-pot-provider` on Docker Hub, port 4416) to your
  Unraid box, then set `DOWNTIFY_YT_POT_PROVIDER=http://<unraid-ip>:4416`
  on the Downtify container. PO Tokens are generated on demand and the bot
  check goes away — no cookies.txt needed. The plugin is already built into
  this fork's image.
- **When YouTube still answers `LOGIN_REQUIRED` on every player client**
  (visible with `DOWNTIFY_YT_VERBOSE=1` in the log), only a signed-in
  session helps. The **cookies-keeper** sidecar automates that: a
  Chromium with a persistent profile whose YouTube session is exported
  as a `cookies.txt` every 10 minutes — no manual re-uploads. Build it
  from the repo (`docker/cookies-keeper/`), log in once, done:

  ```sh
  cd /mnt/user/appdata/downtify-src/docker/cookies-keeper
  docker build -t cookies-keeper:latest .
  # Docker → Add Container:
  #   Repository: cookies-keeper:latest, WebUI port → 3000
  #   Path: /mnt/user/appdata/cookies-keeper/config → /config
  #   Path: /mnt/user/appdata/downtify → /out   (the Downtify /data volume)
  ```

  Open the container's web UI, log into youtube.com once — from then on
  `/out/cookies.txt` refreshes itself. Point Downtify at it:
  `DOWNTIFY_COOKIES_FILE=/data/cookies.txt` (Downtify re-reads the file
  on every download; the keeper only overwrites it with an
  authenticated jar, so a logged-out browser never clobbers it).
- Upload a fresh `cookies.txt` in **Settings → YouTube cookies** (exported while
  logged into YouTube), or mount one and set `DOWNTIFY_COOKIES_FILE=/data/cookies.txt`.
  Downtify re-reads the file on every download, so updating it needs no restart.
- Optionally set `DOWNTIFY_YT_PO_TOKEN` (format `client.context+token`, e.g.
  `web.gvs+ABC123`) — see the yt-dlp wiki on PO Tokens.
- `DOWNTIFY_YT_PLAYER_CLIENTS` overrides the yt-dlp player client order if the
  defaults stop working.
