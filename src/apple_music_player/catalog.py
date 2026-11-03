"""Documented public iTunes metadata. No audio previews or account credentials."""

import json
import re
import time
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from .model import Album, Artist, PlayerError, Track
from .release_classifier import classify as classify_release


class CatalogDeferred(Exception):
    def __init__(self, seconds, rate_limited=False):
        self.seconds = seconds
        self.rate_limited = rate_limited


class SearchResults(list):
    def __init__(self, values, diagnostics):
        super().__init__(values)
        self.diagnostics = diagnostics


class ArtistAlbums(list):
    def __init__(self, values, diagnostics):
        super().__init__(values)
        self.diagnostics = diagnostics


def public_text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:limit]


def public_result(row):
    # Strict public-metadata allowlist; never retain arbitrary response fields/URLs.
    if not isinstance(row, dict):
        return {"type": "invalid row"}
    artist = row.get("wrapperType") == "artist"
    identifier = str(
        row.get("artistId", "") if artist else row.get("trackId", row.get("collectionId", ""))
    )
    return {
        "id": identifier if re.fullmatch(r"[0-9]{1,30}", identifier) else "",
        "title": public_text(
            row.get("artistName", "")
            if artist
            else row.get("trackName", row.get("collectionName", ""))
        ),
        "artist": public_text(row.get("artistName", "")),
        "wrapper_type": public_text(row.get("wrapperType", ""), 40),
        "kind": public_text(row.get("kind", row.get("collectionType", "")), 40),
    }


class Catalog:
    def __init__(self):
        self.last_request = 0.0
        self.responses = {}
        self.response_times = {}
        self.not_before = 0.0
        self.last_response_source = None

    def request(self, endpoint, **params):
        key = endpoint + "?" + urlencode(params)
        if key in self.responses and time.monotonic() - self.response_times.get(key, 0) < 3600:
            self.last_response_source = (key, "memory cache")
            return self.responses[key]
        delay = max(self.last_request + 3, self.not_before) - time.monotonic()
        if delay > 0:
            raise CatalogDeferred(delay)
        self.last_request = time.monotonic()
        try:
            with urlopen("https://itunes.apple.com/" + key, timeout=15) as response:
                payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise ValueError()
            data = json.loads(payload)["results"]
            if not isinstance(data, list):
                raise ValueError()
        except HTTPError as exc:
            if exc.code == 429:
                value = (exc.headers.get("Retry-After", "") or "")[:128]
                delay = 3.0
                try:
                    delay = (
                        float(value)
                        if value.isdigit()
                        else (parsedate_to_datetime(value).timestamp() - time.time())
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
                delay = max(3.0, delay)
                self.not_before = time.monotonic() + delay
                exc.close()
                raise CatalogDeferred(delay, rate_limited=True) from None
            exc.close()
            raise PlayerError("Catalog request failed. Try again later.") from None
        except Exception:
            raise PlayerError(
                "Catalog unavailable. Check your network/storefront and try again."
            ) from None
        if len(self.responses) >= 100:
            self.responses.clear()
            self.response_times.clear()
        self.responses[key] = data
        self.response_times[key] = time.monotonic()
        self.last_response_source = (key, "network")
        return data

    def tracks(self, data, country):
        result = []
        for row in data:
            if (
                not isinstance(row, dict)
                or row.get("kind") != "song"
                or not str(row.get("trackId", "")).isdigit()
            ):
                continue
            title = (
                public_text(row.get("trackName", "Untitled"))
                + " — "
                + public_text(row.get("artistName", ""))
            )
            result.append(
                Track(
                    country,
                    str(row["trackId"]),
                    title[:300],
                    str(row.get("collectionId", "")),
                    track_name=str(row.get("trackName", "Untitled"))[:300],
                    album_artist=str(row.get("_album_artist") or "")[:300],
                    album_artist_id=str(row.get("_album_artist_id") or "")[:30],
                    track_number=self.positive_number(row.get("trackNumber")),
                    disc_number=self.positive_number(row.get("discNumber")),
                    duration_ms=self.positive_number(row.get("trackTimeMillis")),
                    **self.metadata(row),
                )
            )
        return result

    @staticmethod
    def positive_number(value):
        return value if type(value) is int and value > 0 else 0

    def albums(self, data, country):
        return [
            Album(
                country,
                str(row["collectionId"]),
                (
                    public_text(row.get("collectionName", "Untitled"))
                    + " — "
                    + public_text(row.get("artistName", ""))
                )[:300],
                **self.metadata(row),
            )
            for row in data
            if isinstance(row, dict)
            and row.get("wrapperType") == "collection"
            and row.get("collectionType") == "Album"
            and re.fullmatch(r"[0-9]+", str(row.get("collectionId", "")))
        ]

    @staticmethod
    def metadata(row):
        return {
            field: str(row.get(key, ""))[:limit]
            for field, key, limit in (
                ("artist", "artistName", 300),
                ("artist_id", "artistId", 30),
                ("album_title", "collectionName", 300),
                ("artwork_url", "artworkUrl100", 2048),
                ("release_date", "releaseDate", 40),
                ("store_url", "collectionViewUrl", 2048),
            )
        }

    def artists(self, rows, country):
        return [
            Artist(
                country,
                str(row["artistId"]),
                public_text(row["artistName"]),
                public_text(row.get("primaryGenreName", "")),
                public_text(row.get("artistLinkUrl") or row.get("artistViewUrl") or "", 2048),
            )
            for row in rows
            if isinstance(row, dict)
            and row.get("wrapperType") == "artist"
            and re.fullmatch(r"[0-9]+", str(row.get("artistId", "")))
            and public_text(row.get("artistName", ""))
        ]

    def artist_albums(self, artist_id, country):
        if not re.fullmatch(r"[0-9]+", artist_id) or not re.fullmatch(r"[a-z]{2}", country):
            return []
        params = dict(id=artist_id, country=country, entity="album", limit=200)
        rows = self.request("lookup", **params)
        albums = self.albums(rows, country)
        diagnostics = self.diagnostics("lookup", params, rows, albums)
        diagnostics["artist_id"] = artist_id
        return ArtistAlbums(albums, diagnostics)

    def search(self, query, country, entity="song", *, limit=None):
        if not re.fullmatch("[a-z]{2}", country):
            raise PlayerError("Storefront must be a two-letter country code, e.g. us.")
        if entity not in {"song", "album", "musicArtist"}:
            raise PlayerError("Choose Songs, Albums or Artists.")
        if limit is None:
            limit = 200 if entity == "album" else 50
        if type(limit) is not int or not 1 <= limit <= 200:
            raise PlayerError("Catalog search limit must be between 1 and 200.")
        parse = {"album": self.albums, "song": self.tracks, "musicArtist": self.artists}[entity]
        params = dict(
            term=query[:200],
            country=country,
            media="music",
            entity=entity,
            limit=limit,
        )
        rows = self.request("search", **params)
        accepted = parse(rows, country)
        return SearchResults(accepted, self.diagnostics("search", params, rows, accepted))

    def diagnostics(self, endpoint, params, rows, accepted):
        key = endpoint + "?" + urlencode(params)
        source = self.last_response_source
        return {
            "endpoint": "https://itunes.apple.com/" + endpoint,
            "query": public_text(params.get("term", ""), 200),
            "storefront": params["country"],
            "media": params.get("media", ""),
            "entity": params["entity"],
            "limit": params["limit"],
            "source": source[1] if source and source[0] == key else "adapter (source unavailable)",
            "raw_result_count": len(rows),
            "accepted_result_count": len(accepted),
            "details_truncated": len(rows) > 200 or len(accepted) > 200,
            "raw_public_results": [public_result(row) for row in rows[:200]],
            "accepted_results": [
                {"id": item.id, "title": public_text(item.title), "storefront": item.country}
                for item in accepted[:200]
            ],
        }

    def album(self, album_id, country):
        rows = self.request("lookup", id=album_id, country=country, entity="song", limit=200)
        collection = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("wrapperType") == "collection"
                and str(row.get("collectionId", "")) == album_id
            ),
            {},
        )
        songs = [
            {
                **collection,
                **row,
                "_album_artist": collection.get("artistName", "") or row.get("artistName", ""),
                "_album_artist_id": (
                    collection.get("artistId", "") if collection.get("artistName") else ""
                ),
            }
            for row in rows
            if isinstance(row, dict)
            and row.get("kind") == "song"
            and str(row.get("collectionId", "")) == album_id
        ]

        # Prefer explicit disc/track ordering; preserve the API array if numbering is absent.
        def number(row, key):
            value = row.get(key)
            return value if isinstance(value, int) and value > 0 else 0

        if songs and all(number(row, "trackNumber") for row in songs):
            songs.sort(key=lambda row: (number(row, "discNumber") or 1, number(row, "trackNumber")))
        return self.tracks(songs, country)


def recent_releases(albums, today=None):
    today = today or date.today()
    found = {}
    for album in albums:
        try:
            released = date.fromisoformat(album.release_date[:10])
        except ValueError:
            continue
        if today - timedelta(days=90) <= released <= today:
            found[(album.country, album.id)] = album
    return sorted(found.values(), key=lambda a: a.release_date, reverse=True)[:30]


def release_group(album):
    """Approximate release category from directly programmed neural weights."""
    return classify_release(album.album_title)


def grouped_releases(albums):
    def order(album):
        try:
            released = date.fromisoformat(album.release_date[:10]).toordinal()
        except ValueError:
            released = 0
        return (-released, album.album_title.casefold(), album.country, album.id)

    groups = {
        name: [] for name in ("Albums", "Singles & EPs", "Live Albums", "Compilations", "Others")
    }
    for album in albums:
        groups[release_group(album)].append(album)
    return [(group, sorted(values, key=order)) for group, values in groups.items() if values]
