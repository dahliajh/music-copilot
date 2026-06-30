"""Music Copilot backend - FastAPI skeleton.

This wires the four module contracts together with MOCK implementations to
prove the interfaces compose end-to-end. No real OMR, pitch detection, or DTW
is implemented here - every response is clearly marked as placeholder/mock.

Run (once deps are installed):
    uvicorn app.main:app --reload
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from app.modules.assessment import (
    AssessmentResult,
    Mistake,
    MistakeType,
    Severity,
    ToleranceProfile,
    builtin_profiles,
)
from app.modules.score_align import (
    Alignment,
    AlignMode,
    AlignStrategy,
    NotePair,
)
from app.modules.score_ingest import (
    Score,
    ScoreNote,
    ScoreSourceFormat,
    TempoReference,
)
from app.modules.transcription import (
    DetectedNote,
    Transcription,
    TranscriptionConfig,
)

app = FastAPI(
    title="Music Copilot API",
    version="0.0.1-skeleton",
    description="Offline double-bass mistake detection. SKELETON ONLY - all "
    "analysis endpoints return mock data.",
)


# --------------------------------------------------------------------------- #
# Mock factories - stand-ins until the real modules land.
# --------------------------------------------------------------------------- #
def _mock_score(score_id: str = "mock-score-1") -> Score:
    notes = [
        ScoreNote(index=i, midi=m, pitch_class=m % 12, onset_beats=float(i),
                  duration_beats=1.0)
        for i, m in enumerate([43, 45, 47, 48])  # G2 A2 B2 C3
    ]
    return Score(
        score_id=score_id,
        title="MOCK - C major fragment",
        source_format=ScoreSourceFormat.MUSICXML,
        tempo=TempoReference(bpm=60.0),
        notes=notes,
    )


def _mock_transcription() -> Transcription:
    # Note 2 is deliberately an octave high to exercise the octave-off path.
    notes = [
        DetectedNote(index=0, midi=43, onset_s=0.0, offset_s=1.0, confidence=0.9),
        DetectedNote(index=1, midi=45, onset_s=1.0, offset_s=2.0, confidence=0.8),
        DetectedNote(index=2, midi=59, onset_s=2.0, offset_s=3.0, confidence=0.6,
                     octave_corrected=False),
        DetectedNote(index=3, midi=48, onset_s=3.0, offset_s=4.0, confidence=0.95),
    ]
    return Transcription(
        notes=notes,
        config=TranscriptionConfig(),
        sample_rate=44100,
        duration_s=4.0,
    )


def _mock_alignment() -> Alignment:
    pairs = [NotePair(ref_index=i, performed_index=i, local_cost=0.1)
             for i in range(4)]
    return Alignment(
        mode=AlignMode.OFFLINE,
        strategy=AlignStrategy.SUBSEQUENCE_DTW,
        pairs=pairs,
    )


def _mock_assessment(profile: ToleranceProfile) -> AssessmentResult:
    # Reflect the planted octave-off note (ref 2) per the profile's policy.
    return AssessmentResult(
        profile_name=profile.name,
        mistakes=[
            Mistake(
                ref_index=2,
                performed_index=2,
                type=MistakeType.OCTAVE_OFF,
                severity=Severity.INFO,
                detail="MOCK: same pitch-class, one octave high.",
                cents_off=0.0,
            )
        ],
        correct_ref_indices=[0, 1, 3],
    )


# --------------------------------------------------------------------------- #
# Request / response envelopes
# --------------------------------------------------------------------------- #
class ScoreImportResponse(BaseModel):
    mock: bool = True
    score: Score


class AnalyzeRequest(BaseModel):
    score_id: str
    profile_name: str = "beginner"
    # In the real app, audio arrives as an uploaded file; the skeleton ignores it.


class AnalyzeResponse(BaseModel):
    mock: bool = True
    transcription: Transcription
    alignment: Alignment
    assessment: AssessmentResult


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "stage": "skeleton"}


@app.get("/profiles")
def list_profiles() -> dict[str, ToleranceProfile]:
    return builtin_profiles()


@app.post("/score/import", response_model=ScoreImportResponse)
def import_score() -> ScoreImportResponse:
    """PLACEHOLDER. Real impl: accept a MusicXML/PDF upload, run ScoreIngester,
    persist, and return the canonical Score.
    """
    return ScoreImportResponse(score=_mock_score())


@app.post("/performance/analyze", response_model=AnalyzeResponse)
def analyze_performance(req: AnalyzeRequest) -> AnalyzeResponse:
    """PLACEHOLDER. Real impl: load Score by id, run Transcriber on the uploaded
    audio, ScoreAligner.align(), then Assessor.assess() with the chosen profile.

    This mock composes the real contract TYPES to prove they fit together.
    """
    profiles = builtin_profiles()
    profile = profiles.get(req.profile_name, profiles["beginner"])

    transcription = _mock_transcription()
    alignment = _mock_alignment()
    assessment = _mock_assessment(profile)

    return AnalyzeResponse(
        transcription=transcription,
        alignment=alignment,
        assessment=assessment,
    )
