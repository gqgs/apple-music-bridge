"""Offline synthetic comparison, not a real-world accuracy benchmark."""

import json

from apple_music_player.model import Album

from .baseline import release_group
from .network import LABELS, classify, encode, forward

# Specification checks, not training examples or independent editorial labels.
CASES = (
    ("Ordinary Record", "Albums"),
    ("Ordinary Record - LP", "Albums"),
    ("", "Others"),
    ("  ", "Others"),
    ("Night - Single", "Singles & EPs"),
    ("Night (UK CD1 Single)", "Singles & EPs"),
    ("Night — EP", "Singles & EPs"),
    ("Night - Sencillo", "Singles & EPs"),
    ("Night - Single digital", "Singles & EPs"),
    ("Night (Ao Vivo)", "Live Albums"),
    ("Night [Live at Home]", "Live Albums"),
    ("Live from Home", "Live Albums"),
    ("En Vivo en Casa", "Live Albums"),
    ("Night (Live) - EP", "Singles & EPs"),
    ("Greatest Hits (Live) - Single", "Singles & EPs"),
    ("Greatest Hits (Live)", "Compilations"),
    ("Coletânea", "Compilations"),
    ("Antología", "Compilations"),
    ("Best of Fixture", "Compilations"),
    ("The Best of Me", "Albums"),
    ("Olive", "Albums"),
    ("Live Forever", "Albums"),
    ("Single Minded", "Albums"),
    ("EP Stories", "Albums"),
    ("Untitled Live", "Albums"),
)


def evaluate():
    rows = []
    for title, expected in CASES:
        album = Album("br", "1", title, artist="Fixture", album_title=title)
        rows.append(
            {
                "title": title,
                "specification": expected,
                "heuristic": release_group(album),
                "network": classify(title, "Fixture"),
                "features": encode(title, "Fixture"),
                "scores": dict(zip(LABELS, forward(encode(title, "Fixture")), strict=True)),
            }
        )
    return {
        "kind": "synthetic specification checks; not accuracy on catalog data",
        "cases": len(rows),
        "agreement": sum(r["network"] == r["heuristic"] for r in rows),
        "network_spec_pass": sum(r["network"] == r["specification"] for r in rows),
        "heuristic_spec_pass": sum(r["heuristic"] == r["specification"] for r in rows),
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2, ensure_ascii=False))
