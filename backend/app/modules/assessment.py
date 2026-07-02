"""assessment module contract.

Consumes an `Alignment` (performed <-> reference correspondence) plus the
`Score` and `Transcription`, and diffs it into a structured list of mistakes.

Design notes (see docs/ARCHITECTURE.md):
  * Tolerances are passed in as a named `ToleranceProfile` config object
    (Beginner / Advanced / custom), NOT hardcoded constants. Adding or tuning a
    skill level never touches detection or alignment code.
  * The octave-off-but-same-pitch-class policy is an EXPLICIT enum field
    (`OctavePolicy`) on the profile. The plan's recommended v1 default is
    CORRECT_WITH_WARNING (not a hard error), because octave-detection errors are
    common enough that hard-failing them erodes trust with false positives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .score_align import Alignment
from .score_ingest import Score
from .transcription import Transcription


class OctavePolicy(str, Enum):
    """How to classify a note that is the correct pitch-class but wrong octave."""

    CORRECT_WITH_WARNING = "correct_with_warning"  # v1 recommended default
    HARD_ERROR = "hard_error"  # count as a wrong pitch
    IGNORE = "ignore"  # treat as fully correct, no flag


class ToleranceProfile(BaseModel):
    """Named skill-level tolerance config, passed into assessment.

    Built-in profiles are provided by `builtin_profiles()`, but any custom
    profile with the same shape is accepted - no code change to add a level.
    """

    name: str

    # Pitch tolerance, in cents. A reading within this of the reference is
    # "correct"; beyond it is a wrong-pitch mistake.
    pitch_tolerance_cents: float = Field(..., ge=0)

    # Timing tolerance, in milliseconds, around the reference onset.
    timing_tolerance_ms: float = Field(..., ge=0)

    octave_policy: OctavePolicy = OctavePolicy.CORRECT_WITH_WARNING

    # Below this transcription confidence, suppress a wrong-pitch verdict (the
    # reading itself is untrustworthy) and flag for review instead.
    #
    # DEFAULT IS 0.0 (effectively disabled) IN builtin_profiles() BELOW - see
    # that function's docstring. The field/mechanism itself is kept (not
    # removed) because it's a reasonable idea in principle and a future,
    # better-behaved confidence signal might make it useful; a fixed
    # PROFILE in test_rule_based_assessor.py sets it to 0.5 explicitly and
    # tests the suppression logic still works correctly when a caller does
    # want it.
    min_confidence_for_pitch_error: float = Field(0.5, ge=0.0, le=1.0)


def builtin_profiles() -> dict[str, ToleranceProfile]:
    """Starter profiles. Exact values are placeholders to be tuned against real
    recordings (see plan section 6, open questions).

    `min_confidence_for_pitch_error` is 0.0 (never suppresses) in both
    profiles below - NOT the field's own placeholder default of 0.5. This
    was deliberately disabled, not merely left untuned: validated against a
    real ~102s double-bass recording (272-note reference score, real
    `PyinTranscriber` output, real `OfflineDtwAligner` matches), pYIN's
    per-note confidence (`DetectedNote.confidence`, a voiced-probability
    average - see `pyin_transcriber.py`'s `_pitch_stats`) does NOT predict
    pitch correctness on this instrument/recording: bucketing 166 real
    matched note pairs by confidence and checking exact-pitch-match rate
    per bucket is flat (18-36%, no trend) across the entire observed
    confidence range (0.03-0.58), and the Pearson correlation between
    confidence and pitch-class distance from the reference is -0.004 -
    indistinguishable from zero. A fixed threshold of 0.5 (the field's
    placeholder default) meant this gate suppressed the real pitch verdict
    on ~99% of all detected notes on that recording, regardless of whether
    the reading was actually right or wrong - not a conservative safety
    margin, just discarding real signal. Cents-offset magnitude
    (`|DetectedNote.cents_offset|`, i.e. how far the raw estimate sits from
    the nearest semitone) was also checked as an alternative gating signal
    and is similarly uncorrelated (-0.013). See `docs/ARCHITECTURE.md`
    risk area #3 for the full writeup, including what WAS found to
    strongly predict correctness (the aligner's own `NotePair.local_cost`)
    and why that can't be reused here as an independent trust signal (it's
    circular - `local_cost` already IS a pitch-distance measure, so gating
    on it would mean "only trust the verdict when the verdict already
    looks right", not a real confidence check).
    """

    return {
        "beginner": ToleranceProfile(
            name="beginner",
            pitch_tolerance_cents=60.0,
            timing_tolerance_ms=150.0,
            octave_policy=OctavePolicy.CORRECT_WITH_WARNING,
            min_confidence_for_pitch_error=0.0,
        ),
        "advanced": ToleranceProfile(
            name="advanced",
            pitch_tolerance_cents=25.0,
            timing_tolerance_ms=30.0,
            octave_policy=OctavePolicy.CORRECT_WITH_WARNING,
            min_confidence_for_pitch_error=0.0,
        ),
    }


class MistakeType(str, Enum):
    WRONG_PITCH = "wrong_pitch"
    OCTAVE_OFF = "octave_off"  # same pitch-class, wrong octave
    TIMING_EARLY = "timing_early"
    TIMING_LATE = "timing_late"
    MISSED_NOTE = "missed_note"  # score note with no performed match
    EXTRA_NOTE = "extra_note"  # performed note with no score match
    LOW_CONFIDENCE = "low_confidence"  # flagged for review, not a hard error


class Severity(str, Enum):
    INFO = "info"  # e.g. octave-off under CORRECT_WITH_WARNING
    WARNING = "warning"
    ERROR = "error"


class Mistake(BaseModel):
    """One assessed discrepancy, keyed to the score note index for the UI."""

    ref_index: Optional[int]
    performed_index: Optional[int]
    type: MistakeType
    severity: Severity
    detail: Optional[str] = None

    # Populated where meaningful, for richer UI / debugging.
    cents_off: Optional[float] = None
    timing_error_ms: Optional[float] = None


class AssessmentResult(BaseModel):
    """Structured mistake list. `correct_indices` lets feedback-ui color the
    rest green without recomputing.
    """

    profile_name: str
    mistakes: list[Mistake]
    correct_ref_indices: list[int] = Field(default_factory=list)

    @property
    def has_review_flags(self) -> bool:
        return any(
            m.severity is not Severity.ERROR
            and m.type in (MistakeType.OCTAVE_OFF, MistakeType.LOW_CONFIDENCE)
            for m in self.mistakes
        )


class Assessor(ABC):
    """Contract for the diff stage. v1: RuleBasedAssessor."""

    @abstractmethod
    def assess(
        self,
        alignment: Alignment,
        score: Score,
        performance: Transcription,
        profile: ToleranceProfile,
    ) -> AssessmentResult:
        """Classify each aligned pair into correct / mistake, applying the given
        tolerance profile and its octave policy.
        """
