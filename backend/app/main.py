"""Music Copilot backend - FastAPI skeleton.

This wires the four module contracts together, plus a lightweight
persistence seam (`score_store.py`) so a score imported in one request can
be looked up by id in another.

  * score-ingest (`/score/import`): REAL (MusicXMLIngester, Phase 1).
    Imported scores are persisted (`InMemoryScoreStore`) so a later
    analyze call can look them up by `score_id`.
  * score-align and assessment: REAL (OfflineDtwAligner,
    RuleBasedAssessor - including tempo-elastic timing, see
    rule_based_assessor.py) in both analyze endpoints below.
  * transcription (Phase 2): REAL as of pyin_transcriber.py
    (PyinPitchTracker + RangeClampOctaveCorrector), but only reachable
    through `/performance/analyze_recording` (multipart audio upload).
    `/performance/analyze` (JSON body, no audio field) still uses
    `_mock_transcription()` - kept as its own unchanged endpoint rather
    than retrofitted, so nothing that already depends on its JSON
    contract breaks. New callers with real audio should use
    `/performance/analyze_recording`.

Run (once deps are installed):
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import io
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.modules.assessment import (
    AssessmentResult,
    ToleranceProfile,
    builtin_profiles,
)
from app.modules.musicxml_ingester import MusicXMLIngester
from app.modules.offline_dtw_aligner import OfflineDtwAligner
from app.modules.pyin_transcriber import PyinTranscriber
from app.modules.rule_based_assessor import RuleBasedAssessor
from app.modules.score_align import (
    Alignment,
    AlignConfig,
)
from app.modules.score_ingest import (
    Score,
    ScoreIngestError,
    ScoreNote,
    ScoreSourceFormat,
    TempoReference,
)
from app.modules.score_store import InMemoryScoreStore
from app.modules.transcription import (
    DetectedNote,
    Transcription,
    TranscriptionConfig,
)

_score_ingester = MusicXMLIngester()
_aligner = OfflineDtwAligner()
_assessor = RuleBasedAssessor()
_score_store = InMemoryScoreStore()
_transcriber = PyinTranscriber()

# Sentinel id for the fixed demo reference score (see _mock_reference_score),
# kept requestable by name so the octave-off demo scenario still works
# without importing a file first. Any other unknown score_id is a real 404.
_MOCK_REFERENCE_SCORE_ID = "mock-reference"

app = FastAPI(
    title="Music Copilot API",
    version="0.0.1-skeleton",
    description="Offline double-bass mistake detection. SKELETON - "
    "/score/import is real (Phase 1); /performance/analyze runs real "
    "alignment/assessment against a real or demo score, but still uses "
    "mock transcription pending Phase 2.",
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


def _mock_reference_score() -> Score:
    """Fixed reference score matching `_mock_transcription()`'s 4 notes, so
    `/performance/analyze` has something real to align/assess against until
    there's a persistence layer + real Score lookup by `score_id`.

    Note 2 is intentionally a different octave (47, B2) from the mock
    performance's detected note 2 (59, B3 - same pitch class, wrong
    octave) so the endpoint still exercises the octave-off path, now
    through the real OfflineDtwAligner + RuleBasedAssessor instead of a
    hand-faked AssessmentResult.
    """
    notes = [
        ScoreNote(index=0, midi=43, pitch_class=43 % 12, onset_beats=0.0, duration_beats=1.0),
        ScoreNote(index=1, midi=45, pitch_class=45 % 12, onset_beats=1.0, duration_beats=1.0),
        ScoreNote(index=2, midi=47, pitch_class=47 % 12, onset_beats=2.0, duration_beats=1.0),
        ScoreNote(index=3, midi=48, pitch_class=48 % 12, onset_beats=3.0, duration_beats=1.0),
    ]
    return Score(
        score_id=_MOCK_REFERENCE_SCORE_ID,
        title="Mock reference score (matches _mock_transcription)",
        source_format=ScoreSourceFormat.MUSICXML,
        tempo=TempoReference(bpm=60.0),  # 1 beat == 1s, matches the mock onsets directly
        notes=notes,
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
    come back in `warnings` alongside the best-effort `score`. The imported
    score is persisted (see `score_store.py`) so its `score_id` can be
    passed into `/performance/analyze` in a later request; this is an
    in-process store (no durability across restarts, no multi-worker
    sharing) - fine for this skeleton, not for production.
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

    _score_store.save(result.score)
    return ScoreImportResponse(score=result.score, warnings=result.warnings)


def _resolve_score(score_id: str) -> Score:
    """Shared score_id -> Score lookup used by both analyze endpoints.
    `_MOCK_REFERENCE_SCORE_ID` always works (no import needed); anything
    else must have been imported via `/score/import` in this process, or
    it's a 404 - it means "not imported here", not "feature unimplemented".
    """
    if score_id == _MOCK_REFERENCE_SCORE_ID:
        return _mock_reference_score()
    score = _score_store.get(score_id)
    if score is None:
        raise HTTPException(
            status_code=404,
            detail=f"No score with score_id={score_id!r} has been "
            "imported in this process. Call /score/import first, or "
            f"use score_id={_MOCK_REFERENCE_SCORE_ID!r} for the demo "
            "reference score.",
        )
    return score


def _decode_audio_to_mono_pcm16(raw: bytes) -> tuple[bytes, int]:
    """Decode an uploaded audio file (WAV/FLAC/most soundfile-supported
    formats) into mono 16-bit PCM bytes + sample rate - the contract
    `PitchTracker.transcribe()` expects (see pyin_transcriber.py). Real
    file-format decoding lives here, at the HTTP boundary, so the
    PitchTracker itself stays on the simpler raw-PCM contract that's easy
    to unit-test with synthesized audio.
    """
    import numpy as np
    import soundfile as sf

    data, sample_rate = sf.read(io.BytesIO(raw), dtype="int16", always_2d=False)
    if data.ndim > 1:
        # Multi-channel: average to mono rather than picking one channel.
        data = data.mean(axis=1).astype(np.int16)
    return data.tobytes(), int(sample_rate)


@app.post("/performance/analyze", response_model=AnalyzeResponse)
def analyze_performance(req: AnalyzeRequest) -> AnalyzeResponse:
    """Score lookup and alignment/assessment are real; transcription is
    still `_mock_transcription()` - this endpoint has no way to receive
    audio (JSON body only, matching how it's always worked). For real
    audio, use `/performance/analyze_recording` (multipart file upload),
    which runs the fully real pipeline via `PyinTranscriber`. Kept
    separate rather than merged into one endpoint so this JSON contract -
    and everything that already depends on it - doesn't change.
    """
    profiles = builtin_profiles()
    profile = profiles.get(req.profile_name, profiles["beginner"])
    score = _resolve_score(req.score_id)

    transcription = _mock_transcription()

    alignment = _aligner.align(transcription, score, AlignConfig())
    assessment = _assessor.assess(alignment, score, transcription, profile)

    return AnalyzeResponse(
        transcription=transcription,
        alignment=alignment,
        assessment=assessment,
    )


@app.post("/performance/analyze_recording", response_model=AnalyzeResponse)
async def analyze_recording(
    audio: UploadFile = File(..., description="An audio file (WAV or other soundfile-supported format)."),
    score_id: str = Form(...),
    profile_name: str = Form(default="beginner"),
) -> AnalyzeResponse:
    """FULLY REAL pipeline: real transcription (`PyinTranscriber`, Phase 2)
    + real alignment + real assessment, against an uploaded audio
    recording and a previously-imported (or the fixed demo) score.

    This is genuinely new - Phase 2 didn't have a code path at all before
    (only `/performance/analyze`'s hardcoded mock existed). Validated
    against a real recording during development (see STATUS.md for the
    numbers), not just synthetic test audio - but that validation used
    ad hoc scripts, not this endpoint itself, so treat first real calls
    here as still somewhat unproven end-to-end.
    """
    profiles = builtin_profiles()
    profile = profiles.get(profile_name, profiles["beginner"])
    score = _resolve_score(score_id)

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Uploaded audio file is empty.")

    try:
        pcm16, sample_rate = _decode_audio_to_mono_pcm16(raw)
    except Exception as exc:  # soundfile raises several distinct error types
        raise HTTPException(status_code=422, detail=f"Could not decode audio: {exc}") from exc

    transcription = _transcriber.run(pcm16, sample_rate)

    alignment = _aligner.align(transcription, score, AlignConfig())
    assessment = _assessor.assess(alignment, score, transcription, profile)

    return AnalyzeResponse(
        mock=False,
        transcription=transcription,
        alignment=alignment,
        assessment=assessment,
    )
