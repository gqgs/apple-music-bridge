# Download diagnostics

If a download fails, report only the bracketed category returned by the bridge,
requested quality and two-letter storefront. Do not share raw downloader output,
config, HTTP bodies or account files. Retry preserves the managed login.

The adapter drains stdout and stderr separately with bounded line buffers and
retains only fixed categories and the final stdout result path. Error suffixes
(which may contain URLs, tokens or HTTP bodies) are discarded. Recoverable wrapper
warnings do not reject a successful result. Unknown failures report `no-result`
and the process exit code. Exit zero is never sufficient to accept a download.

Source review against amdl v0.1.4
[`452ba0b`](https://github.com/zhaarey/apple-music-downloader/tree/452ba0b672d49afffca3f57b0c2c4670a316e2ca):

- `internal/app/run.go` accepts `--config`, `--json`, `--atmos`, `--aac`; prints a compact
  final array with `path`, or `null` when no tracks were added. Several early
  failures return exit zero. The song preamble has no newline.
- `internal/config/config.go` loads JSON-compatible YAML using nested keys;
  the private absolute paths, naming templates and disabled artwork/lyrics/
  conversion match `config.yaml.example`. The job working directory is private.
- `internal/app/url.go` accepts `/country/song/numeric-id`. Public iTunes IDs
  can still be unavailable in the subscription catalog or another storefront.
- `internal/amp-api/token.go` obtains catalog access separately from wrapper
  login. A catalog failure does not prove the local account session is invalid.
- `internal/wrapper` uses `/status`, `/m3u8?adamId=…` and `/key` JSON envelopes
  matching wrapper [`56a7dcd`](https://github.com/WorldObservationLog/wrapper/blob/56a7dcd726f2c2ef1d4420d6747ba0bafe215fee/lite/lite_main.cpp).
  A successful status probe does not prove a song can download.
- Pinned amdl audio finalization uses native Go defragmentation, tagging and ALAC
  repair; MP4Box is not required for the supported normal audio path. The bridge requires
  ffprobe for actual format verification; playback is handled by the client.

Tests use source-shaped failure transcripts, both output streams, oversized and
fragmented lines, zero/nonzero exits, nested album output paths and synthetic audio.
No proprietary runtime or account is executed. Fixed diagnostic categories separate
wrapper endpoint/application-code failures from HTTP status, refusal, timeout,
invalid response and empty result. Only allowlisted endpoints/codes/messages escape.

The bridge also distinguishes the two audio routes from the pinned caller's fixed
error prefixes: `download-fairplay` and `download-aac` identify the route but leave
the lower-level cause unknown. Lossless mode can reach amdl's internal AAC route,
so the requested preference does not establish which route failed.

More specific labels are emitted only for anchored, source-verified wrappers:

- `fairplay-playlist`, `fairplay-container`, `fairplay-read`, `fairplay-decrypt`
  and `fairplay-write` follow `internal/fairplay-rip/runv4.go` messages.
- `aac-playlist-fetch`, `aac-playlist-parse`, `aac-license`, `aac-media`,
  `aac-container`, `aac-decrypt` and `aac-write` follow the song path through
  `internal/widevine-rip/{runv5/runv5,playlist,rip,download,decrypt}.go`.
- Explicit local `/key`, `/webplayback` and `/license` failures retain fixed
  wrapper endpoint/API-code/HTTP-status/transport categories. AAC license errors
  must have the exact `acquire content key: ` wrapper. Embedded words in a private
  suffix or upstream `msg=` field are never searched for other causes.

These categories describe where an existing operation failed, not the root cause.
FairPlay does not check CDN HTTP status before parsing, so its playlist/container
failures cannot establish a CDN 404. The pinned wrapper's `/webplayback` maps multiple
failures to application code 404, including network failures, malformed or unexpected
responses, account rejection and missing playback data. This code remains a hard
failure, never a format-fallback cue. Unknown suffixes are discarded; neither these
diagnostics nor retries change account/session state.

Per-track fallback recognizes only the exact `Unavailable` result,
`Failed to dl aac-lc via lite-server: Unavailable`, and
`Failed to extract info from manifest: no codec found` (with the upstream warning
prefix). These must have a normal zero exit and no other errors or wrapper warnings.
The latter means a master playlist was read successfully but lacked a matching
codec. Other manifest failures are not evidence of format availability. Each
attempt uses fresh staging; Atmos → lossless → AAC shares one 900-second download
budget and cancellation event. Lossless → AAC uses the same rules. A returned
ordinary AC-3 file is discarded and retried as lossless, not mislabeled Atmos.
Successful upstream AAC fallback is accepted with its actual probed metadata.

`wrapper-m3u8-api-404` means the reached service could not provide a song manifest;
it does not prove expired login. Use the matching storefront via `serve --country`.
The bridge preserves typed country/song IDs and checks known backend regions before
download. Catalog coverage is not guaranteed to match the subscription catalog.

The rootless launcher shares host networking. Both bridge and downloader use the
loopback lite endpoint, while the downloader has a 900-second job timeout and
readiness probes use 0.5 seconds. The CLI status distinguishes a saved session and
reachable service from verified media access. Never reauthenticate solely because
a public catalog song is unavailable.

## Catalog observations (Brazil, 2026-11-05)

Public iTunes metadata and Apple Music release grouping provide different catalog
views. These are dated observations, not permanent availability claims:

- A [BR song lookup](https://itunes.apple.com/lookup?id=394277479,95787592,1719122348,1720511624&country=br&entity=song&limit=200)
  returned explicit `isStreamable=false` for all 10 returned songs on *Stone Flower*
  (`394277479`) and all 17 returned songs on the older *The Rising Tied (Deluxe Edition)*
  (`95787592`). The two newer deluxe editions (`1719122348`, `1720511624`) each
  returned 21 songs with `isStreamable=true`.
- A [second BR lookup](https://itunes.apple.com/lookup?id=95780485,1345062256&country=br&entity=song&limit=200)
  returned 16 songs with `isStreamable=true` for each of the other two *The Rising
  Tied* editions (`95780485`, `1345062256`).
- The [current deluxe album page](https://music.apple.com/br/album/the-rising-tied-deluxe-edition/1719122348)
  grouped `1720511624`, `95780485` and `1345062256` under **Other Versions**.
  Release grouping and playback availability are separate observations.

Apple's public [Search API documentation](https://performance-partners.apple.com/search-api)
does not document an `isStreamable` guarantee. An explicit false is a diagnostic
clue; a missing field is unknown, and true does not prove playback for an account
or requested format. These observations do not establish a licensing cause.
`wrapper-webplayback-api-404` still cannot distinguish transport, response,
account-session or missing-asset failures. No availability filter or fallback
policy is inferred from these fields.
