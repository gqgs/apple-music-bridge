"""Local bridge image diagnostic; no account/runtime startup or sensitive output."""

import argparse
import http.client
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlencode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--port", type=int, default=4533)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "apple-music-bridge",
    )
    parser.add_argument(
        "--check-identity",
        action="store_true",
        help="One public MusicBrainz lookup for a cached identity-missing result",
    )
    args = parser.parse_args()
    stage = "imports"
    try:
        from PySide6.QtGui import QImage

        from apple_music_player.bridge.artist_images import metadata as provider_metadata
        from apple_music_player.bridge.artist_images import resources, safe_reason, valid_id
        from apple_music_player.bridge.http import auth_file, placeholder
        from apple_music_player.model import Artist

        stage = "authentication_file"
        credentials = auth_file(args.auth_file)
        stage = "port"
        if not 1 <= args.port <= 65535:
            raise ValueError()

        def report(**values):
            print(json.dumps(values), flush=True)

        def call(endpoint, **params):
            nonlocal stage
            stage = endpoint
            conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=20)
            try:
                body = urlencode(
                    dict(apiKey=credentials["key"], v="1.16.1", c="image-probe", f="json", **params)
                )
                conn.request(
                    "POST",
                    "/rest/" + endpoint,
                    body,
                    {"Content-Type": "application/x-www-form-urlencoded"},
                )
                response = conn.getresponse()
                data = response.read(2 * 1024**2 + 1)
                if len(data) > 2 * 1024**2:
                    raise ValueError()
                return response.status, response.headers, data
            finally:
                conn.close()

        def metadata(endpoint, **params):
            status, _, body = call(endpoint, **params)
            data = json.loads(body)["subsonic-response"]
            ok = data.get("status") == "ok"
            report(stage=endpoint, http=status, ok=ok)
            return data if ok else {}

        def cover(kind, identifier):
            status, headers, body = call("getCoverArt", id=identifier, size="400")
            decoded = QImage.fromData(body)
            reason = headers.get("X-Artwork-Reason")
            artwork = headers.get("X-Artwork-Status")
            mime = headers.get_content_type()
            mime = (
                mime
                if mime
                in {
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                    "application/xml",
                    "application/json",
                    "application/octet-stream",
                }
                else "other"
            )
            report(
                stage=kind,
                http=status,
                mime=mime,
                bytes=len(body),
                png=body.startswith(b"\x89PNG\r\n\x1a\n"),
                decodes=not decoded.isNull(),
                width=decoded.width(),
                height=decoded.height(),
                bridge_placeholder=body == placeholder(),
                artwork_status=artwork
                if artwork in {"available", "placeholder", "unavailable"}
                else "unknown",
                reason=safe_reason(reason) if reason else "none",
                cache_header_count=len(headers.get_all("Cache-Control", [])),
                no_store=any(
                    "no-store" in value.lower() for value in headers.get_all("Cache-Control", [])
                ),
            )

        # Cache-only metadata endpoints: no new catalog search/artist expansion.
        albums = (
            metadata("getAlbumList2", type="alphabeticalByName", size="1")
            .get("albumList2", {})
            .get("album", [])
        )
        report(stage="album_candidates", count=len(albums))
        if albums and albums[0].get("coverArt"):
            cover("album_cover", albums[0]["coverArt"])
        data = metadata("getArtists")
        artists = [
            a for group in data.get("artists", {}).get("index", []) for a in group.get("artist", [])
        ]
        candidates = [a for a in artists if str(a.get("coverArt", "")).startswith("artist-image:")]
        report(stage="artist_candidates", count=len(artists), portrait_ids=len(candidates))
        if candidates:
            # Prefer the artist from the reported screen, without printing its name/ID.
            artist = next(
                (
                    a
                    for a in candidates
                    if re.sub(r"[^a-z]", "", a.get("name", "").lower()) == "agcook"
                ),
                candidates[0],
            )
            cover("artist_cover", artist["coverArt"])
            info = metadata("getArtistArtworkInfo", id=artist["id"]).get("artistArtworkInfo", {})
            state = info.get("status")
            report(
                stage="artist_resolution",
                status=state
                if state in {"disabled", "pending", "ready", "unavailable", "retry"}
                else "unknown",
                reason=safe_reason(info["reason"]) if "reason" in info else "none",
            )
            if args.check_identity:
                if info.get("reason") != "identity-missing":
                    report(stage="identity_check", status="skipped_not_identity_missing")
                else:
                    stage = "identity_cache"
                    uri = "file:" + quote(str(args.cache.absolute() / "index.sqlite3")) + "?mode=ro"
                    connection = sqlite3.connect(uri, uri=True, timeout=0.1)
                    try:
                        row = connection.execute(
                            "SELECT data FROM bridge_metadata WHERE id=?", (artist["id"],)
                        ).fetchone()
                    finally:
                        connection.close()
                    if not row:
                        report(stage=stage, status="missing_check_cache_path")
                        return 0
                    item = Artist(**json.loads(row[0]))
                    parts = artist["id"].split(":")
                    if len(parts) != 5 or (parts[1], parts[3], parts[4]) != (
                        item.country,
                        "artist",
                        item.id,
                    ):
                        raise ValueError()
                    urls = resources(item)
                    query = urlencode(
                        [
                            *(("resource", url) for url in urls),
                            ("inc", "artist-rels"),
                            ("fmt", "json"),
                        ]
                    )
                    stage = "identity_provider"
                    try:
                        data = provider_metadata(
                            "https://musicbrainz.org/ws/2/url?" + query, threading.Event()
                        )
                    except Exception as exc:
                        report(
                            stage=stage,
                            reason=safe_reason(getattr(exc, "reason", None)),
                            error_type=type(exc).__name__,
                        )
                        return 0
                    shape = (
                        "list"
                        if isinstance(data.get("urls"), list)
                        else "single"
                        if "resource" in data
                        else "unexpected"
                    )
                    rows = data["urls"] if shape == "list" else [data] if shape == "single" else []
                    matched = [r for r in rows if isinstance(r, dict) and r.get("resource") in urls]
                    relations = [
                        rel
                        for r in matched
                        for rel in r.get("relations", [])
                        if isinstance(rel, dict) and rel.get("target-type") == "artist"
                    ]
                    ids = {
                        r.get("artist", {}).get("id")
                        for r in relations
                        if isinstance(r.get("artist"), dict) and valid_id(r["artist"].get("id"))
                    }
                    report(
                        stage=stage,
                        shape=shape,
                        requested_urls=len(urls),
                        returned_urls=len(rows),
                        matching_urls=len(matched),
                        artist_relations=len(relations),
                        unique_artist_ids=len(ids),
                    )
    except Exception as exc:
        print(json.dumps({"stage": stage, "error_type": type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
