# Artwork resolution and caching

`getCoverArt` honors a positive `size` up to a bounded 1400 pixels. An omitted
or zero `size` requests that full-size bound, rather than the old 400-pixel
thumbnail default. Both album/song artwork and artist release artwork use the
same policy. Accepted sizes above the bound are reduced to it (the HTTP numeric
input limit remains 4096). Source images are never enlarged, including fallback
artwork.

Clients should request the displayed image dimensions in physical pixels
(logical size multiplied by display scale), rounded up to a stable size bucket.
For example, a 565-pixel square calls for 600 pixels at 1x or 1200 pixels at 2x;
a 50-pixel row cover needs only the 128-pixel bucket at either scale. The bridge
uses these CDN buckets: 128, 256, 300, 400, 512, 600, 1024, 1200, and 1400.

The catalog supplies iTunes `artworkUrl100`, not an Apple Music API artwork
template. [Apple's iTunes documentation](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/UnderstandingSearchResults.html)
documents the returned 60- and 100-pixel URLs. Larger variants are a best-effort
CDN convention, not an API guarantee or an exhaustive supported-size list.
Only unsigned square `.../image/thumb/.../NxNbb.jpg` (or jpeg/png/webp) paths on
the existing allowlisted hosts are rewritten. Failed variants retain fallback
to the original URL, negative retry caching, and no-upscaling behavior.

## Verification sample

An iTunes lookup of Kiasmos's *II*, collection 1740326550 (track 1740327518), on
2026-11-04 returned a 100-pixel URL whose variants decoded to the exact requested
sizes below. This is evidence for that asset, not a catalog-wide guarantee.

| Requested pixels | JPEG bytes |
| --- | ---: |
| 100 | 7,447 |
| 128 | 10,523 |
| 256 | 37,692 |
| 300 | 51,720 |
| 400 | 88,008 |
| 512 | 147,252 |
| 600 | 202,885 |
| 1024 | 564,390 |
| 1200 | 764,867 |
| 1400 | 1,031,464 |

Requesting 300 instead of 400 pixels saved 41% of source bytes in that sample.
Network latency was not controlled, so these measurements do not establish a
load-time improvement. Full-size artwork naturally costs more than thumbnails;
clients should not request the maximum for every card or list row.

## Cache and processing behavior

Source cache keys remain the full CDN URL, with the existing 32 MiB / 512-file
bound and seven-day idle expiry. Different size variants have distinct keys;
equal requests share in-flight work. Existing entries remain readable, although
a newly selected bucket needs its own first fetch. No cache purge is required.

The fetch path now shares a fully decoded source image rather than encoding
PNG and decoding that intermediate PNG again. A request copies the image before
resizing or encoding so concurrent callers cannot mutate another caller's image. Each
response is encoded to PNG once, at the requested size. Original downloads
remain limited to 2 MiB and 2048 pixels per axis before normalization; raising
the response bound does not relax those source safeguards.
