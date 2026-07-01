"""Concrete v1 `Assessor`: classifies an `Alignment` into a structured
`Mistake` list using a `ToleranceProfile`.

Design notes (see docs/ARCHITECTURE.md, assessment.py):
  * Indices in `NotePair` are matched against `ScoreNote.index` /
    `DetectedNote.index` (stable identity fields), not raw list position.
    The v1 musicxml_ingester happens to keep these equal to list position,
    but this assessor doesn't assume that — it builds id->note lookup dicts
    so it stays correct even if that ever changes (e.g. after a manual
    score correction re-orders notes).
  * Octave-off classification follows `profile.octave_policy` exactly as
    specified in the architecture doc: same pitch-class + different MIDI
    octave is judged *after* the plain pitch-tolerance check fails, so a
    performance that's merely sharp/flat by a few cents is never
    misclassified as an octave error.
  * `min_confidence_for_pitch_error` gates the wrong-pitch AND octave-off
    verdicts (both are pitch-reading-dependent), but never gates timing
    checks - timing confidence isn't what `DetectedNote.confidence`
    measures (it's pitch-tracker confidence, not amplitude/onset
    confidence; see transcription.py).
  * A tied-continuation note (`ScoreNote.tied_from_prev=True`) has no new
    attack, so a `MISSED_NOTE` verdict for it (no performed note landed on
    that index) is a false positive, not a real miss - it's treated as
    correct instead. This is a known v1 simplification: it doesn't attempt
    to verify the tie was actually held (e.g. no early release detection).
  * A pair can carry multiple independent mistakes (e.g. wrong pitch AND
    late) - they're orthogonal checks, not mutually exclusive branches.
  * Thresholds in `builtin_profiles()` are placeholders (per
    docs/ARCHITECTURE.md); this module's job is to apply whatever profile
    it's given correctly, not to pick good numbers - that's the eval-harness
    work in the plan's section 5, still future work.
  * TIMING IS TEMPO-ELASTIC, not measured against a single fixed bpm - see
    `_TempoCurve` below. This closes a real gap found against an actual
    recording: a fixed-tempo assumption makes free/expressive playing (not
    played to a click track) generate a wall of false "late" verdicts that
    grow without bound as the performance's real pace drifts from the
    printed tempo, even when the performer's relative rhythm is fine. This
    was previously an explicitly deferred limitation (see ARCHITECTURE.md's
    "Known risk areas" #6); still not a full solution (see `_TempoCurve`'s
    own docstring for what it doesn't handle), but a real improvement over
    the single-fixed-bpm baseline.
"""

from __future__ import annotations

import bisect
from typing import Optional

from .assessment import (
    Assessor,
    AssessmentResult,
    Mistake,
    MistakeType,
    OctavePolicy,
    Severity,
    ToleranceProfile,
)
from .score_align import Alignment
from .score_ingest import Score, ScoreNote
from .transcription import DetectedNote, Transcription


class _TempoCurve:
    """Empirical, locally-elastic onset_beats -> expected onset_s mapping,
    built from the alignment's own matched (both-sides-present) pairs
    instead of a single fixed `score.tempo.bpm`.

    Why: a fixed-bpm mapping assumes the performer holds one constant
    tempo for the whole take. Real playing - especially anything not
    performed to a click track - naturally speeds up and slows down
    (rubato, easing into a hard passage, etc.). Comparing against a rigid
    clock makes that normal variation look like a timing mistake that
    gets *worse* the longer the piece runs, even when the performer's
    rhythm relative to their own pace is fine.

    How: for each note being timing-checked, interpolate its expected
    onset between the nearest OTHER matched anchors (leave-one-out - a
    note is never compared against a curve built partly from itself,
    which would make timing trivially "correct"). This tracks genuine
    local tempo drift while still catching a note that's out of place
    relative to its immediate neighbors, drift or no drift.

    What this does NOT solve (still real limitations, not silently
    fixed): it can't distinguish "the performer rushed this one note"
    from "the performer is decelerating through this whole passage" any
    better than a human glancing at 3-4 neighboring notes could - it's a
    local smoothing heuristic, not a tempo model. It also can't tell a
    genuine, intentional pause (rest) from a mistake; and with fewer than
    two OTHER matched anchors nearby (e.g. tiny scores, or a performance
    with almost nothing recognized) it falls back to the fixed-bpm
    mapping, since there's nothing to interpolate from.
    """

    def __init__(self, anchors: list[tuple[float, float]], seconds_per_beat: float):
        # anchors: (onset_beats, onset_s) pairs from real matched NotePairs.
        # Sorted + deduplicated by onset_beats so bisect works and a score
        # with two notes at the same onset_beats (a chord's leftover, or a
        # grace-note edge case) doesn't produce an ill-defined ordering.
        dedup: dict[float, float] = {}
        for beats, seconds in anchors:
            dedup[beats] = seconds
        self._points: list[tuple[float, float]] = sorted(dedup.items())
        self._seconds_per_beat = seconds_per_beat

    def expected_onset_s(self, onset_beats: float, exclude: Optional[tuple[float, float]]) -> float:
        points = self._points
        if exclude is not None and exclude in points:
            points = [p for p in points if p != exclude]

        if len(points) < 2:
            return onset_beats * self._seconds_per_beat  # fixed-tempo fallback

        beats_list = [p[0] for p in points]
        i = bisect.bisect_left(beats_list, onset_beats)
        if i <= 0:
            lo, hi = points[0], points[1]
        elif i >= len(points):
            lo, hi = points[-2], points[-1]
        else:
            lo, hi = points[i - 1], points[i]

        if hi[0] == lo[0]:
            return lo[1]
        slope = (hi[1] - lo[1]) / (hi[0] - lo[0])  # local seconds-per-beat
        return lo[1] + slope * (onset_beats - lo[0])


class RuleBasedAssessor(Assessor):
    """v1 Assessor: per-pair rule checks driven entirely by `ToleranceProfile`.

    No detection/alignment logic lives here - it only classifies the
    `Alignment` it's handed. See `score_align.py`'s docstring for the
    explicit alignment/assessment boundary this respects.
    """

    def assess(
        self,
        alignment: Alignment,
        score: Score,
        performance: Transcription,
        profile: ToleranceProfile,
    ) -> AssessmentResult:
        score_by_index = {n.index: n for n in score.notes}
        perf_by_index = {n.index: n for n in performance.notes}
        seconds_per_beat = 60.0 / score.tempo.bpm

        # Build the tempo curve from every real (both-sides-present) pair up
        # front, from the FULL alignment - not incrementally as we go - so
        # a note early in the pass can still be judged against anchors that
        # come later in score order (e.g. the performer's pace by the end
        # of a passage), not just what's been seen so far.
        anchors = [
            (score_by_index[p.ref_index].onset_beats, perf_by_index[p.performed_index].onset_s)
            for p in alignment.pairs
            if p.ref_index is not None
            and p.performed_index is not None
            and p.ref_index in score_by_index
            and p.performed_index in perf_by_index
        ]
        tempo_curve = _TempoCurve(anchors, seconds_per_beat)

        mistakes: list[Mistake] = []
        correct_ref_indices: list[int] = []

        for pair in alignment.pairs:
            # --- gap pairs: one side is None -------------------------------
            if pair.ref_index is not None and pair.performed_index is None:
                score_note = score_by_index.get(pair.ref_index)
                if score_note is not None and score_note.tied_from_prev:
                    # Tied continuation: no new attack expected, not a miss.
                    correct_ref_indices.append(pair.ref_index)
                else:
                    mistakes.append(
                        Mistake(
                            ref_index=pair.ref_index,
                            performed_index=None,
                            type=MistakeType.MISSED_NOTE,
                            severity=Severity.ERROR,
                            detail="No performed note matched this score note.",
                        )
                    )
                continue

            if pair.ref_index is None and pair.performed_index is not None:
                mistakes.append(
                    Mistake(
                        ref_index=None,
                        performed_index=pair.performed_index,
                        type=MistakeType.EXTRA_NOTE,
                        severity=Severity.WARNING,
                        detail="Performed note has no corresponding score note.",
                    )
                )
                continue

            if pair.ref_index is None or pair.performed_index is None:
                # Both None: nothing to assess (defensive; shouldn't occur).
                continue

            # --- real pair: both sides present ------------------------------
            score_note = score_by_index.get(pair.ref_index)
            detected = perf_by_index.get(pair.performed_index)
            if score_note is None or detected is None:
                # Alignment referenced an index this Score/Transcription
                # doesn't have. Not assessable; skip rather than crash.
                continue

            pair_mistakes = self._assess_pair(score_note, detected, profile, tempo_curve)
            if pair_mistakes:
                mistakes.extend(pair_mistakes)
            else:
                correct_ref_indices.append(pair.ref_index)

        return AssessmentResult(
            profile_name=profile.name,
            mistakes=mistakes,
            correct_ref_indices=correct_ref_indices,
        )

    @staticmethod
    def _assess_pair(
        score_note: ScoreNote,
        detected: DetectedNote,
        profile: ToleranceProfile,
        tempo_curve: "_TempoCurve",
    ) -> list[Mistake]:
        mistakes: list[Mistake] = []

        # ---- pitch -------------------------------------------------------
        detected_cents = detected.midi * 100 + detected.cents_offset
        reference_cents = score_note.midi * 100
        cents_off = detected_cents - reference_cents
        low_confidence = detected.confidence < profile.min_confidence_for_pitch_error

        if abs(cents_off) <= profile.pitch_tolerance_cents:
            pass  # pitch correct, nothing to record
        elif low_confidence:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.LOW_CONFIDENCE,
                    severity=Severity.INFO,
                    detail=(
                        f"Pitch confidence {detected.confidence:.2f} is below "
                        f"the {profile.min_confidence_for_pitch_error:.2f} "
                        "threshold; suppressing wrong-pitch verdict, "
                        "flagging for review instead."
                    ),
                    cents_off=cents_off,
                )
            )
        elif detected.pitch_class == score_note.pitch_class and detected.midi != score_note.midi:
            mistakes.extend(
                RuleBasedAssessor._octave_off_mistake(score_note, detected, profile, cents_off)
            )
        else:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.WRONG_PITCH,
                    severity=Severity.ERROR,
                    detail=f"{cents_off:+.0f} cents from the reference pitch.",
                    cents_off=cents_off,
                )
            )

        # ---- timing --------------------------------------------------------
        # Tempo-elastic: compared against this note's local expected onset
        # (interpolated from OTHER matched notes' actual pace), not a single
        # fixed bpm for the whole performance. See _TempoCurve's docstring.
        expected_onset_s = tempo_curve.expected_onset_s(
            score_note.onset_beats, exclude=(score_note.onset_beats, detected.onset_s)
        )
        timing_error_ms = (detected.onset_s - expected_onset_s) * 1000.0
        if abs(timing_error_ms) > profile.timing_tolerance_ms:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.TIMING_LATE if timing_error_ms > 0 else MistakeType.TIMING_EARLY,
                    severity=Severity.WARNING,
                    detail=f"{timing_error_ms:+.0f} ms from the reference onset.",
                    timing_error_ms=timing_error_ms,
                )
            )

        return mistakes

    @staticmethod
    def _octave_off_mistake(
        score_note: ScoreNote,
        detected: DetectedNote,
        profile: ToleranceProfile,
        cents_off: float,
    ) -> list[Mistake]:
        if profile.octave_policy == OctavePolicy.IGNORE:
            return []
        if profile.octave_policy == OctavePolicy.CORRECT_WITH_WARNING:
            return [
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.OCTAVE_OFF,
                    severity=Severity.INFO,
                    detail="Correct pitch class, wrong octave.",
                    cents_off=cents_off,
                )
            ]
        # HARD_ERROR
        return [
            Mistake(
                ref_index=score_note.index,
                performed_index=detected.index,
                type=MistakeType.WRONG_PITCH,
                severity=Severity.ERROR,
                detail=(
                    "Correct pitch class, wrong octave (counted as wrong "
                    "pitch under the HARD_ERROR octave policy)."
                ),
                cents_off=cents_off,
            )
        ]
