# Artist artwork retention and retry contract

Artist artwork is the newest **known**, available release, not necessarily the
newest release in the live catalog. No background freshness polling is added.
Browsing newer albums or completing an exact artist lookup updates the retained
selection. Same-release metadata updates replace the retained payload too.

`bridge_artist_artwork` retains a validated artist identity and at most one
selected album per full artist/storefront/quality key, separately from the 5000
bulk metadata rows. It retains at most 5000 artists, ordered by artist browsing
or lookup insertion; bulk album insertion does not age out other artists. The
retained selection also preserves the result of an exact collaborator lookup,
without depending on the bulk association table's continued retention. Names
never establish an association. Once an artist itself leaves this bounded cache,
browsing/resolving the exact artist again restores its identity; arbitrary cover
IDs do not fabricate artists or initiate identity lookups on HTTP workers.

Upgrade creates and backfills the compact table without deleting media or image
caches. Before this version, `release-unavailable` checks could represent either
success or absence. Upgrade expires those ambiguous checks once. Re-evaluation
is demand-driven through the existing bounded queue and catalog limiter. Newly
learned absence is still cached for one hour. Successful lookups now record
`release-resolved`; missing or damaged selected data under that state permits
repair instead of returning a false one-hour 404. Provider failures and cooldowns
retain their transient deadlines. The migration marker is table existence and
all migration writes commit together.

For HTTP 503 with `X-Artwork-Status: unavailable`, these existing reasons denote
retryable artwork: `release-pending`, `release-busy`, `release-deferred`, and
`release-failed` (the last is a transient catalog-provider exception). Clients
honor `Retry-After` without holding a download slot. HTTP 404
`release-unavailable` remains confirmed absence. Authentication, malformed image
responses, and unknown failures remain distinct. The matching Supersonic change
uses at most three deferred visible-card retries and cancels them on navigation,
recycling or cache-generation changes. Long cooldowns do not disable recovery.

The JSON fixture `tests/fixtures/artwork_retry_protocol.json` is shared verbatim
with Supersonic's `backend/mediaprovider/subsonic/testdata` and checked against
actual bridge HTTP responses and the client decoder respectively.

This change preserves one release worker, shared three-second catalog start
spacing and foreground priority. It improves retention and recovery; it does not
make cold artist grids instant. Album-search seeding is not implemented without
live public-catalog coverage evidence.
