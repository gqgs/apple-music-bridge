"""Isolated, directly programmed ReLU network. No optimizer or learned weights."""

import math
import re

LABELS = ("Albums", "Singles & EPs", "Live Albums", "Compilations", "Others")
FEATURES = ("title_present", "single_ep_hint", "compilation_hint", "live_hint")

# Written analytically before evaluation, not fitted to the fixtures.
# Rows compute priority gates on binary input (present, single, compilation, live).
# Hidden/output ordering is LABELS. All 55 parameters, including zeros, are explicit.
W1 = ((1, -1, -1, -1), (1, 1, 0, 0), (1, -1, -1, 1), (1, -1, 1, 0), (-1, 0, 0, 0))
B1 = (0, -1, -1, -1, 1)
W2 = ((1, 0, 0, 0, 0), (0, 1, 0, 0, 0), (0, 0, 1, 0, 0), (0, 0, 0, 1, 0), (0, 0, 0, 0, 1))
B2 = (0, 0, 0, 0, 0)


def affine(weights, bias, inputs):
    """Dense layer, implemented using only the Python standard library."""
    if len(weights) != len(bias) or any(len(row) != len(inputs) for row in weights):
        raise ValueError("Layer dimensions do not match")
    return tuple(
        sum(w * x for w, x in zip(row, inputs, strict=True)) + b
        for row, b in zip(weights, bias, strict=True)
    )


def forward(features):
    """Return decision scores, NOT calibrated probabilities or confidence."""
    if len(features) != 4 or any(not math.isfinite(x) or x not in (0, 1) for x in features):
        raise ValueError("Expected four binary features")
    hidden = tuple(max(0, x) for x in affine(W1, B1, features))
    return affine(W2, B2, hidden)


def encode(title, artist=""):
    """Explicit lexical adapter based on the existing heuristic's available metadata.

    These cues deliberately mirror production; the network only implements their
    precedence. It does not understand raw language or infer editorial taxonomy.
    """
    title = title.strip()
    single = re.search(
        r"(?:\s[-–—]\s*|[([][^)\]]*\b)(?:EP|Single|Sencill[oa]s?|Single digital)"
        r"(?:[)\]])?$",
        title,
        re.IGNORECASE,
    )
    compilation = (
        re.search(
            r"\b(?:greatest hits|compilation|compilações|compilación|coletânea)\b",
            title,
            re.IGNORECASE,
        )
        or re.fullmatch(r"(?:the )?(?:best of|anthology|antologia|antología)", title, re.IGNORECASE)
        or (
            artist
            and title.casefold()
            in {"best of " + artist.casefold(), "the best of " + artist.casefold()}
        )
    )
    live = re.search(
        r"(?:[([]|\s[-–—]\s*)(?:live|ao vivo|en vivo)(?:\b[^)\]]*)[)\]]?$"
        r"|^(?:live (?:at|in|from)|ao vivo|en vivo)\b",
        title,
        re.IGNORECASE,
    )
    return tuple(int(bool(value)) for value in (title, single, compilation, live))


def classify(title, artist=""):
    scores = forward(encode(title, artist))
    return LABELS[max(range(len(scores)), key=scores.__getitem__)]
