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
    min_confidence_for_pitch_error: float = Field(0.5, ge=0.0, le=1.0)


def builtin_profiles() -> dict[str, ToleranceProfile]:
    """Starter profiles. Exact values are placeholders to be tuned against real
    recordings (see plan section 6, open questions).
    """

    return {
        "beginner": ToleranceProfile(
            name="beginner",
            pitch_tolerance_cents=60.0,
            timing_tolerance_ms=150.0,
            octave_policy=OctavePolicy.CORRECT_WITH_WARNING,
        ),
        "advanced": ToleranceProfile(
            name="advanced",
            pitch_tolerance_cents=25.0,
            timing_tolerance_ms=30.0,
            octave_policy=OctavePolicy.CORRECT_WITH_WARNING,
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
