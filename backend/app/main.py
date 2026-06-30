"""Music Copilot backend - FastAPI skeleton.

This wires the four module contracts together. As of Phase 1, score-ingest
(`/score/import`) is a real implementation (MusicXMLIngester); transcription,
alignment, and assessment are still MOCK implementations, since Phases 2-3
haven't landed yet. No real pitch detection or DTW is implemented yet -
`/performance/analyze`'s response is clearly marked as placeholder/mock.

Run (once deps are installed):
    uvicorn app.main:app --reload
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.modules.assessment import (
    AssessmentResult,
    Mistake,
    MistakeType,
    Severity,
    ToleranceProfile,
    builtin_profiles,
)
from app.modules.musicxml_ingester import MusicXMLIngester
from app.modules.score_align import (
    Alignment,
    AlignMode,
    AlignStrategy,
    NotePair,
)
from app.modules.score_ingest import (
    Score,
    ScoreIngestError,
    ScoreSourceFormat,
)
from app.modules.transcription import (
    DetectedNote,
    Transcription,
    TranscriptionConfig,
)

_score_ingester = MusicXMLIngester()

app = FastAPI(
    title="Music Copilot API",
    version="0.0.1-skeleton",
    description="Offline double-bass mistake detection. SKELETON - "
    "/score/import is real (Phase 1), /performance/analyze still "
    "returns mock data pending Phases 2-3.",
)


# --------------------------------------------------------------------------- #
# Mock factories - stand-ins until the real modules land. score-ingest (Phase
# 1) is implemented for real now (see MusicXMLIngester below); transcription,
# alignment, and assessment are still mocked pending Phases 2-3.
# --------------------------------------------------------------------------- #
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
    mock: bool = False  # score-ingest (Phase 1) is real; kept for schema parity with AnalyzeResponse.
    score: Score
    warnings: list[ScoreIngestError] = []


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
async def import_score(
    file: UploadFile = File(..., description="A MusicXML file (.musicxml/.xml)."),
    score_id: Optional[str] = Form(default=None),
    source_format: ScoreSourceFormat = Form(default=ScoreSourceFormat.MUSICXML),
) -> ScoreImportResponse:
    """Import a score from an uploaded MusicXML file (Phase 1 of the MVP plan).

    Real implementation: `MusicXMLIngester` (music21 under the hood). Handles
    `musicxml` and `musicxml_pdf` (a human already converted the PDF to
    MusicXML by this point). `omr_photo` (phone photo -> OMR) is a v1.5+ path
    with no implementation yet, and is rejected here.

    Recoverable issues (no tempo marking, chords/multiple voices/parts in
    what's expected to be a monophonic part) don't fail the request — they
    come back in `warnings` alongside the best-effort `score`. There is no
    persistence layer yet; the caller is responsible for holding onto the
    returned `Score` (e.g. to pass its `score_id` into `/performance/analyze`
    once that's no longer mocked).
    """
    if not _score_ingester.supports(source_format):
        raise HTTPException(
            status_code=422,
            detail=f"source_format={source_format.value!r} is not supported "
            "yet (only 'musicxml' and 'musicxml_pdf').",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    try:
        result = _score_ingester.ingest(raw, source_format, score_id=score_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ScoreImportResponse(score=result.score, warnings=result.warnings)


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
