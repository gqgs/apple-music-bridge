# Apple Music Player

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

**External requirements:** `mpv`, `ffmpeg`/`ffprobe`, `MP4Box` (GPAC), and an already-built [zhaarey/apple-music-downloader](https://github.com/zhaarey/apple-music-downloader/tree/12fff0caead84c264abdb02878d43b1773e13368) executable at commit **12fff0caead84c264abdb02878d43b1773e13368**. All must be on PATH except the downloader, whose absolute executable path is set in **Settings**. Run the compiled downloader binary, not `go run`. On Arch, mpv, ffmpeg, gpac and the normal Qt Wayland runtime libraries are needed; this app installs none of them automatically.

Start your existing, authenticated [zhaarey/wrapper](https://github.com/zhaarey/wrapper/tree/4459548fb3c0f96dcb974755eb4a5a0791fbbc74) separately, pinned to **4459548fb3c0f96dcb974755eb4a5a0791fbbc74**, with classic local ports **10020** (decryption) and **20020** (manifest). This does not target wrapper-lite. The app never starts the wrapper, requests passwords or runs sudo. On the validated Arch setup, the wrapper needed a manually managed PID namespace; keep using your already-working wrapper startup procedure.

1. Open **Settings**, select your downloader executable and cache size. For Dolby, set your HDMI device from `mpv --audio-device=help`. The validated machine used `alsa/hdmi:CARD=HDMI,DEV=3`; your device may differ.
2. Search with your two-letter storefront, select a song, and **Queue selected**. **Browse album** shows its tracks. A song URL queues directly; an album URL opens its track list, then **Queue all results** adds the album. Double-clicking a result also queues it.
3. Choose **Prefer Lossless** or **Prefer Dolby Atmos** before queueing. Playback starts after a full download; only the next track is prefetched. Use pause, seek, Next, Remove, Cancel download and Retry. Device changes apply to the next playback; Retry restarts failed playback.

**Media format is measured, not assumed.** Now playing shows Lossless (ALAC/FLAC), Lossy (AAC/AC-3), Dolby Digital Plus / Atmos unverified, or Dolby Atmos when ffprobe reports a JOC profile. Codec, sample rate, channels and available bit depth are shown. Requested-quality fallbacks are conspicuous. The separate output label reports mpv's PCM or compressed passthrough state, never receiver-confirmed Atmos. Dolby playback requires an explicit device and stops if mpv reports PCM fallback. Confirm Atmos on your soundbar. No app volume control is provided.

**Browsing:** the documented [public iTunes Search/Lookup API](https://performance-partners.apple.com/search-api) supplies metadata without account credentials or a developer token. Its catalog is not identical to the subscription Apple Music catalog; availability can differ, and some album IDs may not resolve. Searches are explicit, rate limited and cached in memory. No previews or artwork are fetched. Direct song URLs remain usable when browsing fails. The official Apple Music catalog API would separately require a developer token; wrapper login does not supply one.

**Cache:** `$XDG_CACHE_HOME/apple-music-player` (default `~/.cache/apple-music-player`) is private and separate from existing downloads. The default completed-media limit is 10 GiB. Eviction protects queued/current tracks, distinguishes requested quality variants, and uses playback recency. Remove queue entries or raise the limit if protected files fill it. Staging adds up to 2 GiB per job, checked every 250 ms and at completion, with a 128 MiB free-space guard; a fast writer can briefly overshoot. This is **not a hard disk quota**. Incomplete jobs and unindexed media are recovered on restart. Queue contents are session-only; completed cache entries survive. One app instance per cache.

The app writes a fresh, private per-job downloader config and leaves your existing config/downloads untouched. It disables lyrics, artwork and conversion, closes stdin, and discards all raw downloader output (the pinned CLI can print its configuration). Only a validated final path is accepted. The existing downloader handles authentication; no account tokens are stored in app settings.

See [design and validation](docs/design.md). Real Apple downloads, live catalog requests and HDMI output still require local acceptance testing; automated tests use synthetic audio and fake processes.
