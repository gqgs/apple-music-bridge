# Apple Music Player

<img src="src/apple_music_player/assets/logo.svg" width="64" height="64" alt="Player logo: white play triangle with mint spatial sound arcs on navy" />

A small native Linux player: search songs, browse albums, queue lossless/Dolby downloads, and play through mpv. Python 3.11+, Qt Widgets, SQLite. No embedded browser.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
apple-music-player
# Tests use generated audio and fake external tools; no account required:
QT_QPA_PLATFORM=offscreen pytest -q
ruff check src tests
ruff format --check src tests
```

**One-window backend setup:** open **Backend setup** for the **Managed rootless lite** flow: pinned runtime installation, local sign-in/2FA, start/status and shutdown. It requires Linux x86_64 with permitted user namespaces/mounts. Installation and first sign-in require explicit user actions. Once configured, the managed backend starts in the background with the app; its status is shown in the main window. Stop is respected for the current session. No sudo or automatic OS changes. The wrapper ZIP is an upstream GitHub Actions artifact (browser sign-in may be needed), verified by SHA-256 before installation. See [the three setup steps and exact pins](docs/rootless-setup.md). The user has validated managed lossless and Atmos on Arch; automated tests use synthetic processes.

**Requirements:** `mpv`, `ffprobe` (normally packaged with ffmpeg), and Qt Wayland libraries. Tests also use `ffmpeg` to generate synthetic audio. Backend setup lists missing tools; it does not install OS packages. Managed amdl v0.1.4 handles normal audio finalization natively; GPAC/MP4Box is not required.

1. Open **Settings**, select your cache size. For Dolby, set your HDMI device from `mpv --audio-device=help`. The validated machine used `alsa/hdmi:CARD=HDMI,DEV=3`; your device may differ.
2. Search defaults to **Albums** with **BR** for new configurations. Choose **Songs** or **Albums**, enter a query and your two-letter storefront, then Search. For example, search Albums for `daughter if you leave`, select **If You Leave — Daughter** if available, and **Open album** (or double-click) to see its tracks. **Play album (replace queue)** starts from the first track and replaces current playback/queue; **Queue album** appends those songs. Song results use **Queue selected**; **Browse album** opens the selected song's album. An album URL also opens its track list. Availability and ranking depend on the public catalog/storefront.
3. Choose **Prefer Lossless** or **Prefer Dolby Atmos** before queueing. Playback starts after a full download; only the next track is prefetched. Use pause, seek, Next, Remove, Cancel download and Retry. Device changes apply to the next playback; Retry restarts failed playback.

**Media format is measured, not assumed.** Now playing shows Lossless (ALAC/FLAC), Lossy (AAC/AC-3), Dolby Digital Plus / Atmos unverified, or Dolby Atmos when ffprobe reports a JOC profile. Codec, sample rate, channels and available bit depth are shown. Requested-quality fallbacks are conspicuous. The separate output label reports mpv's PCM or compressed passthrough state, never receiver-confirmed Atmos. Dolby playback requires an explicit device and stops if mpv reports PCM fallback. Confirm Atmos on your soundbar. No app volume control is provided.

**Browsing:** the documented [public iTunes Search/Lookup API](https://performance-partners.apple.com/search-api) supplies metadata without account credentials or a developer token. Its catalog is not identical to the subscription Apple Music catalog; availability can differ, and some album IDs may not resolve. Searches are explicit and cached in memory. Rapid navigation waits automatically for the three-second request spacing while keeping current results visible. Only the newest request is applied. HTTP 429 responses honor Retry-After with at most two automatic retries and at most 60 seconds per automatic wait; longer waits or persistent failures stop with a message. No audio previews are fetched. Album artwork and local Home shelves are described in [browsing](docs/browsing.md). Direct song URLs remain usable when browsing fails. The official Apple Music catalog API would separately require a developer token; wrapper login does not supply one.

The **Storefront** is saved between launches. Opening a valid Apple Music URL adopts its country (for example, `/br/` selects Brazil) and clears results from the previous storefront. Search again to browse that country. Queue rows show their original storefront; existing queued tracks are not rewritten. Remove old-country entries and queue fresh results when switching stores.

The bold storefront notice compares browsing with the managed backend's reported region. If it shows **Browsing US · Managed backend reports BR**, click **Use BR storefront**, search again, and replace any old US queue entries. Mismatched entries are blocked before downloading. New configurations default to BR as requested; explicit saved storefronts (including US) are preserved. The backend comparison still warns about mismatches. Region reporting does not prove subscription availability for every song.

**Cache:** `$XDG_CACHE_HOME/apple-music-player` (default `~/.cache/apple-music-player`) is private and separate from existing downloads. The default completed-media limit is 10 GiB. Eviction protects queued/current tracks, distinguishes requested quality variants, and uses playback recency. Remove queue entries or raise the limit if protected files fill it. Staging adds up to 2 GiB per job, checked every 250 ms and at completion, with a 128 MiB free-space guard; a fast writer can briefly overshoot. This is **not a hard disk quota**. Incomplete jobs and unindexed media are recovered on restart. Queue contents are session-only; completed cache entries survive. One app instance per cache.

The app writes a fresh, private per-job downloader config and leaves your existing config/downloads untouched. It disables lyrics, artwork and conversion, closes stdin, and discards all raw downloader output (the pinned CLI can print its configuration). Only a validated final path is accepted. Authentication is configured through Backend setup. No account tokens are stored in ordinary app settings.

See [design and validation](docs/design.md). New browsing behavior still needs local acceptance testing; automated tests use synthetic audio and fake processes.

For a failed download, see [safe diagnostic categories](docs/download-diagnostics.md). Share only the bracketed category, quality and storefront; no raw logs or account files are needed.

The original [SVG logo](src/apple_music_player/assets/logo.svg) is bundled as the Qt application/window icon; it scales without additional dependencies. This project is not affiliated with Apple.

Home defaults to local recently played albums, with recent releases from artists played. See [browsing scope, artwork conditions and history storage](docs/browsing.md). Legacy external-backend settings now lead to managed setup; no old installations, downloads or sessions are deleted. Existing managed sessions continue unchanged. The pinned amdl engine remains an external dependency.
