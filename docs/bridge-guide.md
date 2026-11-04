# Bridge setup and operations guide

A local, read-only OpenSubsonic bridge for Apple Music. Use a client such as
[Supersonic](https://github.com/gqgs/supersonic) to browse, search and play full
lossless or spatial-audio files. The bridge handles catalog metadata, downloads,
artwork and a bounded media cache. It does not transcode audio or confirm your
receiver's output mode; Atmos passthrough depends on the client and hardware.

## Install and run

Requires Python 3.11+, **ffprobe** (usually provided by ffmpeg), and Linux x86_64
with permitted unprivileged user namespaces/mounts for the managed runtime.
No Qt, display server or mpv installation is required by the bridge.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
apple-music-bridge --help
```

For a **new installation**, obtain the pinned `wrapper-lite-linux-x86_64` ZIP
from the [upstream artifact run](https://github.com/WorldObservationLog/wrapper/actions/runs/41407950505).
GitHub may require browser sign-in. Review the third-party runtime origin before
installing; this project does not bundle its proprietary assets. The recorded
artifact expiration is January 31, 2027. If unavailable, setup is blocked until a
new verified pin is supplied; do not substitute a `latest` or classic wrapper ZIP.

```sh
apple-music-bridge setup --archive /path/to/wrapper-lite-linux-x86_64.zip --accept-runtime
apple-music-bridge login
apple-music-bridge status
apple-music-bridge serve
```

Setup validates the ZIP checksum, installs the rootless launcher and downloads
pinned amdl. It does not execute the runtime or sign in. Login prompts locally for
your Apple account, hidden password and hidden verification code when requested.
Never pass Apple credentials as command arguments or put them in configuration.
Ctrl-C cancels login and restores the previous session. Stop `serve` before login.
No command uses sudo or changes host security policy.

`serve` starts the previously installed, signed-in runtime and binds the bridge
to **127.0.0.1:4533**. Configure your client using the username and key in
`$XDG_CONFIG_HOME/apple-music-bridge/auth.json` (default
`~/.config/apple-music-bridge/auth.json`); the key is the client password/API key.
Read that private file locally; do not paste it into logs or chat. These credentials
are separate from your Apple account. Client access is authenticated; remote
binding and browser access are not supported.

Useful options:

```sh
apple-music-bridge serve --country br --quality lossless --cache-gib 10
apple-music-bridge serve --country br --quality atmos --diagnostics
```

Choose the storefront matching your Apple subscription. Defaults are `br`,
lossless, 10 GiB and port 4533. Flags are not silently persisted. Diagnostics
contain aggregate timings/outcomes, not queries, credentials or raw upstream logs.
The runtime's local API uses port 12340; the bridge never kills an unrelated owner.

`--quality` is a **preference**, not a format lock. `atmos` tries Atmos, then
lossless, then AAC for each track; `lossless` tries lossless, then AAC. Keep the
bridge running in Atmos mode for mixed playlists. Only explicit unavailable-format
results trigger another attempt. Network, authentication, malformed-manifest,
download, disk and cancellation errors stop the request instead of silently
downgrading. Ordinary AC-3 returned by the upstream Atmos selector is retried as
lossless, because Dolby Digital is not Atmos. No transcoding is performed.

## Existing installations

Do **not** repeat setup or login if your current session works. Installed runtimes
under `$XDG_DATA_HOME/apple-music-player` are reused in place, including the existing
`.player-login-ok` receipt and tokens. Fresh installations use
`$XDG_DATA_HOME/apple-music-bridge`. If both contain installations, explicitly
select `--runtime-root /existing/path` on each command. No session is moved/copied.

Keep your existing bridge credential file and cache arguments:

```sh
apple-music-bridge serve --auth-file /existing/private/auth.json --runtime-root /existing/runtime
```

The cache remains `$XDG_CACHE_HOME/apple-music-bridge` (default
`~/.cache/apple-music-bridge`). Supply `--cache /existing/path` if customized.
Desktop settings/downloads/history are left untouched and are not imported as
listening history. Select any previous nondefault storefront/quality explicitly.
The old `python -m apple_music_player.bridge ...` invocation and flags remain
supported. The internal package and `runtime_child` module retain their old names
for compatibility; the desktop `apple-music-player` command is retired. A fresh
virtual environment removes old PySide6 dependencies and stale entrypoints.
The GitHub repository has **not** been renamed yet.

## Behavior and limits

- Public iTunes Search/Lookup metadata needs no developer token, but may omit
  releases or rank unexpectedly. Artist-first client search avoids extra category
  requests. Shared catalog spacing is at least three seconds; cold artist images
  can load slowly. [Protocol details](opensubsonic-poc.md).
- Some streaming-only albums (including DJ mixes) have iTunes album metadata but
  no iTunes song rows. For these albums, the bridge falls back to the exact public
  Apple Music album page, with no account credentials or audio/preview requests.
  It requires a complete, matching track list, preserves individual song credits
  and the album artist, and uses the catalog's existing request spacing, one-hour
  cache and retry limits. Failed or changed pages report a catalog error rather
  than a successful empty album. This fallback is limited to 200 tracks and
  does not establish playback availability for an account or selected quality.
- Artist images are available release covers, not portraits. Artwork requests are
  bounded, HTTPS/host/redirect validated and decoded as PNG/JPEG/WebP within 2 MiB
  and 2048×2048 input limits, then served as PNG. The 400px CDN substitution is
  best-effort and falls back to the returned original URL. [Artwork details](artist-artwork.md).
- Album quality badges are optional catalog availability metadata, fetched only
  when a compatible client requests an album badge. The bridge reads the public
  Apple Music album page without account credentials, cookies, previews, or audio
  downloads. It validates the exact storefront/album and fails closed if the page
  changes or is unavailable. Results are cached for one day (failed lookups five
  minutes), capped at 256 albums, with at most two concurrent requests. Apple sees
  the public album URL and the bridge machine's network address. The extra page
  request does not block catalog browsing or streaming. Badges describe advertised
  availability, not the selected download, every track, or the device's output.
  [Optional extension](album-quality.md).
- Full tracks download before streaming. Media is probed, preference profiles remain
  separate, concurrent requests share work, and streaming leases protect files
  from eviction. Cached fallback files are reused for that preference until normal
  eviction; fallback does not change the next track's preference. Requested Atmos
  is not proof of Atmos/JOC; actual metadata is retained. Stream headers report
  `X-Media-Codec`, `X-Media-Atmos`, `X-Requested-Quality` and `X-Quality-Fallback`.
  An unverified E-AC-3 stream is not labeled JOC-verified. Clients need appropriate
  passthrough support for spatial output and PCM output for stereo tracks.
- Completed media uses LRU eviction. Staging has a 2 GiB per-job guard and a
  128 MiB free-space reserve; fast writers can overshoot between checks. This is
  not a hard total-disk quota. User downloads outside this cache are never deleted.
- Runtime/session directories are private; stored tokens are **not encrypted**.
  Login uses sealed memfd arguments and a private 2FA FIFO. No raw subprocess
  output is logged. [Runtime/security details](rootless-setup.md).
- No library edits, cloud history, personalized recommendations, lyrics provider
  or new DRM implementation. Pinned external amdl handles download/decryption.

## Test

```sh
pytest -q tests experiments/release_network
ruff check src tests experiments
ruff format --check src tests experiments
```

Tests require ffmpeg for synthetic media and intentionally require PySide6 to be
absent. They use fake runtime processes and catalog/image fixtures—no account,
Apple downloads or proprietary runtime execution. Real sign-in and client/hardware
lossless/Atmos acceptance still require local user testing.
