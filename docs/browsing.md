# Home and artwork

Home shows albums actually played on this device, after mpv verifies output and playback time advances. Enqueueing, downloads and failed starts do not create history. History is independent of media eviction, bounded to 200 albums and stored in `$XDG_DATA_HOME/apple-music-player/library` (default `~/.local/share`). Clear local history removes it. Direct URLs without album metadata cannot contribute an album; no older cache timestamps are imported as listens.

“Recent releases from artists you’ve played” uses public artist-to-album lookups for at most five recent artists in the selected storefront, with hourly in-memory caching and the existing request spacing/retry limits. Returned releases within 90 days are deduplicated and sorted newest first. The public selection can be incomplete, and release dates may describe reissues. This is neither an editorial feed nor personalized Apple recommendations. Empty results stay empty. Navigation cancels superseded work.

Search, album details and now playing use returned artwork URLs without undocumented resizing; `artworkUrl100` is normally 100px. Fetches run on two workers with bounded pending work, HTTPS host/redirect checks, timeouts, 2 MiB encoded and 2048px decoded limits. A separate 32 MiB/512-file artwork cache uses hashed names and atomic writes; corrupt files recover with placeholders. This allowance is additional to the media cache and staging budget.

Artwork is promotional material. Cards provide direct store links. Covers are displayed only when the approved iTunes badge has loaded; otherwise neutral placeholders and text links remain. The official badge download was blocked with HTTP 403 in the development environment, so live badge/artwork delivery is not claimed validated. No substitute Apple branding is bundled.

Sources: [Search API and promotional-content conditions](https://performance-partners.apple.com/search-api), [official badge guidelines](https://marketing.services.apple/itunes-identity-guidelines). Tests use synthetic images and metadata. `home-smoke.png` and `browsing-search-smoke.png` show neutral synthetic fixtures, not account data or live artwork.

## Retry and small-screen follow-up

Transient artwork network failures and HTTP 5xx responses now expose a **Retry artwork** action after 30 seconds, capped at two retries following the initial request. HTTP 401/403 (including recognized proxy CONNECT denials), other HTTP errors, TLS verification failures and invalid images do not enter this retry path. Fixed messages distinguish these categories without raw URLs, response bodies or exception text. There is no automatic retry loop. The per-session failure set is bounded to 256 URLs.

The original badge attempt was recorded as a 403 during tunnel establishment; the complete original exception/headers were not retained, so an origin response cannot be established. The URL came from the image link under section 1.1 of the official guidelines, not from a verified asset download. No denied request was retried during this fix.

**Outstanding draft review:** the current per-card badge placement conflicts with section 1.4's one-badge-per-layout guidance. The Search API also requires artwork proximity to a badge. This follow-up does not assert that the current display model is approved and does not remove the badge gate; placement needs resolution before merging the artwork feature.

With both Home shelves populated, the old layout forced a 1019px minimum window height. Home and Search now scroll vertically, keeping transport controls outside the scrolled area. `home-small-screen-smoke.png` is a synthetic 480-logical-pixel viewport rendered at 150% scale (720 physical pixels); both shelves are populated and reachable by scrolling. No live artwork is shown.
