"""Explicit opt-in public catalog lookup; prints predictions, never inferred truth labels."""

import argparse
import json
import math
import re
import sys
import time

from apple_music_player.catalog import Catalog, CatalogDeferred, public_text, release_group
from apple_music_player.model import PlayerError

from . import candidate


def fetch(catalog, artist_id, country, sleep=time.sleep):
    remaining = 30.0
    for attempt in range(4):
        try:
            return catalog.artist_albums(artist_id, country)
        except CatalogDeferred as exc:
            delay = max(0.0, exc.seconds)
            if attempt == 3 or not math.isfinite(delay) or delay > remaining:
                raise PlayerError("Catalog wait budget exceeded; try again later.") from None
            print("Waiting for catalog request spacing or rate limit…", file=sys.stderr)
            sleep(delay)
            remaining -= delay
    raise AssertionError("Unreachable")


def collect(artist_ids, country, catalog=None, sleep=time.sleep):
    if not re.fullmatch(r"[a-z]{2}", country) or not 1 <= len(artist_ids) <= 10:
        raise ValueError("Use a two-letter country and one to ten numeric artist IDs")
    if any(not re.fullmatch(r"[0-9]{1,30}", identifier) for identifier in artist_ids):
        raise ValueError("Invalid artist ID")
    catalog = catalog or Catalog()
    rows = []
    for artist_id in dict.fromkeys(artist_ids):
        for album in fetch(catalog, artist_id, country, sleep):
            rows.append(
                {
                    "requested_artist_id": artist_id,
                    "country": country,
                    "collection_id": album.id,
                    "title": public_text(album.album_title),
                    "artist": public_text(album.artist),
                    "candidate": candidate.classify(album.album_title),
                    "heuristic": release_group(album),
                }
            )
    return {
        "meaning": "Live public metadata predictions only; no editorial labels or accuracy score",
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="br")
    parser.add_argument("--artist-id", action="append", required=True)
    args = parser.parse_args()
    try:
        result = collect(args.artist_id, args.country)
    except (PlayerError, ValueError):
        print(
            "Public catalog lookup failed or was rate-limited. Check IDs/network and retry later.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
