"""score-align module contract.

Lines up the performed (detected) note sequence against the reference score
note sequence. The output is an *alignment* (a mapping between performed and
reference indices) - NOT an error classification. Error classification is the
assessment module's job; this module only decides "which performed note
corresponds to which score note (if any)."

Design notes (see docs/ARCHITECTURE.md):
  * The `align()` signature is mode-agnostic so v1 offline DTW can be swapped
    for v2 online/incremental DTW (OLTW) without callers changing. `AlignMode`
    selects strategy; the return type is identical.
  * Skip/repeat handling is FIRST-CLASS, not an afterthought. Plain DTW assumes
    monotonic full coverage and breaks when a performer skips or repeats a
    section. `AlignStrategy` exposes SUBSEQUENCE and RESYNC strategies, and the
    alignment result can carry multiple monotonic `segments` plus explicit
    `skipped`/`repeated` spans.
  * The local-cost threshold that distinguishes "plausibly the same note" from
    "this is a gap" lives here (`AlignConfig`), because the cost function is an
    alignment concern even though wrong-note severity is assessment's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .score_ingest import Score
from .transcription import Transcription


class AlignMode(str, Enum):
    """Computation mode. Lets the v1 offline path and v2 streaming path share
    one interface.
    """

    OFFLINE = "offline"  # v1: classic global/subsequence DTW over full input
    ONLINE = "online"  # v2: incremental OLTW as audio streams in


class AlignStrategy(str, Enum):
    """How to handle the monotonicity / coverage problem."""

    GLOBAL_DTW = "global_dtw"  # naive full monotonic align (baseline only)
    SUBSEQUENCE_DTW = "subsequence_dtw"  # match performance to a sub-span of score
    RESYNC = "resync"  # segment + re-anchor on skips/repeats


class AlignConfig(BaseModel):
    """Alignment tuning. The cost function / gap threshold lives here."""

    strategy: AlignStrategy = AlignStrategy.SUBSEQUENCE_DTW

    pitch_cost_weight: float = 1.0
    timing_cost_weight: float = 0.5

    # Above this local DTW cost, a step is treated as a gap (skip/extra) rather
    # than a match. This is the knob that lets assessment see "no correspondence"
    # instead of a forced bad match.
    gap_cost_threshold: float = Field(
        4.0, gt=0,
        description="Local-cost ceiling above which a pair is left unmatched.",
    )

    # Used by RESYNC: how many consecutive high-cost steps trigger a re-anchor.
    resync_window: int = Field(4, ge=1)


class NotePair(BaseModel):
    """One element of the alignment path. Either index may be None to express a
    missed score note (performed=None) or an extra performed note (ref=None).
    """

    ref_index: Optional[int] = Field(None, description="Index into Score.notes.")
    performed_index: Optional[int] = Field(
        None, description="Index into Transcription.notes."
    )
    local_cost: float = Field(
        0.0, description="DTW local cost for this pair; high => weak match."
    )


class AlignSegment(BaseModel):
    """A maximal monotonic run of the alignment. With RESYNC/SUBSEQUENCE there
    may be several, separated by skipped/repeated spans.
    """

    pairs: list[NotePair]
    ref_start: int
    ref_end: int


class SkipRepeatSpan(BaseModel):
    """An explicitly detected skipped or repeated region of the score."""

    kind: str = Field(..., description="'skipped' or 'repeated'.")
    ref_start: int
    ref_end: int


class Alignment(BaseModel):
    """The single alignment output type, identical for offline and online modes.

    `pairs` is the flattened full path (convenient for assessment); `segments`
    and `skip_repeat_spans` preserve the structure needed to reason about
    skips/repeats. `is_partial` is True until an online alignment has consumed
    the whole performance.
    """

    mode: AlignMode
    strategy: AlignStrategy
    pairs: list[NotePair]
    segments: list[AlignSegment] = Field(default_factory=list)
    skip_repeat_spans: list[SkipRepeatSpan] = Field(default_factory=list)
    is_partial: bool = Field(
        default=False,
        description="True for incremental (online) results not yet finalized.",
    )


class ScoreAligner(ABC):
    """Contract for alignment. v1: OfflineDtwAligner. v2: OnlineDtwAligner.

    Callers use `align()` and never branch on mode. The online path additionally
    implements `align_incremental()` for streaming; offline implementations may
    raise NotImplementedError there.
    """

    @abstractmethod
    def align(
        self,
        performance: Transcription,
        score: Score,
        config: Optional[AlignConfig] = None,
    ) -> Alignment:
        """Produce a (possibly partial) alignment of performance to score.

        Handles skip/repeat per `config.strategy`. The result expresses gaps as
        `NotePair`s with a None index rather than forcing every note to match.
        """

    @abstractmethod
    def supports_mode(self, mode: AlignMode) -> bool:
        """Whether this implementation supports the given computation mode."""

    def align_incremental(
        self,
        performance_chunk: Transcription,
        score: Score,
        state: Optional[dict] = None,
        config: Optional[AlignConfig] = None,
    ) -> tuple[Alignment, dict]:
        """Online/incremental alignment for v2 live mode.

        Consumes one streamed chunk, returns a partial `Alignment` plus opaque
        carry-over `state` to feed into the next call. Offline implementations
        may raise NotImplementedError.
        """
        raise NotImplementedError("incremental alignment is an online-mode feature")
