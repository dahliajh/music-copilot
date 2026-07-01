"""Tests for /performance/analyze_recording - the fully-real pipeline
endpoint (real transcription via PyinTranscriber + real alignment + real
assessment), as opposed to /performance/analyze which still uses
_mock_transcription() (see test_analyze_endpoint.py for that one).

Audio is synthesized in-test (sine waves), same rationale as
test_pyin_transcriber.py: no real recording is committed to this repo.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SAMPLE_RATE = 22050


def _sine_samples(freq_hz: float, duration_s: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    t = np.arange(int(duration_s * sample_rate)) / sample_rate
    return 0.6 * np.sin(2 * np.pi * freq_hz * t)


def _wav_bytes(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples.astype(np.float32), sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _midi_to_hz(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def test_analyze_recording_against_mock_reference_score() -> None:
    # _mock_reference_score() is 4 quarter notes at 60bpm: midi 43,45,47,48
    # starting at 0,1,2,3 seconds. Play the first two in tune to get at
    # least a couple of real matches without needing a long/complex clip.
    samples = np.concatenate([_sine_samples(_midi_to_hz(43), 1.0), _sine_samples(_midi_to_hz(45), 1.0)])
    audio = _wav_bytes(samples)

    resp = client.post(
        "/performance/analyze_recording",
        files={"audio": ("test.wav", audio, "audio/wav")},
        data={"score_id": "mock-reference", "profile_name": "beginner"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mock"] is False
    assert body["transcription"]["notes"], "expected the real transcriber to detect something"
    assert body["transcription"]["sample_rate"] == SAMPLE_RATE


def test_analyze_recording_unknown_score_id_is_404() -> None:
    audio = _wav_bytes(_sine_samples(_midi_to_hz(43), 0.5))
    resp = client.post(
        "/performance/analyze_recording",
        files={"audio": ("test.wav", audio, "audio/wav")},
        data={"score_id": "never-imported-xyz"},
    )
    assert resp.status_code == 404


def test_analyze_recording_empty_audio_is_422() -> None:
    resp = client.post(
        "/performance/analyze_recording",
        files={"audio": ("empty.wav", b"", "audio/wav")},
        data={"score_id": "mock-reference"},
    )
    assert resp.status_code == 422


def test_analyze_recording_unparseable_audio_is_422() -> None:
    resp = client.post(
        "/performance/analyze_recording",
        files={"audio": ("bad.wav", b"this is not a wav file", "audio/wav")},
        data={"score_id": "mock-reference"},
    )
    assert resp.status_code == 422
    assert "Could not decode audio" in resp.json()["detail"]


def test_performance_analyze_json_endpoint_unaffected() -> None:
    """Regression guard: the original JSON-body endpoint must still work
    exactly as before - adding analyze_recording must not have touched it."""
    resp = client.post(
        "/performance/analyze", json={"score_id": "mock-reference", "profile_name": "beginner"}
    )
    assert resp.status_code == 200
    assert resp.json()["mock"] is True
