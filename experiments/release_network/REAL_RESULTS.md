# Independent direct-weight candidate: real-release comparison

The user requested a classifier designed from scratch rather than a neural copy
of our existing rules. `candidate.py` implements that second experiment. It is
unchanged since evaluation. A subsequent opt-in [UI integration](UI_TESTING.md)
is available on the experimental branch; main remains unchanged. The measurements
below describe the frozen classifier, not end-to-end UI performance.

## Reproduce

Use the repository's existing development environment, from the repository root:

```sh
python -m experiments.release_network.compare_real
python -m pytest -q experiments/release_network
ruff check experiments
ruff format --check experiments
```

`results.json` contains the complete reproducible output: input metadata, source
URLs, features, scores, predictions, confusion matrices and every disagreement.
The evaluator is entirely offline; no Apple account or runtime is involved.

## Design and sequencing

1. Candidate weights and encoder frozen in commit
   `da749845de538c79490ded80e0d4777212ba0a32`, before new label collection or inference.
2. Real-release facts and observed page-section labels recorded separately in
   `b237cae`, before candidate evaluation.
3. Evaluation and tests added afterward. **No weight or encoder edits after results.**

The SHA-256 of frozen `candidate.py` is
`f8d15e7917e74dc76fd05732331f0becacfc92a297ca1aeb68468f697ce57f14`.
The evaluator verifies it. A local lint exception preserves an intentionally
truncating adjacent-token zip without modifying frozen bytes.

The network is **10 → 6 ReLU → 5 linear**, with all **101 parameters** directly
specified in one pass. Ten binary title features feed evidence channels for short
releases, live performances, compilations, remixes, presence and absence. Broad
words/phrases, Unicode normalization and numeric evidence combinations replace
the control's exact rule precedence. An Albums prior handles unmarked titles;
remixes alone favor Others. There are no title-specific lookups, artist identity
weights, training examples, optimizer or gradient updates. Scores are not calibrated
probabilities. Date and artist IDs offer no justified type signal here, and richer
unavailable metadata was not invented.

The [article's experiment 5](https://rikiyat.github.io/blog/weight-space-programming/)
motivates direct weight construction followed by evaluation. This is **not a strict
replication**: tools implement and evaluate our network; the authoring model already
knew the heuristic and some release examples from earlier work. No clean-room or
unseen-data claim is possible. The labels were assembled separately from the
classifiers, but not by a blinded independent human annotator. Feature engineering
and the model author's prior knowledge remain part of the method.

## Sources and label definition

Metadata comes from the visible release sections of Apple's public US pages:

- [Björk](https://music.apple.com/us/artist/bj%C3%B6rk/295015): 27 unique releases.
- [Faye Webster](https://music.apple.com/us/artist/faye-webster/532412540): 9 releases.

All visible entries in Albums, Singles & EPs, Live Albums and Compilations were
transcribed, not just examples favoring the candidate. Top songs, essential albums,
videos, playlists and Appears On were excluded. No See All expansion. The source
includes an upcoming release; it remains included under the same selection rule.
`real_releases.json` records original titles, displayed years, source URL, observed
sections and collection date (2026-11-03). No full editorial text or images copied.

This is public web metadata, **not a live BR iTunes API response**. The web tool's
representations were cached (Björk: three days; Faye: last week). One initial Faye
URL with an incorrect ID was inaccessible; the correct ID was resolved through
public search. No API credential, denied route or private data was used.

Labels mean **observed page-section membership**, not adjudicated release-type
truth. Mount Wittenberg Orca (Expanded Edition) appears under both Albums and
Compilations; it is retained in the data but excluded from single-label scoring.
Other oddities are preserved: some titles ending in Single occur in Compilations.
No title suffix was used to override the source label. Artist-page presence does
not establish sole primary-artist credit. No Others examples were visible.

## Results

| Observed section | Count | Candidate matches | Heuristic matches |
| --- | ---: | ---: | ---: |
| Albums | 11 | 10 | 11 |
| Singles & EPs | 11 | 10 | 10 |
| Live Albums | 7 | 7 | 6 |
| Compilations | 6 | 0 | 0 |
| Others | 0 | n/a | n/a |
| **Total** | **35** | **27 (77.1%)** | **27 (77.1%)** |

Both match 18/26 Björk and 9/9 Faye Webster labels. This is a small convenience
sample's section agreement, **not a general catalog accuracy estimate**.

All three real-release disagreements:

| Title | Observed section | Candidate | Heuristic |
| --- | --- | --- | --- |
| Cornucopia Live | Albums | Live Albums | Albums |
| Apple Music Live: Björk (Cornucopia) | Live Albums | Live Albums | Albums |
| Apple Music Live: NYE 2025 (DJ Mix) | Compilations | Live Albums | Albums |

The first row also shows why section agreement is not the same as semantic truth.
Neither method recognizes opaque compilation names or the source's inconsistent
placement from the available title cues.

Separately, on the original 25 synthetic specification cases, the candidate matches
20 and the heuristic 25. Candidate failures: The Best of Me, Live Forever, Single
Minded, EP Stories and Untitled Live. These expose broader-cue risks; the synthetic
labels intentionally reflect the earlier specification, not real editorial truth.
They are not pooled with the real-release score.

## Interpretation

It is possible to specify every weight of a functioning classifier without training.
This independently designed candidate behaves differently and improves one live
case, but demonstrates **no aggregate gain** on the sourced sample and weaker
behavior on adversarial titles. Weights remain unchanged despite those findings.

Keep production unchanged. A future experiment could preregister richer inputs
and gather an independently adjudicated, broader evaluation set with Others cases.
If this sample guides any edits, it becomes development feedback; a new untouched
evaluation set would be required. No such tuning is included here.

## Inference cost

Reproduce with `python -m experiments.release_network.benchmark`; the captured run
is in `benchmark_results.json`, including all nine timing samples per method/batch.
CPython 3.12.14, Linux 6.18.57 x86_64, shared cloud VM reporting AMD EPYC 9V74 and
five visible logical CPUs. One process/thread; no CPU pinning or GC disabling.
Five warmup batches per method, then nine samples of 50 batches each, alternating
method order. Times include title feature extraction and output list allocation.
Album objects are constructed beforehand for both methods. Batches cycle the 36
source titles in fixed order; the 200-row case is not 200 unique releases.

| Batch size | Heuristic median | Candidate median |
| ---: | ---: | ---: |
| 10 | 0.0368 ms | 0.1656 ms |
| 50 | 0.1572 ms | 0.7849 ms |
| 200 | 0.6465 ms | 3.2160 ms |

At 200 rows this is about 3.23 versus 16.08 microseconds per release: the candidate
is roughly five times slower in this Python implementation, but the measured
absolute difference is about 2.57 ms per batch. This is a warm-process microbenchmark,
**not end-to-end catalog latency**. It excludes network waits, imports, parsing,
artwork, Qt rendering and playback. No user-hardware speed or UI improvement is
established, and small timings vary with shared-host scheduling.

No performance optimization was applied. Hypothetically, batching dense arithmetic
or caching features might reduce overhead, but neither was measured and both add
complexity. Given no demonstrated classification gain, this experiment does not
justify optimizing or replacing the production heuristic.

## Try live public catalog metadata locally

Run explicitly from the development environment:

```sh
python -m experiments.release_network.live_compare --country br \
  --artist-id 295015 --artist-id 532412540 > live-release-predictions.json
```

These are the public artist IDs for Björk and Faye Webster. The command uses the
existing `Catalog.artist_albums` adapter, public HTTPS lookup and its parsing/size
limits. It never starts the managed service or accesses credentials. It respects
three-second request spacing and Retry-After, with at most three deferred retries
and a 30-second cumulative sleep budget per artist. Longer waits cause failure
rather than an early retry; Ctrl-C cancels. No raw HTTP errors/bodies are printed.
Empty results remain empty and do not cause retry loops.

Output is public metadata plus both predictions, **not an accuracy score**: iTunes
does not supply the page-section labels used above. No attempt is made to join US
web labels to potentially different BR editions. The command has mock tests for
spacing, bounded rate limiting, region/ID validation and deduplication.

The live command was **not run against iTunes here**: the selected executor's
restricted network policy does not list `itunes.apple.com`. No request or bypass
was attempted. The sourced web-page evaluation above must not be mistaken for a
successful live API run. If the local command fails, it exits nonzero with a generic
message; inspect the network/storefront without sharing credentials or raw logs.
