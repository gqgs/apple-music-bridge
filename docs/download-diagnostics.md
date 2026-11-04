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

- `internal/app/run.go` accepts `--config`, `--json`, `--atmos`; prints a compact
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

`wrapper-m3u8-api-404` means the reached service could not provide a song manifest;
it does not prove expired login. Use the matching storefront via `serve --country`.
The bridge preserves typed country/song IDs and checks known backend regions before
download. Catalog coverage is not guaranteed to match the subscription catalog.

The rootless launcher shares host networking. Both bridge and downloader use the
loopback lite endpoint, while the downloader has a 900-second job timeout and
readiness probes use 0.5 seconds. The CLI status distinguishes a saved session and
reachable service from verified media access. Never reauthenticate solely because
a public catalog song is unavailable.
