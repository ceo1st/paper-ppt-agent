from __future__ import annotations

from backend.workers import consumer


def test_configured_worker_count_is_clamped(monkeypatch) -> None:
    monkeypatch.setattr(consumer.settings, "generation_worker_concurrency", 99)

    assert consumer.worker_count() == 8


def test_auto_worker_count_is_at_least_one(monkeypatch) -> None:
    monkeypatch.setattr(consumer.settings, "generation_worker_concurrency", 0)
    monkeypatch.setattr(consumer.os, "cpu_count", lambda: 2)

    assert consumer.worker_count() == 1
