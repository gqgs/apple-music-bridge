# Candidate v1: pre-evaluation specification

Freeze `candidate.py` in its own commit before collecting this round's real-release
labels or running predictions. Do not edit its weights or encoder after evaluation.
This supersedes the rule-compilation-only scope of the first experiment, which
remains available as a control. No training, fitting or numerical search.

The candidate is a 10 → 6 ReLU → 5 linear network, with 101 manually specified
parameters. Broad lexical features feed weighted evidence channels with weaker
cross-category inhibition, rather than implementing the production decision tree.
Ordinary titles have a modest Albums prior. Explicit words supply evidence for
short releases, live recordings and compilations. Remixes without stronger cues
favor Others. These are speculative choices, not established editorial rules.

Available title metadata is sufficient to run it. Artist identity, release year
and IDs are deliberately excluded to avoid catalog memorization or spurious year
correlations. No track count or editorial flags are assumed available. Feature
engineering still contains human linguistic knowledge; this is not raw-text
language understanding or learned knowledge.

Evaluation plan: collect public primary-source release titles and category evidence
for Björk and Faye Webster after freezing. Use all accessible entries in selected
source sections, record per-row URLs and evidence, and explicitly separate ambiguous
or overlapping categories. Compare both classifiers on exactly the same inputs.
Do not derive labels from either classifier or title regex. Report per-class counts,
confusion matrices and every disagreement. No claim of a representative sample.
Check synthetic counterexamples separately, never pool them into real-release accuracy.

This is not a blinded replication of article experiment 5: the authoring model
already saw the current heuristic, prior examples and some Björk/Faye release names
in this conversation. Tool-assisted implementation/evaluation is used. Freezing
before this round's labels limits subsequent tuning but does not erase that prior
exposure or turn this convenience sample into an independent held-out benchmark.
No weights have been chosen by running candidate predictions before this freeze.
