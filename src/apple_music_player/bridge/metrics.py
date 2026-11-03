"""Aggregate diagnostics only: no URLs, queries, item IDs or credentials."""

import threading


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}

    def record(self, stage, outcome, seconds=0):
        if stage not in {
            "cover_cache",
            "cover_fetch",
            "cover_wait",
            "metadata_queue",
            "metadata",
            "metadata_work",
            "catalog_wait",
            "download",
        }:
            raise ValueError("Unknown metric stage")
        if outcome not in {
            "hit",
            "deferred",
            "rate_limited",
            "miss",
            "ok",
            "failed",
            "busy",
            "timeout",
            "cancelled",
            "missing",
            "negative",
            "network",
            "server",
            "denied",
            "invalid",
            "tls",
            "http",
            "unavailable",
        }:
            raise ValueError("Unknown metric outcome")
        milliseconds = max(0, round(seconds * 1000))
        with self.lock:
            value = self.values.setdefault((stage, outcome), [0, 0, 0])
            value[0] += 1
            value[1] += milliseconds
            value[2] = max(value[2], milliseconds)

    def snapshot(self):
        with self.lock:
            return [
                {"stage": stage, "outcome": outcome, "count": v[0], "totalMs": v[1], "maxMs": v[2]}
                for (stage, outcome), v in sorted(self.values.items())
            ]
