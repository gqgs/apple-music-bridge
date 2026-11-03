import itertools

import pytest

from .evaluate import CASES, evaluate
from .network import B1, B2, LABELS, W1, W2, affine, classify, encode, forward


def test_exhaustive_binary_priority():
    for present, single, compilation, live in itertools.product((0, 1), repeat=4):
        expected = (
            "Others"
            if not present
            else "Singles & EPs"
            if single
            else "Compilations"
            if compilation
            else "Live Albums"
            if live
            else "Albums"
        )
        scores = forward((present, single, compilation, live))
        assert scores == tuple(int(label == expected) for label in LABELS)


@pytest.mark.parametrize("title, expected", CASES)
def test_specification(title, expected):
    assert classify(title, "Fixture") == expected


def test_comparison():
    report = evaluate()
    assert report["agreement"] == report["cases"] == report["network_spec_pass"]


@pytest.mark.parametrize(
    "values",
    [
        (1,),
        (1, 0, 0, 0, 0),
        (1, -1, 0, 0),
        (1, 0.5, 0, 0),
        (1, float("nan"), 0, 0),
        (1, float("inf"), 0, 0),
    ],
)
def test_invalid_features(values):
    with pytest.raises(ValueError):
        forward(values)


def test_parameters_and_dense_math():
    assert sum(map(len, W1)) + len(B1) + sum(map(len, W2)) + len(B2) == 55
    assert affine(((2, -3),), (4,), (5, 2)) == (8,)
    with pytest.raises(ValueError):
        affine(((1,),), (0,), (1, 2))


def test_no_information_gain_for_unmarked_titles():
    # Synthetic editorial truth could differ, but identical input features cannot.
    assert encode("Unmarked studio record") == encode("Unmarked concert recording")
    assert classify("Unmarked studio record") == classify("Unmarked concert recording")


def test_artist_context_unicode_and_determinism():
    assert classify("Best of Björk", "Björk") == "Compilations"
    assert classify("Best of Björk", "Different artist") == "Albums"
    for _ in range(20):
        assert classify("  NIGHT — ep  ") == "Singles & EPs"
