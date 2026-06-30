"""transcription module contract.

Turns a mono audio recording into a timestamped sequence of detected notes,
each carrying a per-note pitch *confidence* (from the tracker's own confidence
output, NOT raw amplitude).

Design notes (see docs/ARCHITECTURE.md):
  * Octave errors are the dominant double-bass failure mode. The contract keeps
    octave correction as a SEPARATE, swappable post-processing step
    (`OctaveCorrector`) rather than baking it into the tracker. A `PitchTracker`
    emits raw notes; an `OctaveCorrector` rewrites octaves and records what it
    changed; the `Transcriber` facade composes them.
  * Per-note `confidence` is first-class so that alignment/assessment can
    down-weight or skip untrustworthy readings (quiet low-register notes are
    exactly where confidence is weakest).
  * `frame_size_ms` is exposed as explicit config because of the low-frequency
    vs. timing-precision tradeoff (~31-41 Hz needs ~50-100ms windows, which
    fights onset precision). It is a tuning knob, not a hidden default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PitchAlgorithm(str, Enum):
    PYIN = "pyin"
    CREPE = "crepe"


class DetectedNote(BaseModel):
    """One detected note event. Times are seconds from the start of the
    recording. `midi` may be fractional internally but is reported rounded;
    `cents_offset` preserves the fine deviation for pitch-tolerance checks.
    """

    index: int
    midi: int = Field(..., description="Nearest MIDI note number, C4 = 60.")
    cents_offset: float = Field(
        0.0, description="Signed cents from the nominal MIDI pitch (-50..+50)."
    )
    onset_s: float
    offset_s: float

    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Tracker's own per-note pitch confidence. NOT amplitude.",
    )
    octave_corrected: bool = Field(
        default=False,
        description="True if an OctaveCorrector shifted this note's octave.",
    )

    @property
    def pitch_class(self) -> int:
        return self.midi % 12


class TranscriptionConfig(BaseModel):
    """Explicit, tunable transcription parameters."""

    algorithm: PitchAlgorithm = PitchAlgorithm.PYIN

    # Low-fundamental vs. timing-precision tradeoff lives here.
    frame_size_ms: float = Field(
        60.0, gt=0,
        description="Analysis window. Larger resolves low fundamentals but "
        "blurs onset timing. Tune for the bass register.",
    )
    hop_size_ms: float = Field(10.0, gt=0)

    # Range prior: clamp candidate pitches to the instrument's register to
    # suppress octave jumps. Defaults span double-bass B0..G3-ish playing range.
    min_midi: int = Field(23, description="Lowest plausible MIDI note (B0).")
    max_midi: int = Field(67, description="Highest plausible MIDI note.")

    confidence_threshold: float = Field(
        0.5, ge=0.0, le=1.0,
        description="Below this, a frame is treated as unvoiced/untrusted.",
    )


class Transcription(BaseModel):
    """Full output of the transcription stage."""

    notes: list[DetectedNote]
    config: TranscriptionConfig
    sample_rate: int
    duration_s: float


class PitchTracker(ABC):
    """Raw monophonic pitch + onset detection. Emits notes WITHOUT octave
    correction. Swappable: PyinTracker (v1) / CrepeTracker.
    """

    @abstractmethod
    def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        config: TranscriptionConfig,
    ) -> Transcription:
        """Detect notes from raw mono PCM audio."""


class OctaveCorrector(ABC):
    """Separate, swappable octave-correction pass.

    Kept out of the tracker so the correction policy (range-clamp, median-pitch
    smoothing, overtone heuristics) can be iterated and unit-tested in isolation
    against hand-labeled recordings.
    """

    @abstractmethod
    def correct(
        self,
        transcription: Transcription,
        config: TranscriptionConfig,
    ) -> Transcription:
        """Return a new Transcription with octaves corrected and the affected
        notes marked `octave_corrected=True`.
        """


class Transcriber(ABC):
    """Facade composing a PitchTracker with an optional OctaveCorrector.

    Callers depend only on this; whether/which correction runs is an
    implementation detail behind the same signature.
    """

    @abstractmethod
    def run(
        self,
        audio: bytes,
        sample_rate: int,
        config: Optional[TranscriptionConfig] = None,
    ) -> Transcription:
        """Track pitches and apply octave correction, returning final notes."""
