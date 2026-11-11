# Lyrics

Supersonic's Now Playing lyrics tab can use the bridge's OpenSubsonic
`songLyrics` extension (`getLyricsBySongId`). Update and restart the bridge;
no Supersonic rebuild or new login is needed. Reconnect the server/restart the
client if it cached extension discovery or an empty lyrics result.

The bridge uses the existing managed runtime's private developer and music-user
session tokens to request Apple's catalog lyrics over **certificate- and
hostname-verified HTTPS**. It does not call the pinned wrapper's lyrics endpoint,
which disables verification. This does not fix other existing wrapper requests.
The fixed Apple origin is not configurable, redirects are rejected, environment
proxies are not used, and tokens/upstream errors are never logged or returned.
No session is copied or persisted by this feature.

Apple's unofficial syllable-lyrics response is reduced to line-level timestamps
in milliseconds. Apple's absolute song times (including bare decimal seconds)
are preserved rather than adding section and line starts together. Lyric/timing
collection excludes head metadata. TTML media-clock/offset timing and untimed text are supported;
unsupported timing falls back to plain lines rather than fabricated timestamps.
Word-level karaoke, translations/romanizations, and language selection are not
implemented. The legacy title/artist `getLyrics` endpoint remains an empty
compatibility response; exact song IDs avoid ambiguous title matches.

Coverage depends on the account, storefront and track. Missing lyrics, expired
sessions, unavailable service, TLS failure or changed response format return an
empty list so Supersonic can use its existing LRCLIB fallback if enabled.
The provider keeps at most 64 songs in memory (one hour for results, five minutes
for misses), coalesces concurrent requests, and applies a short failure cooldown.
It never downloads audio for a lyrics request or stores lyrics on disk.

Token files must be owned, private regular files in the existing runtime without
symlinks. No automatic login, token refresh or permission changes occur. A bridge
started without a managed runtime still returns empty lyrics. If playback works
but lyrics do not, confirm the client reconnected and the selected song has
lyrics in Apple Music; no need to delete the media cache.

Tests use synthetic lyrics and mock Apple responses. Live account coverage must
be checked on the user's signed-in installation. Displaying lyrics in a public
broadcast is a separate rights question from private playback.

## Timing regression checks

The Apple timing dialect follows the [Apple Video and Audio Asset Guide](https://help.apple.com/itc/videoaudioassetguide/en.lproj/static.html)
(Timed Lyrics). The generic TTML parser keeps relative container timing separately.
Synthetic fixtures cover decimal seconds, absolute section/line times, span-only
starts, and metadata exclusion. Public Apple-format fixtures in
[AMLL](https://github.com/Steve-xmh/applemusic-like-lyrics/tree/9db9236818464ce83f696ee9c5ff0e5395cb744a/packages/ttml/tests/fixtures)
were checked locally without copying song lyrics into this repository.
