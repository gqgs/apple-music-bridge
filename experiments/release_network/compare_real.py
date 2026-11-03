"""Evaluate frozen weights against public page membership, offline and without fitting."""

import hashlib
import json
from pathlib import Path

from apple_music_player.model import Album

from . import candidate
from .baseline import release_group
from .evaluate import CASES

ROOT = Path(__file__).parent
FROZEN_SHA256 = "f8d15e7917e74dc76fd05732331f0becacfc92a297ca1aeb68468f697ce57f14"


def production(title, artist):
    # Synthetic object identity only; never represents a fetched Apple collection ID.
    return release_group(Album("us", "fixture", title, artist=artist, album_title=title))


def summarize(rows, model):
    eligible = [row for row in rows if len(row["observed_sections"]) == 1]
    confusion = {label: dict.fromkeys(candidate.LABELS, 0) for label in candidate.LABELS}
    by_artist = {}
    for row in eligible:
        label = row["observed_sections"][0]
        confusion[label][row[model]] += 1
        counts = by_artist.setdefault(row["artist"], {"matched": 0, "total": 0})
        counts["total"] += 1
        counts["matched"] += int(row[model] == label)
    return {
        "matched": sum(row[model] == row["observed_sections"][0] for row in eligible),
        "total": len(eligible),
        "by_artist": by_artist,
        "confusion_actual_rows_predicted_columns": confusion,
    }


def evaluate():
    digest = hashlib.sha256((ROOT / "candidate.py").read_bytes()).hexdigest()
    if digest != FROZEN_SHA256:
        raise ValueError("Candidate changed after pre-evaluation freeze")
    dataset = json.loads((ROOT / "real_releases.json").read_text())
    rows = []
    for release in dataset["rows"]:
        title = release["title"]
        vector = candidate.encode(title)
        rows.append(
            {
                **release,
                "features": dict(zip(candidate.FEATURES, vector, strict=True)),
                "scores": dict(zip(candidate.LABELS, candidate.forward(vector), strict=True)),
                "candidate": candidate.classify(title),
                "heuristic": production(title, release["artist"]),
            }
        )
    synthetic = [
        {
            "title": title,
            "specification": label,
            "candidate": candidate.classify(title),
            "heuristic": production(title, "Fixture"),
        }
        for title, label in CASES
    ]
    return {
        "candidate_sha256": digest,
        "meaning": "Agreement with observed US Apple Music page sections, not catalog accuracy",
        "candidate": summarize(rows, "candidate"),
        "heuristic": summarize(rows, "heuristic"),
        "excluded_overlapping_sections": [r for r in rows if len(r["observed_sections"]) != 1],
        "disagreements": [r for r in rows if r["candidate"] != r["heuristic"]],
        "rows": rows,
        "synthetic_separate": {
            "total": len(synthetic),
            "candidate_matched": sum(r["candidate"] == r["specification"] for r in synthetic),
            "heuristic_matched": sum(r["heuristic"] == r["specification"] for r in synthetic),
            "failures": [r for r in synthetic if r["candidate"] != r["specification"]],
        },
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
