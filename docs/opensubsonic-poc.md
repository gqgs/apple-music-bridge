# Local OpenSubsonic bridge protocol

This document describes the bridge API and the history of its compatibility fixes.
See [README](../README.md) for current installation, terminal login and migration.
The API is a read-only subset, not a complete Navidrome replacement.

## Launch and compatibility

The preferred command is `apple-music-bridge serve`, which starts an existing
installed/signed-in runtime. `setup` and `login` are separate explicit local steps.
The compatibility invocation `python -m apple_music_player.bridge --auth-file ...`
retains its original cached-file-only behavior unless `--start-runtime PATH` is
supplied. It never installs or signs in automatically.

Client username/key credentials are separate from Apple authentication. The file
is private (0600), validated and never logged. API-key or username/password
(token/salt, plain and hex protocol authentication) remain supported. Prefer form
POST to avoid credentials in client URL logs. To rotate, stop the service, choose
a new private file with legacy `--init-auth`, then configure both server and client
with that file. Existing files are never overwritten by initialization.

Cache directories must be app-owned, never arbitrary music/download folders.
Default media cache remains `$XDG_CACHE_HOME/apple-music-bridge`; old desktop files
are not moved or deleted. Runtime/cache locks reject concurrent owners.

Restart with `--quality atmos` to request that variant. Quality is a server profile,
not a standard Subsonic bitrate setting. IDs include storefront and quality to keep
client caches/variants distinct. The bridge never silently transcodes or substitutes
AAC: lossy output for lossless and non-E-AC-3 output for Atmos are rejected. E-AC-3
without verified JOC remains **Atmos unverified**, exposed through `X-Media-Atmos`;
receiver confirmation remains separate. Some clients may ignore these headers.

Ctrl-C / SIGTERM stops accepting work and cancels the bridge's downloader process
group; the original scoped cancellation mechanism does not kill unrelated services.
A disconnected cold request may finish one bounded download for another waiter/cache;
individual client disconnect does not cancel other clients' work.

## Minimal API and boundaries

JSON (`f=json`) and default XML; `/rest/name` and `/rest/name.view`; GET, HEAD and
form POST. Standard `c` and `v` parameters are required. Supported methods:

- `ping`, `getLicense`, `getMusicFolders`, `getUser`, `tokenInfo`;
- `getOpenSubsonicExtensions` (static public discovery only);
- `search3`, `getArtists`, `getArtist`, `getArtistInfo2`, `getAlbum`, `getSong`;
- `getAlbumList2` (`alphabeticalByName` and `newest`);
- empty read-only `getPlaylists`, `getGenres`, `getInternetRadioStations`;
- `getCoverArt` validated artwork or fallback PNG and `stream` original completed-file bytes.

Use **search first**: empty queries, `getArtists` and album lists show only locally
discovered metadata, not the entire Apple catalog or Apple personal library.
Artist albums reuse the existing neural grouping order, though clients may sort them
again. Artist/album counts before opening details reflect locally known children;
unknown album duration is zero. No fake personal history, editorial recommendations,
artist portraits, private library sync or Navidrome-specific `/api` compatibility.
The page is a separate server/account in a client; it is not a Navidrome plugin and
does not inject Apple releases into another Navidrome database.

Search and ID lookup use the existing public iTunes adapter and its catalog limits.
Three-second spacing / Retry-After are honored, with at most three deferred retries
and a 30-second cumulative sleep budget per operation; larger waits fail without
retrying early. A three-entity search can take multiple requests. Metadata persists
independently of media eviction, capped at 5,000 records. Typed IDs distinguish
artist/album/song and include country/profile; evicted metadata can be resolved by
numeric ID again. No title-based remapping across storefronts.

One core worker owns SQLite writes and catalog requests; a separate single worker
runs downloads and managed-runtime readiness checks. All catalog requests (including
track resolution before a download) still use the same Catalog instance/rate limiter.
At most 16 HTTP
workers/connections are active; excess connections receive HTTP 503 with Retry-After.
At most four metadata requests are admitted concurrently; excess metadata work
receives a sanitized protocol busy error instead of joining an unbounded queue.
This serializes cold
misses, deduplicates successful identical downloads, bounds queue growth and permits
concurrent warm streams. Downloads have their own four-operation admission bound;
queued duplicates recheck the media cache before downloading. Cache commit and
lease acquisition run together on the core worker. Downloads do not hold that
worker while fetching/decrypting/probing media; catalog waits can still delay a
short cache transaction or track resolution. Full files download/probe/commit before
response headers. Downloader timeout is 900 seconds across all per-track format
attempts; a client's shorter timeout may require retry after completion. The quality
profile is a preference: Atmos → lossless → AAC, or lossless → AAC. Explicit format
misses can fall back; network/authentication/download errors do not. Actual probed
codec/JOC metadata and `X-Quality-Fallback` distinguish a fallback from the requested
preference. Successful fallback files remain cached under the preference profile.
Completed media is bounded by the configured limit, with the existing separate
2 GiB staging allowance/free-space reserve. This is not a hard total-disk quota.

Streaming pins are reference-counted until each response ends; eviction cannot
remove an active file. Serving uses an open no-follow descriptor and bounded reads.
Single byte ranges, suffix/open-ended ranges, Content-Range, Content-Length,
Accept-Ranges, ETag/If-Range and 416 are implemented. Multiple ranges are rejected.
HEAD does not trigger cold media downloads. No transcoding, timeOffset seeking,
HLS or incremental download streaming. Client bitrate limits/format conversions
are rejected instead of silently losing quality. Range seeks work only after full
completion and depend on client/container behavior.

## Artwork fetching and format

Album and song covers use the public catalog artwork URL as their source and fallback.
Songs without their own URL use their locally known album; artists without a verified
portrait return the neutral placeholder (an album cover is not a portrait).
Only HTTPS `isN-ssl.mzstatic.com` URLs on port 443 are accepted, including redirects.
No URL, HTTP body or upstream diagnostic is printed. Authentication remains required.
Cover metadata uses short-lived read-only SQLite connections (100ms busy timeout),
not the catalog queue. Unknown or evicted metadata returns a placeholder without
an additional Apple lookup; opening/searching the item restores its metadata.

Two dedicated workers fetch covers outside the serialized catalog/media worker.
Duplicate in-flight URLs (including size variants) share work. Up to eight distinct
fetches can be queued/running on the two workers, so Supersonic's five concurrent
requests wait for real covers rather than receiving overload placeholders. Disk
cache hits are checked before admission or negative-cache decisions. Each caller has a shared 20-second waiting budget for the variant and fallback;
fetches use 8-second socket timeouts, a 15-second read-loop deadline and a 2 MiB
body cap. OS DNS resolution is not a hard wall-clock timeout; a stuck resolver can
occupy an artwork worker but cannot occupy the catalog worker. Shutdown cancels
queued artwork; already running workers finish subject to these limits.

The existing safe image cache validates JPEG/PNG/WebP content and dimensions
(maximum 2048 per side), then scales down to at most 1024 pixels in the bridge, without upscaling.
The bridge re-encodes decoded images as **PNG**, with `Content-Type: image/png`.
This avoids relying on URL suffixes or sending unsupported source formats directly
to clients. Pillow provides the headless codecs with an explicit format allowlist;
there is no Qt dependency. Supersonic's pinned Go
client registers PNG and JPEG decoders. Missing, corrupt, denied or unsupported
images return HTTP 503 with `Retry-After: 30`, `Cache-Control: no-store` and
`X-Artwork-Status: unavailable`. Only absent artwork metadata (including unavailable
artist portraits) returns a neutral PNG with status `placeholder`; successful covers
use `available`. Supersonic caches successfully decoded images itself, regardless
of HTTP cache headers, so temporary failures must not masquerade as valid images. SQLite metadata
contention returns a sanitized protocol error, never a successful placeholder.
No specific cause is inferred from the user's earlier unknown-format
error without its actual image bytes.

The separate `artwork/` disk cache is bounded to 32 MiB/512 entries, uses hashed
safe paths and atomic completion, recovers corrupt entries, and survives restart.
It is additional to the media-cache limit. Transient network/server failures allow
up to three attempts separated by at least 30 seconds, then a five-minute cooldown.
Denials/invalid content also cool down for five minutes; retries occur only on a new
client request. The 256-entry bounded negative cache is per URL: eviction never disables
unrelated artwork or disk hits. No timer repeatedly fetches denied URLs. Refresh the
client after recovery; it may retain old successful placeholders from earlier builds.

Apple's [iTunes Search API](https://performance-partners.apple.com/search-api)
documents `artworkUrl100` as a 100×100 URL, returned only when available. It does
not document rewriting `100x100bb.jpg` to request another size or format.
[Apple Music API artwork templates](https://developer.apple.com/videos/play/wwdc2022/10148/)
support `{w}`/`{h}` substitution, but are a different API contract. The bridge
therefore treats CDN size substitution as a best-effort convention,
not an API guarantee. Only an allowlisted `/image/thumb/` URL whose final filename
is a square numeric size followed by `bb.jpg`, `bb.jpeg`, `bb.png` or `bb.webp` is
eligible. Query-bearing URLs and unknown shapes are never rewritten. The host,
asset path and format suffix remain unchanged. There is no assumed format negotiation.

The client `size` parameter defaults to 400; values up to 4096 are accepted and
capped at 1024 (zero uses the default). Requests above the source's named size try
one of five buckets: 128, 256, 400, 512 or 1024 pixels. The user-requested
400×400 substitution is the default; a client explicitly asking for 400 also gets
a 400×400 URL rather than a larger bucket. A failed, corrupt or unsupported
variant falls back to the original URL within the same waiting budget; if the
variant consumes the budget, this request returns HTTP 503, and a subsequent
request can use the fallback while the failed variant is cooling down. Valid
images are downscaled to the requested limit but never enlarged. Variant URLs
have distinct cache keys under the same disk/concurrency/validation limits.
Missing or invalid variants follow the existing negative-cache rules rather than
being retried on every client request. A successful higher-resolution image is not
a guarantee of sharpness: Apple may return smaller or upsampled artwork.

Supersonic source confirms it passes a positive requested size to `getCoverArt`.
Tests verify dimensions above 256, client sizing, rejected variants falling back,
unknown/query-bearing URLs, PNG output and distinct cached variants. The CDN
substitution has not been verified against live Apple image responses here. A
public image URL from Apple documentation failed with a network error at the
original 100-pixel request in this environment; no alternate fetch route was tried.
Live Apple responses and actual Supersonic display still need user testing; automated
coverage uses synthetic JPEG, PNG, WebP, malformed bodies and mocked transports.

Artwork terms assessment is deferred by the user. Enabling this feature does not
assert compliance with Apple's promotional-use, badge or store-link conditions.

## Authentication and exposure

The bind address is hard-coded to IPv4 loopback. No LAN/WAN option, CORS, external
proxy support or unauthenticated stream/cover routes. Strict Host validation and
rejection of browser Origin headers reduce browser/DNS-rebinding exposure; browser
clients requiring CORS are intentionally unsupported. Credentials must be an owned,
private regular file with no symlink path components. Authentication applies to
all data routes. Only static extension discovery is public. Lightweight authenticated endpoints
(ping, library/user/license information and empty playlist/genre/radio lists)
bypass the catalog worker, so they remain responsive during catalog waits. Mixed/duplicate auth
parameters fail. Errors are fixed/sanitized and cannot contain downloader output,
HTTP bodies, credentials or account state. Never publish these local URLs with keys.

Loopback HTTP is not TLS: other processes with the same user's authority can read
private files, and a compromised client can disclose its key. Do not port-forward,
reverse-proxy or expose this experimental server. Key rotation requires restart.
The protocol's MD5 compatibility method is not modern password protection; the
secret is a generated high-entropy bridge key rather than a reused account password.

## Reference and license review

Issue [#5](https://github.com/gqgs/apple-music-player/issues/5) references
[sky8282/apple-music-subsonic-navidrome](https://github.com/sky8282/apple-music-subsonic-navidrome).
Observed HEAD: `c5139d3dcd251d6936532d25fdacec1508175707` on 2026-11-03.
Its visible root has no LICENSE file/license grant; README restrictions are not a
permissive reuse license. No code, database, credentials or binary copied. Inspection
of `main.py`/`subsonic.py` found a non-loopback bind, permissive authentication paths,
AAC-forced downloading and raw process-error printing. Its larger FastAPI/scraping/
Navidrome-specific architecture is unnecessary for this narrow adapter. This repo
also has no root license grant; this local user-authorized PoC is not a redistribution
or licensing opinion. Existing external pinned engines remain external.

Written against public protocol documentation, not copied reference implementation:
[API/authentication](https://opensubsonic.netlify.app/docs/api-reference/),
[API keys](https://opensubsonic.netlify.app/docs/extensions/apikeyauth/),
[form POST](https://opensubsonic.netlify.app/docs/extensions/formpost/),
[stream](https://opensubsonic.netlify.app/docs/endpoints/stream/).

## Validation and next steps

```sh
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_bridge.py
QT_QPA_PLATFORM=offscreen python -m pytest -q tests experiments/release_network
ruff check src tests experiments
ruff format --check src tests experiments
```

Synthetic HTTP tests exercise all authentication modes, XML/JSON, actual fake-process
ALAC download → three concurrent streams → range seeks, deduplication, cache leases,
restart metadata, mixed-format preference fallback, cancellation, sanitized errors and bad
IDs. Existing full player tests remain required. Runtime lifecycle tests use mocks;
no proprietary runtime, Apple account or media download is executed by the agent.
No Supersonic/Feishin real-client or HDMI validation has been performed.

On the user's Arch machine: configure this as a separate server in a native client,
search/open an artist, play lossless cold/warm, seek, and repeat requests. Then restart
with Atmos profile, disable client transcoding, select HDMI passthrough if the client
supports it, and verify the soundbar's Atmos indication. Merely using libmpv or
receiving E-AC-3 is insufficient. Note unsupported endpoint names without sharing
credential-bearing URLs/logs. Resolve client protocol gaps and artwork terms before
considering a merge; this PoC remains a draft for review and testing.


## Supersonic compatibility fixes

The bridge returns numeric music-folder IDs (media IDs remain opaque strings).
`getArtistInfo2` validates the requested catalog item and returns an empty similar
artist list; biography, external links and portraits are unavailable, not fabricated.
Album/song `year` and album `releaseDate` come from the validated public catalog
release date. This is not asserted to be the original release date: reissues may
have later dates. Missing/invalid dates are omitted; clients may still display zero
for genuinely unknown years.

Source inspection of [Supersonic f28980f8](https://github.com/supersonic-app/supersonic/tree/f28980f8d79d0c6b43d43b512c99d84a66632e47)
confirmed its artist-info request and album header year fallback. Its pinned
[go-subsonic types](https://github.com/supersonic-app/go-subsonic/blob/7a5f505a273c/subsonic/models.go)
confirm numeric folder IDs and years. Both projects were inspected read-only;
no GPL implementation code was copied or added as a dependency. JSON/XML contract
regressions use synthetic catalog data; this is not real-client UI validation.
The actual client must be retested after restarting the bridge and refreshing its
server/album data.

Artwork fetching now replaces the initial placeholder-only implementation; see the
format, safety and terms limitations above.


## Search latency and overload investigation

Source-reviewed Supersonic `f28980f8` SearchAll sends `search3` (16 results per
category) alongside playlist, genre and radio requests. The latter now return
immediately, independently of catalog work. This PoC has no genre/radio indexes;
empty lists do not describe the entire Apple catalog. Its Albums iterator expands
artist matches through getArtist and song matches through getAlbum before yielding
its results. That can produce many additional rate-limited catalog requests.

A synthetic clock/HTTP fixture reproduces cold search start times at 0, 3 and 6
seconds for artists/albums/songs. Identical cached searches avoid those upstream
calls. This is a scheduling model, not measured live Apple latency. Album expansion
adds more requests; network time is additional. No parallel upstream requests,
rate-limit bypass, hidden result-type omission or inferred query cancellation is used.

Each admitted HTTP metadata operation now has a 12-second deadline including its
queue wait. Expired queued tasks skip dispatch and retain their admission slots until drained
(to bound the executor queue even during a stalled upstream request); running work checks the deadline
before/after upstream calls and before cooldown sleeps, so it does not proceed to
remaining categories after expiry. An in-flight urllib request cannot be forcibly
interrupted and finishes subject to its existing network timeout. Client disconnects
and changed queries are not uniquely identified: no global latest-query rule is
applied across clients/tabs. Cold media downloads retain their separate existing
longer timeout on the separate download worker. Shutdown cancels the shared download
cancellation event, cancels queued download futures and waits for active downloads
before closing runtime/cache ownership. No client disconnect cancels another user's
shared download.

Search numeric parameters are validated together before any upstream request, and
result child counts are computed once per metadata revision instead of repeatedly
scanning the complete local catalog for every result. Counts remain locally known
children, not a claim of full Apple discography size.

Tests reproduce parallel Supersonic auxiliary traffic during a blocked search,
responsive cached covers, explicit admission failures, queued/running expiry,
rate-spacing preservation, cached pagination and count invalidation. Real Supersonic
blank-screen recovery and live search behavior still require retesting. If slow
search persists, distinguish Search Everywhere from filtering the Albums/Artists
pages; those client flows have materially different request fan-out.


## Burst regressions and sanitized diagnostics

An authenticated synthetic HTTP replay uses 40 album covers, five client workers
(the inspected Supersonic limit), and a 150ms fake image-fetch delay. The previous
four-waiter bridge returned four covers and 36 successful placeholders. The queued
implementation returns all 40 covers with the same two fetch workers. This is a
synthetic scheduling test, not a live CDN or Supersonic UI benchmark. Tests also cover
warm disk hits under saturation, more than 256 failed keys, 503 instead of a cacheable
error image, search during a blocked download, actual fake-process cancellation,
download deduplication, range leases and rate-limiter preservation.

Add `--diagnostics` to the existing bridge launch command to print a JSON aggregate
every 30 seconds. These lines contain only allowlisted stage/outcome names, counts,
and total/maximum milliseconds; no search terms, URLs, item IDs, headers, credentials
or raw exceptions. Restart resets the counters. The same snapshot is available from
the authenticated nonstandard `getBridgeDiagnostics` endpoint. Do not share other
client logs containing authentication query parameters.

Stages cover disk hits/misses, fetch/decode outcomes, artwork wait time, catalog
queue/operation time and download outcomes. Fetch failures distinguish network,
server, denied, TLS, invalid content and other HTTP failures without response bodies.
The metrics describe completed operations (plus explicit timeout/busy events), not
percentiles or a promise that every pending operation completes within its deadline.

## Release types contract for native clients

Every serialized album includes OpenSubsonic `releaseTypes`: `getAlbum`, albums
embedded in `getArtist`, `search3` album results, and `getAlbumList2`. Song/artist
objects do not acquire an album-only field. The mapping uses the existing frozen,
direct-weight classifier; it is approximate, not Apple's editorial classification.
No classifier weights, catalog IDs, titles, sorting or playback behavior change.

| Classifier category | Wire values |
| --- | --- |
| Albums | `["Album"]` |
| Singles & EPs | `["EP"]`, `["Single"]`, or both, using the classifier's existing EP/single token features |
| Live Albums | `["Live"]` |
| Compilations | `["Compilation"]` |
| Others, with a nonempty title | `["Other"]` |
| Missing/blank title, unsupported category, or no subtype evidence | `[]` |

Values use the spelling above; clients should normalize case. `Other` is an
explicit category, distinct from unknown/absent metadata. Clients must not turn
an empty array or unrecognized future string into explicit Other. In XML, each
value is a repeated `<releaseTypes>` text element; an empty list has no elements.
The title can contain both EP and Single evidence; both values still belong to
the client's combined Singles & EPs section. Live/Compilation are emitted alone,
without inventing a studio-album primary type or an additional subtype.

This is the integration contract for the Supersonic fork: map explicit `Other`
to its Other flag/group, preserve an unknown state for absent/unrecognized types,
and group EP/Single together. The inspected upstream normalizer currently falls
back to Album when no recognized flags exist, including explicit Other; that client
behavior needs the separate fork patch. No Supersonic repository code is changed
by this bridge update. The mapper is independent of Qt and covered by JSON/XML
HTTP regressions across all four album response locations.

Protocol source: [OpenSubsonic AlbumID3 releaseTypes](https://opensubsonic.netlify.app/docs/responses/albumid3/),
an optional array of strings rather than a closed enum. This metadata does not
claim complete or authoritative release categorization.

## Metadata scheduling follow-up (#8)

Catalog cooldown and HTTP 429 deferrals now wait outside the sole catalog/cache
worker. A warm artist lookup can finish while a cold search waits for its next
public request. Each logical RPC keeps one of four admission slots and its original
12-second deadline across retries. Completed catalog responses are reused on retry;
expired queued work is drained before releasing admission. Shutdown interrupts waits.
The same shared three-second spacing and upstream Retry-After remain enforced.

Diagnostics now distinguish `metadata_queue` (each executor admission),
`metadata_work` (each dispatch attempt, including blocking upstream I/O), and
`catalog_wait` (deferred time outside the worker). `catalog_wait:rate_limited`
identifies an explicit 429 deferral; `deferred` may include either local spacing or
an already-established upstream cooldown. `metadata` measures the complete logical
RPC, including queue and deferred time, rather than only its dispatch execution.
Counts across these stages need not match. All remain aggregate and secret-free.

A synthetic regression with the real Catalog limiter/cache performs four requests
at least three seconds apart. A cached artist request completes within one second
while the cold three-entity search waits. On the previous implementation that warm
request times out in the regression. This fixes head-of-line blocking during waits,
not all search latency: a cold search still needs multiple spaced public requests,
and a blocking upstream network call (15-second socket timeout) can still occupy the
worker. Supersonic's additional artist/album lookups add demand. Live client retesting
is required; cumulative maxima cannot attribute a timeout to a specific query, and
the reported successful cover fetches do not establish artwork as the cause.


### Rapid typing remains a client integration problem

Source inspection of Supersonic fork commit
[`95df47f`](https://github.com/gqgs/supersonic/tree/95df47fea0e8e232047444b35c7617a2865157f6)
found a 200ms SearchEntry debounce, a separate goroutine per search, and generation
checks on returned UI results rather than HTTP cancellation. Its configured default
HTTP timeout is 15 seconds. Four cold queries (`b`, `bj`, `bjo`, `bjor`) may require
12 public catalog requests, at least 33 seconds between the first and last starts.
Worker fairness does not remove that upstream capacity limit.

The bridge metadata deadline is now 12 seconds, below that client default, so it
can return a structured error before the client abandons the response. This is a
bounded failure improvement, **not a fix for superseded search traffic**. Different
client timeout settings or a blocked local HTTP writer can still time out. Already
running upstream I/O is not forcibly interrupted; expired dispatch stops before
starting another entity request.

The bridge deliberately does not cancel queries based on `c`, the account, localhost,
or query prefixes: different windows/clients may share all of those. A complete
integration needs client-side search-on-submit/debouncing and per-view cancellation,
plus a reliable cancellation signal (or explicit random per-view session ID and
monotonic generation) to retire server-side work. Merely hiding old UI results or
raising the HTTP timeout is insufficient. No new session protocol is silently
assumed here, and no Supersonic source was copied into this project.


Identical concurrent `search3` calls now use singleflight: one bounded shared job,
up to four distinct jobs, and at most the existing 16 HTTP handlers. Only exact
semantic parameters in the same authenticated identity/client scope share work;
per-request password/token/salt differences are ignored after authentication. Keys
are ephemeral process-keyed HMACs, never credential-bearing log/cache filenames.
Different queries, users/API keys, client names, pagination and entity counts remain
separate. Each waiter has its own deadline; timing out one waiter does not cancel
others. When all waiters leave, pending work stops at the next safe boundary; a
blocking upstream call is not forcibly interrupted. Completed/failed flights are
removed, not retained as a result/error cache. `search_flight:joined` counts duplicate
waiters without exposing identities or queries.

The HTTP regression sends eight simultaneous identical searches and verifies one
three-entity dispatch serves all eight. This complements a longer client debounce;
it deliberately does not collapse the four distinct typing-prefix queries. Client
cancellation/debounce changes are separate from this bridge PR.

### Artist-first client search contract

A client can request only artists with `search3`, `artistCount=20`, `albumCount=0`,
`songCount=0`, and `artistOffset=0`. This makes one cold upstream request instead
of three. Album-only and song-only requests work the same way; mixed requests and
omitted counts (20 each) retain their normal protocol meaning. No client names are
special-cased and no requested category is silently discarded.

The bridge requests an upstream limit of `min(offset + count, 200)` for each enabled
category, then slices locally. For example, the second artist page (`offset=20`,
`count=20`) fetches a 40-result prefix and returns entries 21–40. Identical pages
reuse the catalog cache; a larger prefix is another request through the shared
three-second limiter. No undocumented upstream offset is sent. Offset 200 returns
an empty page without another request. This is a bounded public search window,
not full-catalog pagination; upstream ranking/catalog changes can shift pages.
Artist/album detail lookups
remain explicit, separate operations. Client-side artist-first navigation must
avoid automatic album/song expansion to realize the latency benefit.

[Apple's public Search API documentation](https://performance-partners.apple.com/search-api)
specifies limits from 1 to 200 and recommends reducing requested result counts for
response time. Its documented search parameters do not include an offset. The
bridge's prefix-and-slice approach makes that limitation explicit.

### Artist counts and release covers

Artist search/index entries omit optional `albumCount` and `artistImageUrl`:
public artist search does not supply a verified total discography count or a
supported portrait source. `getArtist.albumCount` counts only albums returned in
that response, subject to lookup limits and storefront coverage. Clients should
hide missing counts instead of presenting zero as a verified total.

Artist entries advertise a `coverArt` resolver ID. This uses exact-artist and
storefront release artwork, not a portrait. Search and index responses do not
expand artists into album lookups. See [artist artwork](artist-artwork.md) for
background limits, cache behavior, pending responses and source metadata.

Album-only client search (`artistCount=0`, `albumCount=50`, `songCount=0`) is covered
by an authenticated HTTP regression: exactly one upstream album request, limit 50.
Clients must also stop eagerly expanding artist/song matches; otherwise the earlier
multi-request latency returns. A live public artist-response check was unavailable
through the web tool during this change; no new portrait endpoint or URL was guessed.


### Optional lyrics requests

The bridge has no lyrics source. `getLyrics` returns an empty `lyrics` object and
`getLyricsBySongId` returns an empty `structuredLyrics` list, after authentication
and typed song-ID validation. Neither request fetches catalog or lyric data.
These use the [standard lyrics response](https://opensubsonic.netlify.app/docs/endpoints/getlyrics/)
and [structured lyrics list](https://opensubsonic.netlify.app/docs/endpoints/getlyricsbysongid/)
shapes, rather than reporting an unimplemented endpoint during ordinary playback.
This is an empty-result compatibility response, not lyrics support or a new provider.
