# Artist release artwork

Artist cards now use release artwork by default. Remove `--artist-images` from
older bridge commands and restart the bridge. Refresh the client artist list to
receive the new `artist-release:` cover IDs; existing client disk caches may need
refreshing separately. No new credentials or provider setup are required.

The portrait experiment and its provider/configuration code have been removed.
Old experiment cache files are left untouched and are no longer read.

## Selection and requests

Choose the newest dated release with an allowed artwork URL among remembered
albums matching the exact Apple artist ID and bridge storefront. Future and
malformed dates are excluded; undated releases are a last resort. Equal dates
have a stable ID tie-break. Compilation rows belonging to another artist do not
qualify. Browsing additional releases can update the selection.

This is the **newest known available release**, not a complete-discography claim.
Public lookup returns at most 200 albums, and release dates may describe reissues.
[Apple documents artist-to-album lookup](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/LookupExamples.html);
it does not guarantee a complete newest-first album response.

Cached metadata and image bytes are reused immediately. A cover request without
matching metadata schedules a deduplicated background lookup and returns HTTP 503
with `Cache-Control: no-store`, `Retry-After`, and a sanitized `X-Artwork-Reason`.
A cached unavailable result returns HTTP 404/no-store with its remaining suppression
interval, rather than inviting immediate retries of a known suppressed lookup. Search,
`getArtists`, and artwork-info requests do not schedule enrichment themselves.
One worker, at most four pending jobs and eight admissions per minute prevent
large artist lists from causing hundreds of upstream requests. Catalog calls share
the interactive three-second rate limiter and honor HTTP 429 deferrals. Background
work waits for a one-second interactive quiet period, with a bounded 30-second
scheduling budget. Each upstream call retains its existing network timeout.

Successful empty lookups are suppressed for one hour; failures for five minutes.
These checks persist across restarts, but newly browsed album metadata takes
precedence. Some uncached cards will remain empty until retried/refreshed or their
artist page is browsed. Clients should retry pending/busy failures sparingly and
never cache a transient placeholder as a successful image. Ready responses have
a one-hour client cache lifetime; refreshing may be needed to see a new release.

## Image handling and source information

The existing album artwork pipeline validates HTTPS hosts and redirects, bounds
bytes/dimensions, decodes and emits PNG, and caches results. Its best-effort 400px
URL substitution retains fallback to the original returned URL. No separate image
cache or image provider is introduced. XML error responses and a single effective
Cache-Control header remain compatible with the supported client behavior.

`getArtistArtworkInfo?id=<typed artist ID>` returns cache-only metadata with
`imageKind: release-artwork`, `status: ready`, `albumId`, `albumTitle`,
`releaseDate`, validated Apple `sourceUrl`, and
`selection: newest-known-available-release`. Pending/unavailable responses omit
album details. Clients should identify this as album artwork and can show the
album/source destination; it is not a portrait or independently licensed imagery.
Existing Apple promotional-artwork conditions still apply; this change makes no
claim of legal clearance for arbitrary client presentation.

## Validation

Synthetic authenticated HTTP tests cover search/index IDs, pending responses,
concurrent search, bounded bursts, exact identity/date selection, shared album
image-cache reuse, restart persistence, rate-limit sharing and shutdown. Existing
album image safety tests remain. No real account or media download was used;
live catalog completeness and the user's client rendering still need retesting.

## Artist-grid versus album-page contention

Artist cover downloads have one separate worker and at most two pending image
jobs. Direct album/song artwork retains its two workers; total image jobs remain
bounded at eight and image cache/singleflight keys are shared. Saturated image
queues request a one-second delayed retry rather than the generic 30-second wait. Cached images do
not consume either network pool. This prevents unrelated slow artist images from
occupying both album-page workers. Requests for the same image still share work.

Cold metadata admission remains four jobs/eight starts per minute with the shared
catalog limiter: this is not a claim that a large cold artist grid fills instantly.
`release-pending` uses a short Retry-After, while `release-busy` reflects the
admission window. Clients must release image concurrency slots between delayed
retries and cancel stale-view work. Retrying 503 four times over seven seconds
while holding all five client image slots can block album requests before they
even reach the bridge. A bridge worker fix cannot repair that client queue.

A local comparison used actual main and PR 76647c7 snapshots, identical cached
metadata, five concurrent artist-cover HTTP requests with deliberately blocked
image delivery, followed by a direct album-cover request. Main returned in 6ms;
76647c7 remained blocked beyond 500ms; the isolated-worker change returned in 3ms.
These are controlled synthetic observations, not live-provider or Supersonic UI
benchmarks. Regression tests exercise shared versus isolated worker scheduling,
valid decoded album output, bounded pending jobs, and cached-miss HTTP policy.
