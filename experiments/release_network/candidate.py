"""Independent, one-pass direct-weight candidate; freeze before collecting labels.

Broad lexical evidence is combined numerically, not production-rule precedence.
No classifier calls, title/artist identity lookup, fitting or optimizer.
"""

import math
import re
import unicodedata

LABELS = ("Albums", "Singles & EPs", "Live Albums", "Compilations", "Others")
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

# 10 -> 6 ReLU -> 5 linear. All 101 scalar parameters are specified here.
# Hidden evidence channels: present, short release, live, compilation, remix, missing.
W1 = (
    (1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 3, 4, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 3, 2, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 4, 3, 3, 4, 0),
    (0, 0, 0, 0, 0, 0, 0, 0, 0, 2),
    (-1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
)
B1 = (0, 0, 0, 0, 0, 1)
W2 = (
    (0.5, -0.25, -0.25, -0.25, -0.25, -4),
    (0, 1, 0, 0, 0, 0),
    (0, -0.5, 1, 0, 0, 0),
    (0, -0.5, 0, 1, 0, 0),
    (0, 0, 0, 0, 1, 4),
)
B2 = (1, 0, 0, 0, 0)


def encode(title):
    # Deliberately broader than the production suffix-only checks; false positives
    # are a hypothesis to measure, not filtered using evaluation labels.
    normalized = unicodedata.normalize("NFKC", title).casefold()
    words = re.findall(r"[^\W_]+", normalized)
    tokens = set(words)
    phrases = set(zip(words, words[1:]))
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
