"""Archived pre-network production heuristic, for offline comparison only."""

import re


def release_group(album):
    """Conservative title hints, not Apple Music editorial release types."""
    title = album.album_title.strip()
    # An explicitly marked live single remains a single; no track-count guesses.
    if re.search(
        r"(?:\s[-–—]\s*|[([][^)\]]*\b)(?:EP|Single|Sencill[oa]s?|Single digital)"
        r"(?:[)\]])?$",
        title,
        re.IGNORECASE,
    ):
        return "Singles & EPs"
    if re.search(
        r"\b(?:greatest hits|compilation|compilações|compilación|coletânea)\b", title, re.IGNORECASE
    ):
        return "Compilations"
    if re.fullmatch(r"(?:the )?(?:best of|anthology|antologia|antología)", title, re.IGNORECASE):
        return "Compilations"
    if album.artist and title.casefold() in {
        "best of " + album.artist.casefold(),
        "the best of " + album.artist.casefold(),
    }:
        return "Compilations"
    if re.search(
        r"(?:[([]|\s[-–—]\s*)(?:live|ao vivo|en vivo)(?:\b[^)\]]*)[)\]]?$"
        r"|^(?:live (?:at|in|from)|ao vivo|en vivo)\b",
        title,
        re.IGNORECASE,
    ):
        return "Live Albums"
    if re.search(
        r"(?:\s[-–—]\s*|[([])(?:studio album|album|álbum|LP)[)\]]?$", title, re.IGNORECASE
    ):
        return "Albums"
    return "Albums" if title else "Others"
