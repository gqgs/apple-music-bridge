# Player integration status

The user accepted the directly programmed network after live testing. It is now
the default and sole production release classifier in `release_classifier.py`.
The experimental selector, comparison tooltips and old production heuristic were
removed. No opt-in or configuration migration is needed. Search for an artist and
open their page to see grouped releases; album navigation and playback are unchanged.

The packaged network is byte-identical to the frozen candidate, verified by test.
The old heuristic is archived in `baseline.py` for reproducible offline comparisons
only. `compare_real`, `benchmark` and `live_compare` use that snapshot rather than
pretending the new production classifier is still the old baseline.

The results and limitations in [REAL_RESULTS.md](REAL_RESULTS.md) remain valid;
acceptance after user testing is not evidence of higher benchmark accuracy. To run
the player, use the normal installation instructions in the root README. No model
or dependency download is required beyond the existing app environment.

Historical opt-in UI testing occurred at commit
`a4cd90da83f13a6d95aa7426343eaf51adbd952b`; its selector and branch-specific testing
instructions no longer describe the current UI.
