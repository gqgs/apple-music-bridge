# Separate EPs and Singles

The runtime predicts Albums, EPs, Singles, Live Albums, Compilations and Others.
It uses title tokens, not authoritative Apple release categories. Broad cues such
as “EP Stories” or “Single Minded” can still be false positives. No new language,
track-count or duration inference is introduced.

The historical 10→6→5 network is preserved in `experiments/release_network/candidate.py`.
The runtime revision keeps its ten binary inputs and original six hidden channels,
adds an EP indicator channel, and splits the short-release output: **10→7→6, 125
explicit parameters**. This is a direct-weight edit, without training or new labels.

For single indicator `s`, EP indicator `e` and original short-release score
`h = 3s + 4e`, the new scores are:

- EP: `h + 3e − 3`
- Single: `h − 4e`

Their maximum is exactly `h` for every binary input. The other scores and their
relative tie order are unchanged. When the short-release category wins, both
markers mean EP (scores 7 versus 3). This is an explicit ambiguity policy, not a
claim about the true release type. It does not override an existing Live or
Compilation winner.

Grouping places Albums, EPs, Singles, Live Albums, Compilations, Others in that
order, preserving newest-first sorting and deterministic ties within each group.
The bridge serializes EPs as `["EP"]` and Singles as `["Single"]`. A both-marker
short release previously emitted both values; it now emits only EP, avoiding
placement in two client sections. Client grouping support is a separate change.
Existing stored title metadata is reclassified on response; client caches may
need refresh. No metadata/media cache migration is needed.

## Local comparison (2026-11-05)

All **1,024 binary input vectors** were compared against the frozen candidate:

| Old output | New output | Inputs |
| --- | --- | ---: |
| Albums | Albums | 1 |
| Live Albums | Live Albums | 33 |
| Compilations | Compilations | 550 |
| Others | Others | 130 |
| Singles & EPs | EPs | 280 |
| Singles & EPs | Singles | 30 |

These are exhaustive feature combinations, including combinations impossible for
real titles, not catalog frequencies or accuracy measurements. All 714 unrelated
category winners and all original scores (after merging the two short outputs by
maximum) are unchanged.

Replaying the **36 previously recorded titles** in `results.json` gives 13 unchanged
Albums, 10 unchanged Live Albums, and splits 13 short releases into 1 EP and 12
Singles. This is an offline behavior comparison, not new live metadata retrieval
or independently labeled EP-versus-Single accuracy. Historical data, predictions,
evaluation results and performance measurements remain untouched.

Reproduce with:

```sh
pytest -q tests/test_release_classifier.py tests/test_release_groups.py tests/test_bridge_release_types.py
```
