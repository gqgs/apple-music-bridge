"""Warm-process CPU microbenchmark; deliberately excludes network and GUI work."""

import argparse
import gc
import json
import os
import platform
import statistics
import time
from pathlib import Path

from apple_music_player.model import Album

from . import candidate
from .baseline import release_group
from .compare_real import ROOT


def run(repeats=9, loops=50):
    if repeats < 1 or loops < 1:
        raise ValueError("Positive repeats and loops required")
    records = json.loads((ROOT / "real_releases.json").read_text())["rows"]
    albums = [
        Album("us", "fixture", r["title"], artist=r["artist"], album_title=r["title"])
        for r in records
    ]
    functions = {
        "heuristic": release_group,
        "candidate": lambda album: candidate.classify(album.album_title),
    }
    batches = []
    for size in (10, 50, 200):
        # Cycle actual titles in fixed source order; no fabricated long titles.
        batch = [albums[index % len(albums)] for index in range(size)]
        for function in functions.values():
            for _ in range(5):
                [function(album) for album in batch]
        samples = {name: [] for name in functions}
        for repeat in range(repeats):
            order = list(functions) if repeat % 2 == 0 else list(reversed(functions))
            for name in order:
                function = functions[name]
                start = time.perf_counter_ns()
                for _ in range(loops):
                    [function(album) for album in batch]
                samples[name].append((time.perf_counter_ns() - start) / loops / 1_000_000)
        batches.append(
            {
                "size": size,
                "timings": {
                    name: {
                        "median_ms_per_batch": statistics.median(values),
                        "min_ms_per_batch": min(values),
                        "max_ms_per_batch": max(values),
                        "median_us_per_release": statistics.median(values) * 1000 / size,
                        "samples_ms_per_batch": values,
                    }
                    for name, values in samples.items()
                },
            }
        )
    cpu = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        cpu = next(
            (
                line.split(":", 1)[1].strip()
                for line in cpuinfo.read_text().splitlines()
                if line.startswith("model name")
            ),
            "unknown",
        )
    return {
        "context": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "os": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "cpu_model": cpu,
            "visible_logical_cpus": os.cpu_count(),
            "gc_enabled": gc.isenabled(),
            "timer": "perf_counter_ns",
            "repeats": repeats,
            "loops_per_sample": loops,
            "warmup_batches": 5,
        },
        "method": (
            "Single process/thread, alternating measurement order, warm regex caches; "
            "includes title normalization/features and output list allocation; excludes "
            "imports, fixture parsing, Album construction, network, artwork, Qt and playback. "
            "Shared cloud VM, no CPU pinning. Batches cycle 36 real titles; "
            "not 200 unique releases."
        ),
        "batches": batches,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--loops", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(run(args.repeats, args.loops), indent=2))
