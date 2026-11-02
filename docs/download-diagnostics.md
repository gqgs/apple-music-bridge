# Download diagnostics

If a download fails, report only the bracketed category shown in the player,
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
- Lite finalization uses its Go media implementation; MP4Box is needed by the
  classic path, not evidence of this lite failure. The player still requires
  ffprobe for actual format verification and mpv for playback.

Tests use source-shaped failure transcripts, both output streams, oversized and
fragmented lines, zero/nonzero exits, nested album output paths and synthetic
audio. No proprietary runtime or account is executed. The reported real-user
download failure is not yet reproduced: its new category is needed before
claiming a root cause or changing authentication.

The original `[wrapper]` category combined status/manifest failures. It now
reports the endpoint and separates `api` (application envelope code), `http`
(HTTP status), `refused`, `timeout`, `invalid-response`, `empty`, and `unknown`.
Only allowlisted endpoints, codes and fixed messages are retained. In particular,
`wrapper-m3u8-api-404` means the service responded but could not provide a
manifest; it does **not** mean connection refusal or prove expired credentials.
This matches the pinned wrapper's `handle_m3u8` and amdl's
`internal/wrapper/wrapper_test.go` application-error test. `/status` uses only
the saved storefront and can succeed while `/m3u8` fails. The setup screen now
says “Service reachable · local session saved; media access not yet verified”.

Lifecycle review and a fake-process Qt regression verify that closing the setup
dialog leaves the service and its app-owned health timer running. The rootless
launcher uses user/mount/PID namespaces, not a network namespace; both clients
target `http://127.0.0.1:12340`. amdl's default HTTP client has no total request
timeout; the player still enforces its 900-second job timeout, independently of
its 0.5-second status probe. None of these checks exercises an actual account.

A direct BR song URL succeeding while browse results fail exposed a UI mismatch:
search previously reset to `us`, and pasting a BR URL left the old US results in
place. The player now saves the storefront and adopts it from validated URLs,
invalidating prior results/in-flight lookups. Album lookup already propagates the
selected result's country. Song queue URLs use `trackId`; album lookup uses
`collectionId`. A regression uses the supplied `/br/song/still/593614986?` URL
with synthetic collection metadata to verify all three routes produce the same
BR song URL. Live public lookup was unavailable in the execution environment,
so this does not establish cross-catalog ID equivalence or every track's
subscription availability. If a fresh BR result still fails, report that one
song's public URL for comparison; do not reauthenticate based on this error alone.

Starting directly in search could still reuse a saved US choice without opening
that BR URL. The UI now prominently compares the selected storefront with the
backend's normalized two-letter `/status` region, with a button to adopt it.
Fresh setups now default to BR by explicit user request; saved country choices are preserved. Unknown/numeric/multiple backend
regions are not guessed. Country edits persist immediately; changing the field
invalidates searches. The controller rejects both newly added and previously
queued mismatched tracks before download, preserving their original IDs.
Qt mouse/keyboard regressions cover selecting BR, searching Albums, opening
tracks and queueing; `docs/storefront-smoke.png` uses synthetic backend metadata.
