"""Tests for /performance/analyze's score lookup (real score_id vs. the
fixed demo reference vs. an unknown id).

`client` shares one `app` (and therefore one `_score_store`) across every
test in the process, matching test_score_import_endpoint.py's pattern -
each test here uses its own score_id so tests can't collide with each
other or with anything imported elsewhere in the suite.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)


def test_analyze_with_mock_reference_id_uses_demo_score() -> None:
    """score_id="mock-reference" works without importing anything first,
    and reproduces the octave-off demo scenario (kept from before this
    session's persistence work)."""
    resp = client.post(
        "/performance/analyze", json={"score_id": "mock-reference", "profile_name": "beginner"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mock"] is True
    mistake_types = [m["type"] for m in body["assessment"]["mistakes"]]
    assert "octave_off" in mistake_types


def test_analyze_with_unknown_score_id_is_404() -> None:
    resp = client.post(
        "/performance/analyze",
        json={"score_id": "never-imported-xyz", "profile_name": "beginner"},
    )
    assert resp.status_code == 404
    assert "never-imported-xyz" in resp.json()["detail"]
    assert "/score/import" in resp.json()["detail"]


def test_import_then_analyze_uses_the_real_imported_score() -> None:
    """The core persistence behavior: analyze against a score_id that was
    actually imported runs alignment/assessment against that REAL 10-note
    score, not the mock 4-note reference."""
    content = (FIXTURES / "sample_bass_excerpt.musicxml").read_bytes()
    import_resp = client.post(
        "/score/import",
        files={"file": ("sample_bass_excerpt.musicxml", content, "application/xml")},
        data={"score_id": "test-real-score-for-analyze"},
    )
    assert import_resp.status_code == 200
    assert import_resp.json()["score"]["score_id"] == "test-real-score-for-analyze"

    analyze_resp = client.post(
        "/performance/analyze",
        json={"score_id": "test-real-score-for-analyze", "profile_name": "beginner"},
    )
    assert analyze_resp.status_code == 200
    body = analyze_resp.json()

    # Structural proof this is the real 10-note score, not the mock
    # 4-note reference: every ref_index the alignment touched (whether
    # matched, missed, or correct) must fit within 0-9, and at least one
    # must be >= 4 (impossible against the 4-note mock reference).
    ref_indices = {p["ref_index"] for p in body["alignment"]["pairs"] if p["ref_index"] is not None}
    assert ref_indices, "alignment produced no ref-indexed pairs at all"
    assert all(0 <= i <= 9 for i in ref_indices)
    assert any(i >= 4 for i in ref_indices)
