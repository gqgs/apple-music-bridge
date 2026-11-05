# Optional artist-search artwork seeding

An explicit Artists-only `search3` (`albumCount=0`, `songCount=0`, nonzero
artist count and nonempty query) enables one optional background album search.
The text request neither waits for this search nor inherits its failure. Other
providers and Supersonic are unchanged.

The server keeps one current query generation independently of completed search
flights. Query text (the catalog's first 200 characters), storefront and quality
form the key; offsets do not. Work starts only after successful artist results,
one second of query stability, and the existing foreground quiet period. A new
search supersedes the old generation; rapid typing replaces pending work.
At most one drain is queued in addition to the existing four artist jobs. Each
drain processes at most one generation, then yields to queued jobs. A bounded
128-key history suppresses repeated attempts for one hour, including optional
failures. A process restart clears this optional history.

Seeds and artist lookups share the existing single artwork executor and its
catalog instance. Foreground metadata uses its own catalog/cache and the same
three-second start limiter and 429 cooldown. No upstream concurrency is added.
The seed waits at most 30 seconds for admission; a network operation already
admitted retains the catalog's 15-second timeout. Long cooldowns defer the seed
without repeated requests. All uncovered artists remain eligible for their
normal lookup and existing retry protocol; a seed never creates a negative
artist result.

Pending/busy cards retain the usual three-second retry initially. Once a seed
has occupied the worker for six seconds, responses advertise its remaining
admission-plus-network bound (at most 45 seconds, or the remaining 15-second
network timeout after admission). This keeps the existing provider/UI retry
budget from being exhausted by a slow optional seed. Fast seeds still resolve
on the normal short retries. A deterministic actual-HTTP adversarial test holds
the seed for 29 seconds before admission plus 15 seconds in flight, fills four
artist slots, and verifies uncovered recovery with zero/partial seed coverage
using the existing Go provider's [1,2,4] waits and three UI deferred attempts.
This is a model of that retry policy, not execution of the Go UI.

Seed metadata follows the existing bounded album retention and selection rules.
Only exact primary artist IDs in the same storefront/quality can match. Search
records never create collaborator lookup associations. Invalid/future artwork
cannot become the selected cover, and newer valid known releases can replace
older selections. Each queued artist job rechecks cached selection after
foreground, quiet-period and limiter waits, immediately before attempting its
catalog lookup, allowing matching seeds to retire redundant work.

Cancellation covers superseded server search queries and server shutdown. There
is no explicit Supersonic navigation cancellation message. An already admitted
network call cannot be interrupted; its stale result is rolled back before
publication. Database writer admission and retention do not hold the query lock;
only the final generation check and commit do. The single bounded executor may
remain occupied until an in-flight network call finishes.

## Offline comparison

`tests/test_artwork_seed_benchmark.py` sends actual HTTP requests through the
bridge with synthetic public-catalog JSON and CDN image bytes. It exercises the
real catalog parser, shared limiter, SQLite retention and image decoding. There
are no live Apple requests or coverage measurements.

The full matrix uses a controllable clock: K=0/1/8/16/32 exact matches among 32
artists, with zero or two seconds of modeled latency per upstream request.
The modeled client requests cards in display order, waits for worker completion
after a pending response, then immediately retries. This is **not** Supersonic's
parallel scheduling or Retry-After behavior. Seed matches occupy the first K
cards. Times below are virtual seconds from the artist text request, not measured
end-to-end application latency. Catalog request totals include the artist text
search. One shared synthetic CDN URL supplies the fixture image; these
comparisons isolate catalog scheduling, not real CDN throughput or cache hit rates.

| K | Baseline requests | Seed requests | Seed first / median / last, zero latency | Seed first / median / last, 2s latency |
|---|---:|---:|---|---|
| 0 | 33 | 34 | 6 / 52.5 / 99 | 8 / 54.5 / 101 |
| 1 | 33 | 33 | 3 / 49.5 / 96 | 5 / 51.5 / 98 |
| 8 | 33 | 26 | 3 / 28.5 / 75 | 5 / 30.5 / 77 |
| 16 | 33 | 18 | 3 / 4.5 / 51 | 5 / 6.5 / 53 |
| 32 | 33 | 2 | 3 / 3 / 3 | 5 / 5 / 5 |

Baseline first/median/last: 3/49.5/96 with zero latency, 5/51.5/98
with two seconds upstream latency. Zero coverage costs one request and one
three-second slot; one match breaks even on requests; larger coverage reduces
requests. No claim is made about real query coverage or guaranteed speedup.
Actual HTTP text processing in the virtual matrix was roughly 6–15 ms; those
measurements exclude the modeled upstream delay and are not real-network times.

Representative wall-clock cases retain the production limiter and use four
artists plus 50 ms synthetic upstream delay. Recorded results:

| Case | Requests | Text (measured ms) | First / median / last cover (measured s) |
|---|---:|---:|---|
| Baseline | 5 | 60.03 | 3.07 / 7.57 / 12.07 |
| Seed K=0 | 6 | 56.32 | 6.07 / 10.57 / 15.07 |
| Seed K=4 | 2 | 56.00 | 3.07 / 3.07 / 3.08 |

These remain synthetic HTTP/client experiments, not a Supersonic runtime test.
Cold catalog spacing remains unchanged and no live coverage was established.
