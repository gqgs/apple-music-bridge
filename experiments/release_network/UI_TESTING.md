# Test the frozen classifier in the player

Close the player before switching branches. From your existing checkout:

```sh
git fetch origin
git switch experiment/release-weight-network
git pull --ff-only
. .venv/bin/activate
pip install -e '.[dev]'
apple-music-player
```

The branch is experimental and **not merged into main**. If Git reports local
changes or a divergent branch, preserve your work instead of resetting it.
Use your existing environment path if it differs from `.venv`.

1. Open **Search**. Under the search field, select **Experimental neural network**
   in **Release grouping (experiment)**.
2. Search **Artists** for Björk or Faye Webster and open the artist. The usual
   public catalog, storefront and automatic rate-limit handling apply.
3. The grouped list and status identify the selected classifier. Hover any release
   to see both predictions. The status counts disagreements, not known errors.
4. Switch to **Current heuristic** to regroup the same loaded releases immediately,
   without another network request. Selection is retained; scroll position is kept
   where possible. During an in-flight lookup, the next result uses your choice.
5. Open albums and play/queue as before. Back restores that page's classifier and
   navigation context. Restarting the application defaults to the heuristic; no
   new preference is written to settings.

The packaged `src/apple_music_player/experimental_classifier.py` is a byte-identical
copy of the frozen 101-parameter candidate, checked by SHA-256 in tests. It uses no
extra library or model download. The experiment's offline scripts remain usable.
Only artist-page grouping uses the selected classifier; search ranking, album
contents, Home shelves, authentication, media verification and playback are unchanged.

The candidate was directly programmed, not trained. It matched 27/35 public page
section labels, exactly the heuristic's aggregate result, and introduced synthetic
false positives. Neither prediction is an authoritative Apple category. Inference
was approximately 3.22 ms per 200-title batch in the recorded cloud microbenchmark;
this is not a user-hardware or UI latency measurement.

Automated UI tests use synthetic metadata and no account/runtime execution. They
cover mode changes, comparison tooltips, selection, empty results, pending results,
Back navigation, and the exact frozen packaged model. Normal and 1.5× Qt scale
tests pass. Live iTunes and actual account/playback testing remain for your machine.

To leave the experiment, close the app, switch to `main`, reinstall the editable
package if needed, and relaunch. No credential, cache or settings migration is needed.
