"""Map the existing approximate classifier to OpenSubsonic album metadata.

No Qt dependency. Empty means unavailable/unknown; Other is an explicit prediction.
"""

from ..release_classifier import FEATURES, classify, encode


def release_types(title):
    if not title.strip():
        return []
    label = classify(title)
    if label == "Singles & EPs":
        # The network deliberately groups these together. Reuse its existing
        # lexical evidence to expose the subtype(s), without a new classifier.
        features = dict(zip(FEATURES, encode(title), strict=True))
        return [
            name
            for name, feature in (("EP", "ep_word"), ("Single", "single_word"))
            if features[feature]
        ]
    value = {
        "Albums": "Album",
        "Live Albums": "Live",
        "Compilations": "Compilation",
        "Others": "Other",
    }.get(label)
    return [value] if value else []
