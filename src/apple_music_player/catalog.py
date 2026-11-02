"""Documented public iTunes metadata. No previews, artwork or account credentials."""

import json
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from .model import Album, PlayerError, Track


class Catalog:
    def __init__(self):
        self.last_request = 0.0
        self.responses = {}

    def request(self, endpoint, **params):
        key = endpoint + "?" + urlencode(params)
        if key in self.responses:
            return self.responses[key]
        if time.monotonic() - self.last_request < 3:
            raise PlayerError("Please wait three seconds between catalog requests.")
        self.last_request = time.monotonic()
        try:
            with urlopen("https://itunes.apple.com/" + key, timeout=15) as response:
                payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise ValueError()
            data = json.loads(payload)["results"]
            if not isinstance(data, list):
                raise ValueError()
        except Exception:
            raise PlayerError(
                "Catalog unavailable. Check your network/storefront or paste a song URL."
            ) from None
        if len(self.responses) >= 100:
            self.responses.clear()
        self.responses[key] = data
        return data

    def tracks(self, data, country):
        result = []
        for row in data:
            if row.get("kind") != "song" or not str(row.get("trackId", "")).isdigit():
                continue
            title = f"{row.get('trackName', 'Untitled')} — {row.get('artistName', '')}"
            result.append(
                Track(country, str(row["trackId"]), title[:300], str(row.get("collectionId", "")))
            )
        return result

    def albums(self, data, country):
        return [
            Album(
                country,
                str(row["collectionId"]),
                f"{row.get('collectionName', 'Untitled')} — {row.get('artistName', '')}"[:300],
            )
            for row in data
            if row.get("wrapperType") == "collection"
            and row.get("collectionType") == "Album"
            and re.fullmatch(r"[0-9]+", str(row.get("collectionId", "")))
        ]

    def search(self, query, country, entity="song"):
        if not re.fullmatch("[a-z]{2}", country):
            raise PlayerError("Storefront must be a two-letter country code, e.g. us.")
        if entity not in {"song", "album"}:
            raise PlayerError("Choose Songs or Albums.")
        parse = self.albums if entity == "album" else self.tracks
        return parse(
            self.request(
                "search", term=query[:200], country=country, media="music", entity=entity, limit=50
            ),
            country,
        )

    def album(self, album_id, country):
        return self.tracks(
            self.request("lookup", id=album_id, country=country, entity="song", limit=200), country
        )
