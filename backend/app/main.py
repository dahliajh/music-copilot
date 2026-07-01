"""Music Copilot backend - FastAPI skeleton.

This wires the four module contracts together, plus a lightweight
persistence seam (`score_store.py`) so a score imported in one request can
be looked up by id in another.

  * score-ingest (`/score/import`): REAL (MusicXMLIngester, Phase 1).
    Imported scores are now persisted (`InMemoryScoreStore`) so a later
    `/performance/analyze` call can look them up by `score_id`.
  * score-align and assessment, inside `/performance/analyze`: REAL
    (OfflineDtwAligner, RuleBasedAssessor). If `req.score_id` matches a
    previously-imported score, alignment/assessment run against that REAL
    score. Unknown ids other than the reserved demo id are a 404.
  * transcription, inside `/performance/analyze`: STILL MOCK. There's no
    real pitch tracker yet (Phase 2 - needs hand-labeled recordings from
    the developer per the MVP plan's evaluation strategy, not started).
    `_mock_transcription()` stands in for it. Because of that, aligning a
    real imported score against the mock performance's fixed 4 notes is
    only meaningful for a small demo score with matching content -
    `AnalyzeResponse.mock=True` still reflects that the pipeline's audio
    side is a placeholder even though the score side may now be real.

Run (once deps are installed):
    uvicorn app.main:app --reload
"""

from __future__ import annotations

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


@app.post("/performance/analyze", response_model=AnalyzeResponse)
def analyze_performance(req: AnalyzeRequest) -> AnalyzeResponse:
    """PARTIALLY REAL. The transcription is still `_mock_transcription()`
    (no real pitch tracker - Phase 2 hasn't landed), but `req.score_id` is
    now looked up for real against whatever was imported via
    `/score/import`: alignment and assessment run against that REAL score.

    `req.score_id == "mock-reference"` (or omitting a prior import
    entirely) falls back to the fixed 4-note demo score
    (`_mock_reference_score()`) matching the mock transcription, so the
    octave-off demo scenario keeps working without importing a file
    first. Any other unrecognized `score_id` is a 404 - it means the
    caller hasn't imported that score in this process, not that the
    feature is unimplemented.

    Once Phase 2 (real transcription) lands, only `_mock_transcription()`
    needs to change - the score lookup, align/assess/response wiring is
    the real, final shape.
    """
    profiles = builtin_profiles()
    profile = profiles.get(req.profile_name, profiles["beginner"])

    if req.score_id == _MOCK_REFERENCE_SCORE_ID:
        score = _mock_reference_score()
    else:
        score = _score_store.get(req.score_id)
        if score is None:
            raise HTTPException(
                status_code=404,
                detail=f"No score with score_id={req.score_id!r} has been "
                "imported in this process. Call /score/import first, or "
                f"use score_id={_MOCK_REFERENCE_SCORE_ID!r} for the demo "
                "reference score.",
            )

    transcription = _mock_transcription()

    alignment = _aligner.align(transcription, score, AlignConfig())
    assessment = _assessor.assess(alignment, score, transcription, profile)

    return AnalyzeResponse(
        transcription=transcription,
        alignment=alignment,
        assessment=assessment,
    )
