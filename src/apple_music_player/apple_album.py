"""Bounded, credential-free metadata from an exact Apple Music album page."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_PAGE_BYTES = 1024 * 1024


def album_page_url(url, country, identifier):
    """Only the requested public Apple album in the exact storefront is allowed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    path = parts.path.strip("/").split("/")
    return (
        parts.scheme == "https"
        and parts.netloc == "music.apple.com"
        and not parts.query
        and not parts.fragment
        and len(path) in {3, 4}
        and path[:2] == [country, "album"]
        and path[-1] == identifier
    )


class _AlbumRedirect(HTTPRedirectHandler):
    def __init__(self, country, identifier):
        self.country, self.identifier = country, identifier

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not album_page_url(newurl, self.country, self.identifier):
            raise ValueError("Unexpected album page redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ServerData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (
            tag == "script"
            and attrs.get("id") == "serialized-server-data"
            and attrs.get("type") == "application/json"
        ):
            self.active = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data


def server_data(page):
    if len(page) > MAX_PAGE_BYTES:
        return None
    parser = _ServerData()
    try:
        parser.feed(page.decode("utf-8"))
        return json.loads(parser.scripts[0]) if len(parser.scripts) == 1 else None
    except (ValueError, RecursionError):
        return None


def matching_nodes(data, identifier):
    pending = [data]
    matches = []
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("id") == identifier:
                matches.append(node)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return matches


def album_descriptor(node, country, identifier):
    descriptor = node.get("contentDescriptor")
    if not isinstance(descriptor, dict):
        return False
    identifiers = descriptor.get("identifiers")
    return (
        descriptor.get("kind") == "album"
        and isinstance(identifiers, dict)
        and identifiers.get("storeAdamID") == identifier
        and isinstance(descriptor.get("url"), str)
        and album_page_url(descriptor["url"], country, identifier)
    )


def fetch_album_page(country, identifier):
    if not re.fullmatch(r"[a-z]{2}", country) or not re.fullmatch(r"[0-9]{1,30}", identifier):
        raise ValueError("Invalid album identity")
    url = f"https://music.apple.com/{country}/album/{identifier}"
    # No cookies, account tokens, downloader, previews, or audio requests.
    request = Request(url, headers={"Accept": "text/html"})
    opener = build_opener(_AlbumRedirect(country, identifier))
    with opener.open(request, timeout=15) as response:
        if not album_page_url(response.geturl(), country, identifier):
            raise ValueError("Unexpected album page")
        if response.headers.get_content_type() != "text/html":
            raise ValueError("Unexpected album page type")
        return response.read(MAX_PAGE_BYTES + 1)


def track_artist_id(row, country):
    links = row.get("subtitleLinks")
    if not isinstance(links, list):
        return ""
    for link in links:
        if not isinstance(link, dict) or link.get("title") != row.get("artistName"):
            continue
        segue = link.get("segue")
        destination = segue.get("destination") if isinstance(segue, dict) else None
        descriptor = destination.get("contentDescriptor") if isinstance(destination, dict) else None
        identifiers = descriptor.get("identifiers") if isinstance(descriptor, dict) else None
        identifier = identifiers.get("storeAdamID") if isinstance(identifiers, dict) else None
        if (
            isinstance(descriptor, dict)
            and descriptor.get("kind") == "artist"
            and isinstance(identifier, str)
            and re.fullmatch(r"[0-9]{1,30}", identifier)
            and isinstance(descriptor.get("url"), str)
        ):
            try:
                parts = urlsplit(descriptor["url"])
            except ValueError:
                continue
            path = parts.path.strip("/").split("/")
            if (
                parts.scheme == "https"
                and parts.netloc == "music.apple.com"
                and not parts.query
                and not parts.fragment
                and len(path) in {3, 4}
                and path[:2] == [country, "artist"]
                and path[-1] == identifier
            ):
                return identifier
    return ""


def parse_album_tracks(page, country, identifier, expected_count):
    """Return complete, album-bound public song rows, never preview/audio URLs."""
    data = server_data(page)
    headers = matching_nodes(data, "album-detail-header - " + identifier)
    lists = matching_nodes(data, "track-list - " + identifier)
    if (
        len(headers) != 1
        or not album_descriptor(headers[0], country, identifier)
        or type(expected_count) is not int
        or not 1 <= expected_count <= 200
        or type(headers[0].get("trackCount")) is not int
        or headers[0]["trackCount"] != expected_count
        or len(lists) != 1
        or lists[0].get("itemKind") != "trackLockup"
    ):
        return None
    items = lists[0].get("items")
    if not isinstance(items, list) or len(items) != expected_count:
        return None
    result, seen, positions = [], set(), set()
    for row in items:
        if not isinstance(row, dict):
            return None
        descriptor = row.get("contentDescriptor")
        identifiers = descriptor.get("identifiers") if isinstance(descriptor, dict) else None
        song_id = identifiers.get("storeAdamID") if isinstance(identifiers, dict) else None
        if (
            not isinstance(song_id, str)
            or not re.fullmatch(r"[0-9]{1,30}", song_id)
            or song_id in seen
            or row.get("id") != f"track-lockup - {identifier} - {song_id}"
            or descriptor.get("kind") != "song"
            or not isinstance(descriptor.get("url"), str)
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
            or not isinstance(row.get("artistName"), str)
        ):
            return None
        try:
            parts = urlsplit(descriptor["url"])
        except ValueError:
            return None
        if not album_page_url(
            urlunsplit(parts._replace(query="")), country, identifier
        ) or parse_qs(parts.query) != {"i": [song_id]}:
            return None
        track, disc = row.get("trackNumber"), row.get("discNumber")
        if (
            type(track) is not int
            or track <= 0
            or type(disc) is not int
            or disc <= 0
            or (disc, track) in positions
        ):
            return None
        seen.add(song_id)
        positions.add((disc, track))
        result.append(
            {
                "kind": "song",
                "trackId": song_id,
                "collectionId": identifier,
                "trackName": row["title"],
                "artistName": row["artistName"],
                "artistId": track_artist_id(row, country),
                "trackNumber": track,
                "discNumber": disc,
                "trackTimeMillis": row.get("duration"),
            }
        )
    return result
