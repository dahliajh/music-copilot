"""Tests for the /score/import endpoint (Phase 1 of the MVP plan).

Exercises the FastAPI wiring on top of MusicXMLIngester — see
test_musicxml_ingester.py for the ingester's own unit tests, which cover
the parsing edge cases (ties, rests, chords, etc.) in more depth.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)


def _upload(name: str, **data):
    content = (FIXTURES / name).read_bytes()
    return client.post(
        "/score/import",
        files={"file": (name, content, "application/xml")},
        data=data,
    )


def test_import_sample_excerpt_returns_real_score_and_warnings() -> None:
    resp = _upload("sample_bass_excerpt.musicxml")
    assert resp.status_code == 200

    body = resp.json()
    assert body["mock"] is False
    assert len(body["score"]["notes"]) == 10
    assert any(w["code"] == "missing_tempo" for w in body["warnings"])


def test_import_honors_caller_supplied_score_id() -> None:
    resp = _upload("sample_bass_excerpt.musicxml", score_id="my-custom-id")
    assert resp.status_code == 200
    assert resp.json()["score"]["score_id"] == "my-custom-id"


def test_import_empty_file_is_422() -> None:
    resp = client.post(
        "/score/import", files={"file": ("empty.musicxml", b"", "application/xml")}
    )
    assert resp.status_code == 422


def test_import_unparseable_file_is_422_with_detail() -> None:
    resp = client.post(
        "/score/import",
        files={"file": ("bad.musicxml", b"not xml at all", "application/xml")},
    )
    assert resp.status_code == 422
    assert "Could not parse" in resp.json()["detail"]


def test_import_rejects_omr_photo_source_format() -> None:
    resp = _upload("sample_bass_excerpt.musicxml", source_format="omr_photo")
    assert resp.status_code == 422
    assert "not supported yet" in resp.json()["detail"]


def test_health_and_profiles_still_work() -> None:
    assert client.get("/health").json()["status"] == "ok"
    profiles = client.get("/profiles").json()
    assert "beginner" in profiles and "advanced" in profiles
