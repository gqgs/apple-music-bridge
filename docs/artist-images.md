# Experimental bridge artist images

Append `--artist-images` to your existing bridge launch command. No new dependencies,
login or API key are required. It is disabled by default, does not affect Qt artwork,
and does not install/start the managed runtime unless your existing command does so.
Use it with a client that displays image credits, such as an image-info action.

Search returns artist cards immediately. The opt-in cover ID starts with
`artist-image:`; `getCoverArt` triggers a separate bounded background lookup.
An image may arrive later, be unavailable, or need another client request after a
503. The bridge does not promise an automatic refresh in every Subsonic client.
It never turns a transient failure into a cacheable successful placeholder.
A confirmed missing mapping/image returns a non-cacheable error, not a successful
placeholder: some clients persist decoded placeholders regardless of `no-store`.
The client can display its own neutral fallback.

## Identity and source selection

1. Look up exact Apple artist URLs via MusicBrainz's documented multi-resource URL
   lookup, including the returned `artistLinkUrl`/`artistViewUrl` when it validates
   against the same numeric Apple ID. Fixed countryless/current-country/US URL
   forms are identity references, not Apple catalog requests or region remapping.
   Include canonical iTunes URLs for US, the selected storefront and the validated
   returned URL's country: `https://itunes.apple.com/<country>/artist/id<ID>`.
   MusicBrainz stores these with the `id` prefix and matches URLs exactly; a
   countryless or slugged link alone is insufficient.
2. Accept exactly one linked MusicBrainz artist MBID. No name search, fuzzy matching,
   scraping, user credentials, or assumption that Apple IDs equal MBIDs.
3. Prefer a Commons **image** relationship from that artist. Otherwise follow a
   unique Wikidata relationship and its non-deprecated P18 image (preferred rank
   wins; conflicting values are rejected). P154 logos and cover-art substitutions
   are deliberately excluded from this experiment.
4. Ask Commons Imageinfo for its thumbnail, source page, author and license. Only
   known CC BY/CC BY-SA versions, CC0 or Public Domain with matching standard license
   URLs and usable attribution are accepted. Missing/unrecognized metadata is not
   guessed. Unsupported images remain unavailable.

MusicBrainz relationships and Wikidata statements are community-maintained evidence,
not a guarantee of a portrait, current appearance or perfect identity coverage.
Multiple valid Commons image relationships are selected deterministically; no
claim of popularity or editorial preference is made. Returned metadata calls it an
`artist-image`. Coverage can be sparse when Apple URL relationships are missing.

## Client attribution contract

After a successful cover request, fetch authenticated
`getArtistArtworkInfo` with the **artist ID**, not the prefixed cover ID. It returns:

```json
{
  "artistArtworkInfo": {
    "status": "ready",
    "credits": {
      "creator": "Creator text",
      "credit": "Additional supplied credit",
      "license": "CC BY-SA 4.0",
      "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
      "sourceUrl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
      "artistMbid": "11111111-1111-1111-1111-111111111111",
      "imageKind": "artist-image",
      "origin": "MusicBrainz image relationship",
      "changes": "Resized and converted to PNG"
    }
  }
}
```

This example is synthetic. Other statuses are `disabled`, `pending`, `unavailable`
and `retry`. Failed/missing results include a sanitized `reason`. The endpoint is cache-only: it never starts or waits for network work.
Display creator, supplied credit, license/link, source/link and changes in a visible
image-info affordance; all text is plain text, not trusted HTML. Do not imply that
Wikidata's data license licenses the image. Cached credits also appear as explicitly
labeled image-credit text in `getArtistInfo2.biography` for compatibility; they are
not an invented artist biography. Clients may need to reload that info after the
image finishes. JSON and XML require the existing bridge authentication.

## Bounds, caching and privacy

- One independent image-resolution worker, at most eight distinct queued/running
  artist jobs; identical jobs share work. Search/download workers are not used.
- MusicBrainz calls start at least 1.1 seconds apart, shared across image services
  within this process. Wikimedia calls use a separate equally conservative gate,
  a descriptive contact User-Agent, and `maxlag=5`. Do not run multiple bridge
  processes alongside other heavy MusicBrainz tools and assume their IP-wide usage
  is coordinated. 429/503 Retry-After establishes a cooldown; no busy retry loop.
- HTTP image wait is at most eight seconds; the background job has a cooperative
  25-second budget including queue time. Socket calls have at most five-second
  timeouts and bounded reads. A blocking socket/DNS operation is not forcibly
  killed and can finish after the cooperative deadline. Shutdown cancels queued
  work and interrupts waits; no new requests start after cancellation.
- HTTPS only, fixed API hosts, metadata redirects refused, image redirects limited
  to `upload.wikimedia.org/wikipedia/commons/`. JSON is capped at 256 KiB, source
  images at 2 MiB/2048 pixels per dimension, decoded thumbnails at 500 pixels.
  Only decoded JPEG/PNG/WebP inputs are accepted; output is verified PNG.
- Private `artist-images/` under the bridge cache stores atomic image+credit records,
  bounded to 128 records/32 MiB (temporary writes add at most one record). Successes
  last seven days, missing/ambiguous results one day, transient failures five minutes.
  Expiry does not slide on reads. Symlinks/hardlinks and malformed cache entries are
  rejected; interrupted temporary writes are cleaned on restart. This cache is
  separate from music and album-cover storage. It contains public IDs/credits/images,
  no credentials, account data, raw HTTP bodies, or query logs.

Provider requests contain public Apple artist URL references, MBIDs, Wikidata IDs
and Commons filenames only. No Apple playback/authentication tokens are forwarded.
The default neutral fallback remains when this flag is absent. Existing cached
artist records without a store URL remain readable.

## Sources and validation

- [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API): URL resources,
  artist URL relationships, identification, and one-request-per-second requirement.
- [MusicBrainz's Commons selection logic](https://github.com/metabrainz/musicbrainz-server/blob/1bedfed82b794a23ced029d397da01f2bd4d4256/lib/MusicBrainz/Server/Controller/Role/CommonsImage.pm)
  and [bulk URL response tests](https://github.com/metabrainz/musicbrainz-server/blob/1bedfed82b794a23ced029d397da01f2bd4d4256/t/lib/t/MusicBrainz/Server/Controller/WS/2/JSON/LookupURL.pm)
  were inspected read-only; no GPL implementation code was copied.
- [Commons Imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo),
  [API etiquette](https://www.mediawiki.org/wiki/API:Etiquette), and
  [image reuse guidance](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia).

Tests use synthetic provider responses, images and HTTP clients. They exercise direct
Commons and Wikidata paths, ambiguity, credits, unsafe redirects, payload limits,
provider cooldown, cache recovery/expiry/eviction, bounded jobs/deduplication and
search while image work is blocked. Documentation and Björk's public MusicBrainz
Apple link were source-checked; no complete live image resolution or Supersonic
credit-display validation is claimed. Review per-image credits; this is not a blanket
permission statement about all Commons images.

## Troubleshooting artist image requests

`getCoverArt` errors use XML even when `f=json` is requested: Supersonic's
Go client recognizes XML errors but attempts image decoding for other content.
Unavailable images return HTTP 503, `Cache-Control: no-store`, `Retry-After: 30`
and `X-Artwork-Reason`. No successful placeholder is returned for opt-in artist
images, including confirmed misses. Existing client-cached placeholders are not
remotely invalidated by this change.

Reasons include `identity-missing`, `identity-ambiguous`, `attribution-unavailable`,
`musicbrainz-http-503`, `musicbrainz-network`, provider `-cooldown`/`-invalid`,
`lookup-pending`, `lookup-expired`, and `queue-full`. These are fixed categories;
provider bodies, URLs and exception text are never exposed. Old cached failures
without a reason show `cached-retry` until their existing five-minute expiry.
Share only the bracketed reason when reporting a failure, not request URLs or
credentials. A network reason does not distinguish DNS, proxy, TLS or connection
failure; it is not evidence of an authentication problem.

The client needs bounded delayed retries for transient failures: the first response
can time out while background work continues and later populates the cache. It
should stop retrying when leaving the view, avoid caching error responses, and
respect Retry-After. The bridge does not implement client retries. Missing matches
remain negatively cached for a day; repeatedly requesting them does not query the
provider again. Live provider resolution remains unverified in this environment:
a MusicBrainz API attempt was blocked by the environment proxy (tunnel HTTP 403).

### Canonical iTunes identity correction

The original experiment omitted canonical US/current-storefront iTunes URLs and
incorrectly removed `id` while normalizing a returned iTunes artist URL. This could
produce `identity-missing` for an artist whose only matching relationship used that
canonical URL. The corrected forms join the same multi-resource request; there are
no extra requests or name-based matching. The URL set is part of the cache key, so
old negative entries cannot suppress the corrected lookup. Existing files remain
subject to normal cache eviction; no manual cache deletion is needed.

Source evidence:
[MusicBrainz iTunes normalization](https://github.com/metabrainz/musicbrainz-server/blob/1bedfed82b794a23ced029d397da01f2bd4d4256/root/static/scripts/edit/URLCleanup.js#L3611)
and [exact URL lookup](https://github.com/metabrainz/musicbrainz-server/blob/1bedfed82b794a23ced029d397da01f2bd4d4256/lib/MusicBrainz/Server/Data/URL.pm#L337).
Tests model these URL forms and provider response structure through search,
getArtists, getCoverArt and full restart, including a pre-existing negative cache.
They do not prove live coverage for a particular artist, missing relationships in
other countries, or the identity selected by an anonymous diagnostic probe.

### Album/portrait cache headers

Successful images emit exactly one `Cache-Control: private, max-age=86400` header;
errors emit exactly one `Cache-Control: no-store`. Previously the default no-store
header was sent before an additional cacheable header. Go's `Header.Get` reads the
first value, so a client interpreting no-store as a retry signal fetched even a
valid cached album cover four times. The bridge now replaces its default header
instead of appending a conflicting value. Tests reproduce that client decision
with real HTTP responses and verify album retrieval during a pending portrait.
Clients should also keep image cache policy separate from retry policy: no-store
alone does not mean a valid image is transient or should not be displayed.
