# Local OpenSubsonic bridge proof of concept

Draft PoC branch `poc/opensubsonic-bridge`, based on main `22d9dce` (PR #6).
Published for review and testing; no change to the desktop UI. This is a read-only API
subset, not a tested replacement for Navidrome or a promise of client compatibility.

## Run locally

Use the existing player development environment. No new dependencies:

```sh
. .venv/bin/activate
python -m apple_music_player.bridge --auth-file ~/.config/apple-music-bridge/auth.json --init-auth
python -m apple_music_player.bridge --auth-file ~/.config/apple-music-bridge/auth.json
```

The first command creates a random bridge-only key in a new 0600 file. Open it in
your local editor; never paste its contents into chat/logs. Configure a native
OpenSubsonic client with URL `http://127.0.0.1:4533`, preferably API-key authentication.
For older clients use username `bridge` and that same random key as the password
(token/salt, plain and hex protocol authentication are supported). This is **not**
your Apple password. Stop the bridge, replace the credential file using `--init-auth`,
and restart to revoke the old key. Prefer clients using form POST so credentials
are not in their URL history/logs. The bridge itself has no request/access logging.

By default the service browses public metadata and streams **already cached files
only**. It does not start/install/authenticate any proprietary component. To enable
cold downloads on your own machine after completing the existing managed setup,
close the desktop player and explicitly launch:

```sh
python -m apple_music_player.bridge \
  --auth-file ~/.config/apple-music-bridge/auth.json \
  --country br --quality lossless \
  --start-runtime ~/.local/share/apple-music-player
```

Adjust the runtime path if you use `XDG_DATA_HOME`. This explicitly starts only the
already installed and locally signed-in managed runtime, verifies the existing
pin/installation through `ManagedRuntime`, checks the returned storefront before
download, and closes its own runtime on exit. No login prompt, sudo, installer or
security setting changes. A busy runtime/cache is rejected, not taken over.

The default cache is `~/.cache/apple-music-bridge` (or under `XDG_CACHE_HOME`), 10 GiB.
It reuses the player's Cache implementation but is separate from the desktop cache.
Use `--cache ~/.cache/apple-music-player` **with the GUI closed** if you explicitly
want the existing app-owned media cache. Only app-owned cache directories are suitable;
never point this at an arbitrary downloads/music folder. Existing lock, reconciliation,
atomic completion, staging bounds and eviction safety checks apply.

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
- `getAlbumList2` (`alphabeticalByName` and `newest`), empty read-only `getPlaylists`;
- `getCoverArt` placeholder PNG and `stream` original completed-file bytes.

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

One core worker owns SQLite, catalog requests and downloads. At most eight HTTP
workers/connections are active; excess connections are closed. This serializes cold
misses, deduplicates successful identical downloads, bounds queue growth and permits
concurrent warm streams. Slow downloads also delay metadata work: an explicit PoC
tradeoff. Full files download/probe/commit before response headers. Downloader timeout
is 900 seconds; a client's shorter timeout may require retry after completion.
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

## Artwork limitation (deliberately unresolved)

`getCoverArt` authenticates and resolves the catalog ID but currently returns an
original neutral PNG marked `X-Artwork-Status: placeholder`. It does **not** fetch
Apple images. The current app's artwork approach requires an official badge and
appropriate catalog association; a generic external client's layout cannot be
controlled or verified by this adapter. Before enabling Apple covers, review the
specific client's attribution/link support and applicable promotional artwork
conditions. No unofficial badge, URL-rewriting trick or unapproved image reuse was
added. Thus the artwork **protocol flow** works; real album covers are a blocker,
not claimed complete. No legal/terms compliance conclusion is implied.

## Authentication and exposure

The bind address is hard-coded to IPv4 loopback. No LAN/WAN option, CORS, external
proxy support or unauthenticated stream/cover routes. Strict Host validation and
rejection of browser Origin headers reduce browser/DNS-rebinding exposure; browser
clients requiring CORS are intentionally unsupported. Credentials must be an owned,
private regular file with no symlink path components. Authentication applies to
all data routes. Only static extension discovery is public. Mixed/duplicate auth
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
restart metadata, quality fallback refusal, cancellation, sanitized errors and bad
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

Covers remain placeholders. Apple's [Search API usage conditions](https://performance-partners.apple.com/search-api)
restrict promotional artwork use and require associated store promotion/linking;
adding a badge alone is not a blanket permission. Enabling covers in an arbitrary
client requires a separate assessment of its display and applicable conditions.
