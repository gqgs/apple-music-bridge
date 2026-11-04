from apple_music_player.catalog import grouped_releases, release_group
from apple_music_player.model import Album


def album(id, title, date=""):
    return Album("br", id, title + " — Fixture", "Fixture", "301", title, release_date=date)


def test_release_classification_dates_and_deterministic_ties():
    releases = [
        album("1", "Old album", "2010-01-01"),
        album("2", "New album", "2026-01-01"),
        album("3", "No date"),
        album("4", "Short – eP", "2020-01-01"),
        album("5", "Shorter — EP", "2025-01-01"),
        album("6", "Song - Single", "2026-02-01"),
        album("7", ""),
        album("8", "Tie", "2024-01-01"),
        album("9", "Tie", "2024-01-01"),
        album("10", "Invalid date", "no-date"),
    ]
    groups = grouped_releases(releases)
    assert [(name, [a.id for a in values]) for name, values in groups] == [
        ("Albums", ["2", "8", "9", "1", "10", "3"]),
        ("Singles & EPs", ["6", "5", "4"]),
        ("Others", ["7"]),
    ]
    assert grouped_releases(list(reversed(releases))) == groups
    assert release_group(album("11", "EP Stories")) == "Singles & EPs"
    assert release_group(album("12", "Série - EP")) == "Singles & EPs"
    assert release_group(album("13", "Édition - Single")) == "Singles & EPs"
    # No unverified localization / track-count heuristics.
    assert release_group(album("14", "Title - Sencillo")) == "Singles & EPs"
    assert grouped_releases([]) == []


def test_bjork_title_hints_and_ambiguous_names():
    # Names verified on Apple's public Björk artist page; these expectations test
    # our documented heuristic, not a claim of matching Apple's editorial categories.
    expected = {
        "Human Behaviour (UK Remixes) - EP": "Singles & EPs",
        "Big Time Sensuality (UK CD1 Single)": "Singles & EPs",
        "Vulnicura (Live)": "Live Albums",
        "Björk's Greatest Hits": "Compilations",
        "Fossora": "Albums",
        "Bastards": "Albums",  # No title hint: approximate Albums fallback.
        "Cornucopia Live": "Live Albums",
    }
    for title, group in expected.items():
        assert release_group(album("1", title)) == group
    # Broad lexical cues can be false positives; these document the frozen network,
    # not asserted editorial truth for arbitrary titles.
    for title, group in {
        "Olive": "Albums",
        "Live Forever": "Live Albums",
        "I Live": "Live Albums",
        "The Best of Me": "Compilations",
        "Single Minded": "Singles & EPs",
        "EP Stories": "Singles & EPs",
        "Record Remixes": "Others",
    }.items():
        assert release_group(album("1", title)) == group
    for title in ("Show (Ao Vivo)", "Show - En Vivo", "Live at the Hall"):
        assert release_group(album("1", title)) == "Live Albums"
    assert release_group(album("1", "Show (Live) - Single")) == "Singles & EPs"
    assert release_group(album("1", "Greatest Hits (Live)")) == "Compilations"
    assert release_group(album("1", "Coletânea")) == "Compilations"
    assert release_group(album("1", "Record (Studio Album)")) == "Albums"
    groups = grouped_releases(
        [
            album("1", ""),
            album("2", "Coletânea"),
            album("3", "Show (Live)"),
            album("4", "Short - EP"),
            album("5", "Record (Studio Album)"),
        ]
    )
    assert [group for group, _ in groups] == [
        "Albums",
        "Singles & EPs",
        "Live Albums",
        "Compilations",
        "Others",
    ]


def test_release_year_uses_only_valid_returned_date():
    assert album("1", "Record", "1997-09-22T00:00:00Z").release_title == "Record (1997)"
    for invalid in ("", "unknown", "2024", "2024-02-30"):
        release = album("1", "Record", invalid)
        assert release.release_year == ""
        assert release.release_title == "Record"
    original = album("1", "Record", "1997-09-22")
    assert original.album_title == "Record" and original.title == "Record — Fixture"
