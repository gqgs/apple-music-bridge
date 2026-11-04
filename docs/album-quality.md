# Album quality availability

The optional `appleMusicAlbumAudioQuality` extension (version 1) exposes Apple
Music's album-level availability badges before downloading any track. It is a
bridge-specific extension, not an official OpenSubsonic standard.

A compatible client checks `getOpenSubsonicExtensions`, then calls the authenticated
`getAlbumAudioQuality?id=<bridge-album-ID>` endpoint. The normal XML or JSON response
contains `albumAudioQuality` with:

- `id`: the exact requested bridge album ID.
- `source`: `apple-music-catalog`.
- `available`: recognized availability strings: `dolby-atmos`, `dolby-audio`,
  `hi-res-lossless`, and `lossless`. An empty list means no badge to display; it is
  not evidence that the album is lossy.

Clients should request this asynchronously, hide missing badges, display Hi-Res
Lossless in preference to a redundant Lossless badge, and retain concurrent Atmos
availability. Do not derive playback format, passthrough success, sample rate,
bit depth, or per-track availability from an album badge.

## Data source and limitations

The regular bridge catalog uses public iTunes Search/Lookup metadata, which does
not supply these badges. Apple's documented Music API has `audioVariants`, but
requires a developer token. This optional lookup instead reads the same badge
booleans embedded in the public Apple Music album page, without extracting web
client tokens, signing in, or invoking the audio downloader.

Only the requested HTTPS `music.apple.com` album and storefront are accepted,
including across redirects. The parser requires the exact album header identity,
content descriptor, and boolean badge fields. Changed markup, missing data,
wrong-region redirects, or failed requests produce no badge. This public-page
structure is not a guaranteed API; it may stop working until the parser is updated.

The fetch has a 15-second socket timeout and a 1 MiB response limit. It runs outside
the serialized catalog/download workers. Results are cached in memory for 24 hours,
unknown results for five minutes, capped at 256 entries. Duplicate in-flight work
is shared, and no more than two distinct lookups run concurrently. HTTP 429 stops
new lookups for five minutes. Restarting the bridge clears this metadata cache.

The request sends only the public album URL and ordinary HTTP headers, with no
Apple account information, cookies, session tokens, or browsing history. Like any
public webpage request, Apple receives the caller's network address. Opening a
cold album incurs roughly one page download plus any canonical redirect; observed
Punisher/Sunbather pages were about 360–450 KiB. No audio is downloaded for a badge.

## Verified references

The album header booleans were checked against these public pages on 2026-11-04:

- [Punisher](https://music.apple.com/us/album/punisher/1504699857): Hi-Res Lossless.
- [Sunbather](https://music.apple.com/us/album/sunbather/652038965): Dolby Atmos and Lossless.

Brazilian storefront pages for the same IDs also matched. These are observations,
not permanent hardcoded album claims.

Official background: [album audio variants](https://developer.apple.com/documentation/applemusicapi/albums/attributes-data.dictionary)
and [developer tokens](https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens).
