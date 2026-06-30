"""score-ingest module contract.

Responsible for turning *some* external score representation (MusicXML file,
typeset PDF, or - later - a phone photo run through OMR) into a single canonical
internal score representation that every downstream module consumes.

Design notes (see docs/ARCHITECTURE.md):
  * The canonical `Score` type is deliberately decoupled from *how* it was
    produced. v1 ingests clean MusicXML directly; v1.5/v2 add an OMR path that
    produces the *same* `Score` object, so nothing downstream changes.
  * OMR will never be perfect, so the contract anticipates a manual-correction
    step: ingest can return notes flagged `needs_review`, and a corrected score
    is just another `Score` fed back in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ScoreSourceFormat(str, Enum):
    """How the raw score reached us. Lets callers/telemetry reason about
    expected reliability (hand-entered MusicXML is trusted; OMR output is not).
    """

    MUSICXML = "musicxml"
    MUSICXML_PDF = "musicxml_pdf"  # PDF converted once by hand to MusicXML
    OMR_PHOTO = "omr_photo"  # v1.5+: phone photo -> OMR -> MusicXML


class ScoreNote(BaseModel):
    """A single notated reference note in the canonical score.

    Pitch is stored as MIDI note number (integer, C4 = 60) so that the
    alignment and assessment modules can do octave / pitch-class arithmetic
    without re-parsing note names. `pitch_class` is derived (midi % 12) but
    stored explicitly because the octave-off policy in `assessment` keys off it.
    """

    index: int = Field(..., description="0-based position in the monophonic part.")
    midi: int = Field(..., description="MIDI note number, C4 = 60.")
    pitch_class: int = Field(..., ge=0, le=11, description="midi % 12.")

    # Score time, in beats from the start of the excerpt. We keep musical time
    # (beats) here rather than seconds because the score itself is tempo-free;
    # the click-track / fixed tempo (see TempoReference) converts to seconds.
    onset_beats: float
    duration_beats: float

    tied_from_prev: bool = False
    needs_review: bool = Field(
        default=False,
        description="Set by OMR ingest when confidence is low; surfaced to the "
        "manual-correction UI. Always False for hand-entered MusicXML.",
    )


class TempoReference(BaseModel):
    """Fixed-tempo / click-track reference for the excerpt.

    The plan requires a tempo reference for v1 so that 'rushed' vs. 'wrong' is
    well-defined in the assessment module. v1 supports a single constant BPM;
    the optional `beat_seconds` map leaves room for per-beat tempo maps later
    without a contract change.
    """

    bpm: float = Field(..., gt=0)
    beats_per_measure: int = 4
    beat_seconds: Optional[list[float]] = Field(
        default=None,
        description="Optional explicit onset-time (seconds) of each beat. When "
        "None, derived from constant bpm. Reserved for v2 tempo maps.",
    )


class Score(BaseModel):
    """Canonical internal score representation. The single output of ingest and
    the single score-side input to alignment/assessment.
    """

    score_id: str
    title: Optional[str] = None
    source_format: ScoreSourceFormat
    tempo: TempoReference
    notes: list[ScoreNote]

    @property
    def needs_manual_correction(self) -> bool:
        return any(n.needs_review for n in self.notes)


class ScoreIngestError(BaseModel):
    """Non-fatal parse warning (e.g. an unsupported MusicXML feature was
    dropped). Fatal problems should raise; this is for surfacing lossy parses.
    """

    code: str
    message: str


class ScoreIngestResult(BaseModel):
    score: Score
    warnings: list[ScoreIngestError] = Field(default_factory=list)


class ScoreIngester(ABC):
    """Contract for any score-ingest implementation.

    v1 implementation: MusicXMLIngester (music21/partitura under the hood).
    v1.5+: OmrIngester producing the same `ScoreIngestResult`.
    """

    @abstractmethod
    def supports(self, source_format: ScoreSourceFormat) -> bool:
        """Whether this implementation can handle the given source format."""

    @abstractmethod
    def ingest(
        self,
        raw: bytes,
        source_format: ScoreSourceFormat,
        *,
        score_id: Optional[str] = None,
    ) -> ScoreIngestResult:
        """Parse raw score bytes into the canonical `Score`.

        Raises on unrecoverable parse failure; recoverable/lossy issues are
        returned as `warnings`.
        """
