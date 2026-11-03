# Direct-weight release classifier experiment

**Follow-up:** [independent candidate and real-release comparison](REAL_RESULTS.md).
The original rule-encoding control below is retained for reproducibility. The
follow-up freezes a different 101-parameter network before collecting labels.

This original control is a **rule-encoding experiment**, not a learned classifier.
The player never imports this control. The later independent candidate is now the production default: see
[integration status](UI_TESTING.md).
The offline control below involves no network calls,
accounts, downloaded media, training dataset, optimizer, gradients, embeddings or
new dependencies are involved.

From the repository root, using the existing development environment:

```sh
python -m experiments.release_network.evaluate
python -m pytest -q experiments/release_network
ruff check experiments/release_network
ruff format --check experiments/release_network
```

The evaluator prints a JSON comparison, including each input, extracted features,
network scores, heuristic output and synthetic specification expectation. Scores
are decision outputs, **not probabilities or confidence**. To try a title locally:

```sh
python -c 'from experiments.release_network.network import classify; print(classify("Example (Live)"))'
```

## Relationship to the article

[Weight-Space Programming](https://rikiyat.github.io/blog/weight-space-programming/)
describes several protocols, not just iterative optimization. Experiment 5 writes
weights from rules in one shot without running the network before evaluation.
Experiment 6 permits bounded forward-pass feedback and direct weight edits, with
no fitting or gradient training. Its zero-feedback setting is also evaluated.

This small analogue constructs weights analytically from our category precedence,
then evaluates them. It does **not** reproduce the article's architectures,
benchmarks, tool restrictions or performance claims. We used coding/test tools,
and already knew the production heuristic and its edge cases. There is no blinded
holdout and no claim of independent zero-shot generalization. No iterative weight
search was performed; the initial numerical weights passed unchanged. The article
does not establish that these metadata can reveal editorial release categories.

## Network and provenance

`network.py` contains a dense **4 → 5 ReLU → 5 linear** network implemented from
scratch with standard-library matrix/vector arithmetic: 55 explicit parameters,
including biases and zeros. A deterministic argmax selects the category.

Inputs are binary `(p, s, c, l)`: title present, single/EP hint, compilation hint,
live-performance hint. The lexical adapter deliberately mirrors the existing
title/artist checks. It does not call the production classifier. Most semantic
work remains in that adapter; this is a neural representation of a known rule,
not an independent language understanding system.

Hidden units, in output order, compute:

| Category | Hidden activation |
| --- | --- |
| Albums | ReLU(p − s − c − l) |
| Singles & EPs | ReLU(p + s − 1) |
| Live Albums | ReLU(p − s − c + l − 1) |
| Compilations | ReLU(p − s + c − 1) |
| Others | ReLU(1 − p) |

The output matrix is identity. For all binary inputs exactly one output is 1:
missing title wins first, then single/EP, compilation, live, and album fallback.
This proves the precedence behavior; it does not prove semantic correctness.
Nonbinary, nonfinite and incorrectly sized feature vectors are rejected.

Only `Album.album_title` and `Album.artist` are used. IDs, storefront and release
year do not identify release type. Public iTunes `collectionType=Album` is not a
studio-album flag. No invented editorial fields, track-count certainty, artist
catalog memorization or new metadata service is added.

## Evaluation and result

Frozen weights were evaluated against 25 synthetic specification cases, including
localized suffixes, overlapping hints, missing titles and misleading word fragments.
The comparison uses the archived pre-network production heuristic in `baseline.py`.

| Measure | Result |
| --- | --- |
| Network / heuristic agreement | 25 / 25 |
| Network specification checks | 25 / 25 |
| Heuristic specification checks | 25 / 25 |
| Exhaustive binary cue combinations | 16 / 16 |
| Experiment pytest tests | 36 passed |

These fixtures are evaluation examples authored from the same specification,
**not a representative labeled catalog or an accuracy estimate**. Tests additionally
cover dimensional errors, NaN/infinity, deterministic inference and artist context.

There is no demonstrated improvement over the simpler heuristic. For example,
synthetic titles `Unmarked studio record` and `Unmarked concert recording` produce
identical features and therefore identical outputs, even if an editor would label
them differently. A larger network cannot reconstruct information discarded by
this encoder. Likewise a misleading explicit suffix fools both approaches.

Recommendation: keep the production heuristic. A meaningful next experiment would
need richer reliably sourced metadata and an independently labeled evaluation
set (which need not be a training set). If weights were then edited in response to
errors, those examples would become development feedback and require a separate
held-out evaluation. Nothing here justifies claiming learned intelligence or
reliable Apple Music editorial taxonomy.
