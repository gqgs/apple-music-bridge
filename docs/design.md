# MVP design and validation

The Qt main thread owns the queue, SQLite and UI. A single download worker runs an isolated CLI process group with a 15-minute deadline. A separate serial metadata worker makes bounded, timed requests; generation IDs discard stale results. The downloader runs in an app-owned 0700 staging directory, with fixed output names and a 0600 temporary config. Arguments are arrays, stdin is closed, stderr is discarded and stdout is consumed with a bounded line parser. No raw logs, title fields from CLI JSON or exception strings from external tools reach the UI.

Only a single-track final JSON array is accepted. This is deliberate: album URLs resolve to individual catalog tracks, so each job can be cancelled, verified and cached independently. The app normalizes direct Apple Music song URLs and album `?i=` links. A successful exit without an existing, nonempty, private media file is a failure. ffprobe validates the actual audio stream. E-AC-3 alone is not evidence of JOC; the stream profile must report it. User tags, filenames and catalog marketing are not trusted for format labels. ffprobe versions that cannot expose JOC conservatively show Atmos unverified.

Cache filenames are SHA-256 of storefront/song/requested quality. Indexed files move atomically on the same filesystem; crashes between rename and SQLite commit leave an orphan cleaned on next startup. Symlinks and path escapes are rejected; eviction only considers generated names within the dedicated media directory. SQLite rows for missing/truncated files are removed. A file lock prevents two instances from racing. Current/queued entries are protected, including the one prefetched successor. A failed cache insertion never begins playback. Files are not a defense boundary against a malicious process running as the same OS user; external binaries must be trusted. No executable, DRM or wrapper code is bundled.

mpv starts paused using `--no-config`, a private Unix JSON IPC socket, and the selected device. The adapter observes input and output audio parameters before unpausing. Output observations are revalidated throughout playback. Missing parameters revoke verification and pause playback; restored parameters re-enable controls without resuming automatically or overriding a user pause. Dolby requests set `--audio-spdif=eac3` (or `ac3` for a detected fallback). PCM fallback aborts playback; the UI does not equate a requested passthrough option with actual output. EOF advances the queue, errors leave an actionable Retry state, and subprocess shutdown is scoped to the created group. No software volume override is applied.

The cache size is a completed-media target, not a filesystem quota. One 2 GiB staging allowance plus a free-space guard limits ordinary temporary growth, with polling overshoot possible. ffprobe runs with a 30-second timeout; cancellation during probing takes at most that long. Shutdown waits for the owned download to stop. Catalog requests have a 15-second timeout; closing during one may wait for that worker to finish. The queue is deliberately not persisted and pinning is deferred.

## Validation

Run the commands at the start of the README. Tests generate one-second ALAC, AAC, AC-3 and E-AC-3 tones using ffmpeg. They cover LRU and protected entries, codec variants, restart reconciliation, cache locking, oversized entries, ENOSPC insertion, insufficient free space, path traversal/symlinks, malformed and zero-exit CLI results, raw secret suppression, process-group cancellation, cancel/retry, duplicate queueing, one-track prefetch, mpv IPC pause/seek, PCM fallback rejection, runtime passthrough loss/recovery and pause preservation, verified-profile classification, stale catalog responses and Qt's offscreen download-to-play flow.

`docs/ui-smoke.png` is an offscreen Qt capture of the synthetic test fixture, not a live Apple download. Regenerate with:

```sh
PLAYER_TEST_SCREENSHOT="$PWD/docs/ui-smoke.png" QT_QPA_PLATFORM=offscreen pytest -q tests/test_ui.py
```

No live catalog request was made in the implementation environment: its destination allowlist excludes itunes.apple.com. No real downloader/wrapper execution, account login or proprietary binary loading was performed. Real mpv/hardware was unavailable; the IPC contract is exercised against a fake executable. Passing these checks does not validate receiver output or the entire pinned downloader on your machine.

## Small local acceptance check

After managed Backend setup, play a known song in Lossless mode. Verify ALAC, bit depth/sample rate, pause/seek and a second queued track. Then choose your known-good HDMI device, queue an Atmos version and verify the soundbar's own Atmos indication. Confirm the UI reports the actual codec/JOC status and that unplugging HDMI produces an error rather than claiming successful passthrough. Try a public catalog search and album browse in your storefront. Cancel a download and Retry, then relaunch and replay a cached track.

## Interface references

- [Pinned downloader CLI/config](https://github.com/zhaarey/apple-music-downloader/tree/452ba0b672d49afffca3f57b0c2c4670a316e2ca): `main.go`, `config.yaml.example`.
- [Apple public Search/Lookup API](https://performance-partners.apple.com/search-api).
- [mpv manual](https://mpv.io/manual/stable/): JSON IPC, audio-spdif, audio-params and audio-out-params. Forcing spdif with `--ad` is not supported; the app uses audio-spdif and observes the resulting format.
