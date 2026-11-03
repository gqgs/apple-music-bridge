import pytest
from apple_music_player.catalog import CatalogDeferred
from apple_music_player.model import Album, PlayerError

from .benchmark import run
from .live_compare import collect


def test_live_adapter_spacing_country_and_no_truth_labels():
    class Fake:
        calls = 0

        def artist_albums(self, identifier, country):
            self.calls += 1
            assert identifier == "295015" and country == "br"
            if self.calls == 1:
                raise CatalogDeferred(3)
            return [Album(country, "1", "Example", artist="Fixture", album_title="Example")]

    sleeps = []
    catalog = Fake()
    report = collect(["295015", "295015"], "br", catalog, sleeps.append)
    assert sleeps == [3]
    assert catalog.calls == 2
    assert len(report["rows"]) == 1
    assert "observed_sections" not in report["rows"][0]


@pytest.mark.parametrize("delay", [3, 3600, float("inf")])
def test_live_retry_budget(delay):
    class Fake:
        calls = 0

        def artist_albums(self, *args):
            self.calls += 1
            raise CatalogDeferred(delay, rate_limited=True)

    fake = Fake()
    sleeps = []
    with pytest.raises(PlayerError):
        collect(["295015"], "br", fake, sleeps.append)
    assert fake.calls <= 4
    assert sum(sleeps) <= 30
    if delay > 30:
        assert not sleeps  # Never shorten Retry-After and retry early.


@pytest.mark.parametrize(
    "ids, country", [([], "br"), (["1"] * 11, "br"), (["../../x"], "br"), (["1"], "invalid")]
)
def test_live_rejects_bad_arguments(ids, country):
    with pytest.raises(ValueError):
        collect(ids, country)


def test_microbenchmark_smoke_not_timing_thresholds():
    report = run(repeats=1, loops=1)
    assert [batch["size"] for batch in report["batches"]] == [10, 50, 200]
    for batch in report["batches"]:
        assert set(batch["timings"]) == {"candidate", "heuristic"}
        assert all(value["median_ms_per_batch"] > 0 for value in batch["timings"].values())
