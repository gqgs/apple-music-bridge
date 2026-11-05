from collections import Counter
from importlib.util import module_from_spec, spec_from_file_location
from itertools import product
from pathlib import Path

import pytest

from apple_music_player import release_classifier as current

spec = spec_from_file_location(
    "historical_release_candidate",
    Path(__file__).resolve().parents[1] / "experiments/release_network/candidate.py",
)
historical = module_from_spec(spec)
spec.loader.exec_module(historical)


def test_all_binary_inputs_preserve_original_category_and_scores():
    counts = Counter()
    for vector in product((0, 1), repeat=10):
        old = historical.forward(vector)
        new = current.forward(vector)
        assert (new[0], max(new[1:3]), *new[3:]) == old
        old_label = historical.LABELS[max(range(5), key=old.__getitem__)]
        new_label = current.LABELS[max(range(6), key=new.__getitem__)]
        expected = old_label
        if old_label == "Singles & EPs":
            assert vector[1] or vector[2]
            expected = "EPs" if vector[2] else "Singles"
        assert new_label == expected
        counts[new_label] += 1
    assert sum(counts.values()) == 1024
    assert len(current.W1) == len(current.B1) == 7
    assert len(current.W2) == len(current.B2) == 6
    assert (
        sum(map(len, current.W1)) + len(current.B1) + sum(map(len, current.W2)) + len(current.B2)
        == 125
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Record - EP", "EPs"),
        ("Track - Single", "Singles"),
        ("Single EP", "EPs"),
        ("EP Single", "EPs"),
        ("ＳＩＮＧＬＥ ＥＰ", "EPs"),
        ("Track - Sencillo", "Singles"),
        ("Live Single", "Singles"),
        ("Live EP", "EPs"),
        ("Greatest Hits Live EP", "EPs"),
        ("", "Others"),
        ("EP Stories", "EPs"),
        ("Single Minded", "Singles"),
    ],
)
def test_title_encoding_and_ambiguous_precedence(title, expected):
    assert current.encode(title) == historical.encode(title)
    assert current.classify(title) == expected


@pytest.mark.parametrize(
    "vector", [(0,) * 9, (0,) * 11, (float("nan"),) + (0,) * 9, (2,) + (0,) * 9]
)
def test_invalid_features(vector):
    with pytest.raises(ValueError):
        current.forward(vector)


def test_recorded_titles_against_historical_candidate():
    import json

    rows = json.loads(
        (
            Path(__file__).resolve().parents[1] / "experiments/release_network/results.json"
        ).read_text()
    )["rows"]
    changes = Counter()
    for row in rows:
        title = row["title"]
        assert current.encode(title) == historical.encode(title)
        before, after = historical.classify(title), current.classify(title)
        assert before == row["candidate"]
        if before != "Singles & EPs":
            assert after == before
        else:
            assert after == ("EPs" if current.encode(title)[2] else "Singles")
        changes[before, after] += 1
    assert changes == {
        ("Albums", "Albums"): 13,
        ("Live Albums", "Live Albums"): 10,
        ("Singles & EPs", "EPs"): 1,
        ("Singles & EPs", "Singles"): 12,
    }


def test_both_markers_do_not_override_an_existing_compilation_winner():
    title = "Greatest Hits Best Of Anthology Compilation Single EP"
    assert historical.classify(title) == current.classify(title) == "Compilations"
