"""Map the existing approximate classifier to OpenSubsonic album metadata.

No Qt dependency. Empty means unavailable/unknown; Other is an explicit prediction.
"""

from ..release_classifier import classify


def release_types(title):
    if not title.strip():
        return []
    label = classify(title)
    value = {
        "Albums": "Album",
        "EPs": "EP",
        "Singles": "Single",
        "Live Albums": "Live",
        "Compilations": "Compilation",
        "Others": "Other",
    }.get(label)
    return [value] if value else []
