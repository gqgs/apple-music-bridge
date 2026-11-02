# Home and artwork

Home shows albums actually played on this device, after mpv verifies output and playback time advances. Enqueueing, downloads and failed starts do not create history. History is independent of media eviction, bounded to 200 albums and stored in `$XDG_DATA_HOME/apple-music-player/library` (default `~/.local/share`). Clear local history removes it. Direct URLs without album metadata cannot contribute an album; no older cache timestamps are imported as listens.

“Recent releases from artists you’ve played” uses public artist-to-album lookups for at most five recent artists in the selected storefront, with hourly in-memory caching and the existing request spacing/retry limits. Returned releases within 90 days are deduplicated and sorted newest first. The public selection can be incomplete, and release dates may describe reissues. This is neither an editorial feed nor personalized Apple recommendations. Empty results stay empty. Navigation cancels superseded work.

Search, album details and now playing use returned artwork URLs without undocumented resizing; `artworkUrl100` is normally 100px. Fetches run on two workers with bounded pending work, HTTPS host/redirect checks, timeouts, 2 MiB encoded and 2048px decoded limits. A separate 32 MiB/512-file artwork cache uses hashed names and atomic writes; corrupt files recover with placeholders. This allowance is additional to the media cache and staging budget.

Artwork is promotional material. Cards provide direct store links. Covers are displayed only when the approved iTunes badge has loaded; otherwise neutral placeholders and text links remain. The official badge download was blocked with HTTP 403 in the development environment, so live badge/artwork delivery is not claimed validated. No substitute Apple branding is bundled.

Sources: [Search API and promotional-content conditions](https://performance-partners.apple.com/search-api), [official badge guidelines](https://marketing.services.apple/itunes-identity-guidelines). Tests use synthetic images and metadata. `home-smoke.png` and `browsing-search-smoke.png` show neutral synthetic fixtures, not account data or live artwork.
