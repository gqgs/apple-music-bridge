import itertools
import json
import math

import pytest

from . import candidate
from .compare_real import ROOT, evaluate


def test_all_binary_vectors_are_finite_and_deterministic():
    for vector in itertools.product((0, 1), repeat=10):
        scores = candidate.forward(vector)
        assert len(scores) == 5
        assert all(math.isfinite(score) for score in scores)
        assert scores == candidate.forward(vector)


def test_explicit_dimensions_and_parameter_count():
    assert len(candidate.W1) == len(candidate.B1) == 6
    assert all(len(row) == 10 for row in candidate.W1)
    assert len(candidate.W2) == len(candidate.B2) == 5
    assert all(len(row) == 6 for row in candidate.W2)
    assert (
        sum(map(len, candidate.W1))
        + len(candidate.B1)
        + sum(map(len, candidate.W2))
        + len(candidate.B2)
        == 101
    )


@pytest.mark.parametrize(
    "vector", [(1,), (0,) * 11, (0.5,) * 10, (-1,) * 10, (float("nan"),) * 10, (float("inf"),) * 10]
)
def test_reject_invalid_vectors(vector):
    with pytest.raises(ValueError):
        candidate.forward(vector)


def test_normalization_and_boundaries():
    assert candidate.encode("ＬＩＶＥ") == candidate.encode("live")
    assert candidate.encode("Olive")[3] == 0
    assert candidate.encode("Live Forever")[3] == 1  # Broad cue, known false positive.
    assert candidate.encode("AO VIVO")[4] == 1
    assert candidate.encode("greatest hits")[5] == 1
    assert candidate.encode("hits greatest")[5] == 0


def test_real_report_integrity_without_requiring_candidate_to_win():
    report = evaluate()  # Also checks frozen file hash.
    assert report == json.loads((ROOT / "results.json").read_text())
    assert report["candidate"]["total"] == report["heuristic"]["total"] == 35
    assert len(report["rows"]) == 36
    assert len(report["excluded_overlapping_sections"]) == 1
    for model in ("candidate", "heuristic"):
        confusion = report[model]["confusion_actual_rows_predicted_columns"]
        assert sum(sum(row.values()) for row in confusion.values()) == 35
        assert (
            sum(confusion[label][label] for label in candidate.LABELS) == report[model]["matched"]
        )
    assert len({(r["artist"], r["title"]) for r in report["rows"]}) == 36
    assert all(
        r["source_url"].startswith("https://music.apple.com/us/artist/") for r in report["rows"]
    )
    assert report["synthetic_separate"]["total"] == 25
