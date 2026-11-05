"""Title-based direct-weight classifier, with separate EP and Single outputs.

Derived from the frozen historical candidate in experiments/release_network.
The original five-category evaluation does not measure this six-category revision.
No fitting, optimizer, track-count inference or authoritative editorial metadata.
"""

import math
import re
import unicodedata

LABELS = ("Albums", "EPs", "Singles", "Live Albums", "Compilations", "Others")
FEATURES = (
    "title_present",
    "single_word",
    "ep_word",
    "live_word",
    "performance_phrase",
    "greatest_hits_phrase",
    "best_of_phrase",
    "anthology_word",
    "compilation_word",
    "remix_word",
)

# 10 -> 7 ReLU -> 6 linear, 125 explicitly specified scalar parameters.
# Preserve the six historical channels; add the binary EP indicator.
# Short score h = 3*single + 4*ep. EP score = h + 3*ep - 3;
# Single score = h - 4*ep. Their maximum equals h for every binary input,
# so unrelated winners/ties are preserved. Both markers choose EP (7 versus 3).
W1 = (
    (1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 3, 4, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 3, 2, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 4, 3, 3, 4, 0),
    (0, 0, 0, 0, 0, 0, 0, 0, 0, 2),
    (-1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 1, 0, 0, 0, 0, 0, 0, 0),
)
B1 = (0, 0, 0, 0, 0, 1, 0)
W2 = (
    (0.5, -0.25, -0.25, -0.25, -0.25, -4, 0),
    (0, 1, 0, 0, 0, 0, 3),
    (0, 1, 0, 0, 0, 0, -4),
    (0, -0.5, 1, 0, 0, 0, 0),
    (0, -0.5, 0, 1, 0, 0, 0),
    (0, 0, 0, 0, 1, 4, 0),
)
B2 = (1, -3, 0, 0, 0, 0)


def encode(title):
    # Preserve historical broad lexical cues, including their known false positives.
    normalized = unicodedata.normalize("NFKC", title).casefold()
    words = re.findall(r"[^\W_]+", normalized)
    tokens = set(words)
    phrases = set(zip(words, words[1:], strict=False))
    values = (
        bool(title.strip()),
        bool(tokens & {"single", "sencillo", "sencilla"}),
        "ep" in tokens,
        "live" in tokens,
        bool(phrases & {("ao", "vivo"), ("en", "vivo"), ("in", "concert")}),
        ("greatest", "hits") in phrases,
        ("best", "of") in phrases,
        bool(tokens & {"anthology", "antologia", "antología"}),
        bool(tokens & {"compilation", "compilações", "compilación", "coletânea"}),
        bool(tokens & {"remix", "remixes", "remixed"}),
    )
    return tuple(int(value) for value in values)


def dense(weights, bias, vector):
    return tuple(
        sum(w * x for w, x in zip(row, vector, strict=True)) + b
        for row, b in zip(weights, bias, strict=True)
    )


def forward(vector):
    if len(vector) != 10 or any(not math.isfinite(x) or x not in (0, 1) for x in vector):
        raise ValueError("Expected ten binary metadata features")
    hidden = tuple(max(0, x) for x in dense(W1, B1, vector))
    return dense(W2, B2, hidden)


def classify(title):
    scores = forward(encode(title))
    return LABELS[max(range(len(scores)), key=scores.__getitem__)]
