"""Tests for InMemoryScoreStore, the pragmatic persistence seam between
/score/import and /performance/analyze (see score_store.py docstring)."""

from __future__ import annotations

from app.modules.score_ingest import Score, ScoreSourceFormat, TempoReference
from app.modules.score_store import InMemoryScoreStore


def _score(score_id: str) -> Score:
    return Score(
        score_id=score_id,
        source_format=ScoreSourceFormat.MUSICXML,
        tempo=TempoReference(bpm=60.0),
        notes=[],
    )


def test_get_unknown_id_returns_none() -> None:
    store = InMemoryScoreStore()
    assert store.get("nope") is None


def test_save_then_get_round_trips() -> None:
    store = InMemoryScoreStore()
    score = _score("abc-123")
    store.save(score)
    assert store.get("abc-123") is score


def test_save_overwrites_existing_id() -> None:
    store = InMemoryScoreStore()
    store.save(_score("id-1"))
    second = _score("id-1")
    store.save(second)
    assert store.get("id-1") is second


def test_different_ids_dont_collide() -> None:
    store = InMemoryScoreStore()
    a, b = _score("a"), _score("b")
    store.save(a)
    store.save(b)
    assert store.get("a") is a
    assert store.get("b") is b
